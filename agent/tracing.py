"""逐步追蹤（tracing）：每一題記下每一步做了什麼、花多久，寫成本機 JSONL，不需要任何外部服務或 API 金鑰。

開法：run_agent.py 加 --trace，或環境變數 TRACE=1（harness/loop.py probe、scripts/run_eval.py 會把環境變數傳下去）。
輸出：traces/<run-id>.jsonl（可用 TRACE_DIR 改資料夾）。第一行是這次跑分的資訊（type=run），之後每一題一行（type=question）。

每一題的 spans 是一棵樹（parent_id 指向上一層），欄位仿 OpenTelemetry（trace_id／span_id／parent_id／
start_ns／end_ns／attrs），trace_report.py --otlp 可以轉成 OTLP JSON：
  turn      一次 ask()（追問題的前一題是 ask.before）
  llm       一次 Ollama /api/chat（模型、思考模式、token 數、Ollama 回報的耗時、模型要呼叫的工具）
  tool      一次 MCP 工具呼叫（名稱、參數、結果摘要；搜尋會解析出每份文件的名次、檔名、相關度、命中段數）
  check     一個有觸發的程式檢查（FORMULA_CHECK、KIN_LOOKUP…）；retry=true 表示退回模型重答，底下的 llm／tool 就是重答的步驟

追蹤只「旁觀」：不改訊息、不改工具參數、不多呼叫模型或工具（唯一多的是跑分開頭一次 GET /api/version），
關掉時每個呼叫都是空操作。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULT_PREVIEW = 160  # 工具結果只留前幾個字

# search_related_policies 每份文件的標題行（格式見 src/policy_mcp/server.py）
_HIT = re.compile(r"^(\d+)\. \[\[([^\]]+)\]\]（相關度 ([0-9.]+)，命中 (\d+) 段）\n(《全文》|《片段》)", re.M)
# 會記進 run 標頭的開關（只記這些，不整包倒出環境變數）
FLAGS = ("CHAT_MODEL", "THINK", "CALC_CHECK", "RECHECK_MODE", "RECHECK_THINK", "NUMERIC_TRIGGER", "DOC_CHECK",
         "REFUSE_CHECK", "REFUSE_THRESHOLD", "KIN_CHECK", "SUPPORT_CHECK", "SHOW_WORK", "ARITH_VERIFY", "CITE_READ",
         "TERM_CHECK", "FORMULA_CHECK", "KIN_LOOKUP", "LATEX_PLAIN", "FORMULA_FALLBACK", "KIN_COMPOUND", "BENEFIT_CHECK")


def parse_search(text: str) -> list[dict]:
    """搜尋結果 → [{rank, file, score, hits, mode, snippets}]。mode：full＝附全文、snippets＝只有片段。"""
    ms = list(_HIT.finditer(text))
    out = []
    for i, m in enumerate(ms):
        seg = text[m.start(): ms[i + 1].start() if i + 1 < len(ms) else len(text)]
        hit = {"rank": int(m.group(1)), "file": m.group(2), "score": float(m.group(3)), "hits": int(m.group(4)),
               "mode": "full" if m.group(5) == "《全文》" else "snippets"}
        if hit["mode"] == "snippets":
            hit["snippets"] = [ln[4:44] for ln in seg.splitlines() if ln.startswith("  > ")]
        out.append(hit)
    return out


def tool_attrs(name: str, text: str) -> dict:
    """工具結果的摘要：長度、前幾個字；搜尋另外解析文件清單，讀文件記下讀到哪一份。"""
    a: dict = {"result_chars": len(text), "result_preview": text[:RESULT_PREVIEW]}
    if name.endswith("search_related_policies"):
        a["retrieved"] = parse_search(text)
    elif name.endswith("read_specific_document"):
        m = re.match(r"\[\[([^\]]+)\]\]", text)
        a["read_file"] = m.group(1) if m else None
    return a


def llm_attrs(data: dict) -> dict:
    """Ollama /api/chat 的回應 → 模型、token 數、Ollama 自己量的耗時、要呼叫的工具。"""
    msg = data.get("message") or {}
    ns = lambda k: round(data[k] / 1e6, 1) if isinstance(data.get(k), (int, float)) else None  # noqa: E731
    return {"response_model": data.get("model"), "input_tokens": data.get("prompt_eval_count"),
            "output_tokens": data.get("eval_count"), "ollama_total_ms": ns("total_duration"),
            "ollama_load_ms": ns("load_duration"),
            "tool_calls": [tc["function"]["name"] for tc in msg.get("tool_calls") or []],
            "content_chars": len(msg.get("content") or ""), "thinking_chars": len(msg.get("thinking") or "")}


def rel_path(p) -> str:
    """路徑改成 repo 內的相對路徑（追蹤檔不放本機絕對路徑）。"""
    try:
        return Path(p).resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return Path(p).name


def _git_commit() -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return (r.stdout.strip() + ("-dirty" if dirty else "")) or None
    except (OSError, subprocess.SubprocessError):
        return None


class Tracer:
    """關掉時（enabled=False）所有方法都是空操作。"""

    def __init__(self, enabled: bool = False, path: Path | None = None):
        self.enabled, self.path = enabled, path
        self._stack: list[dict] = []
        self._spans: list[dict] = []
        self._q: dict | None = None

    @classmethod
    def from_env(cls, flag: bool, name: str) -> "Tracer":
        if not (flag or os.getenv("TRACE", "0") == "1"):
            return cls()
        d = Path(os.getenv("TRACE_DIR", ROOT / "traces"))
        d.mkdir(parents=True, exist_ok=True)
        run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{name}"
        return cls(True, d / f"{run_id}.jsonl")

    @property
    def run_id(self) -> str:
        return self.path.stem if self.path else ""

    def _write(self, rec: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def header(self, **info) -> None:
        if not self.enabled:
            return
        flags = {k: os.environ[k] for k in FLAGS if k in os.environ}
        for k in ("PROMPT_FILE", "RECHECK_PROMPT_FILE"):
            if os.getenv(k):
                flags[k] = rel_path(os.environ[k])
        self._write({"type": "run", "run_id": self.run_id, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                     "git_commit": _git_commit(), "flags": flags, **info})

    # ---- 一題 ----
    def start_question(self, idx: int, item: dict) -> None:
        if not self.enabled:
            return
        self._spans, self._stack = [], []
        self._q = {"type": "question", "run_id": self.run_id, "trace_id": secrets.token_hex(16), "idx": idx,
                   "q": item.get("q"), "before": item.get("before", []), "expect_file": item.get("expect_file"),
                   "kind": "refuse" if item.get("refuse") else "not_found" if item.get("not_found") else "answer",
                   "start_ns": time.time_ns()}

    def end_question(self, **result) -> None:
        if not self.enabled or self._q is None:
            return
        end = time.time_ns()
        q = self._q
        q.update(result, end_ns=end, total_ms=round((end - q["start_ns"]) / 1e6, 1), spans=self._spans)
        self._write(q)
        self._q = None

    # ---- 步驟 ----
    @contextmanager
    def span(self, name: str, kind: str, **attrs):
        """with TRACE.span(...) as a: a 是 attrs dict，可以在裡面補資料。例外照樣往外丟，span 會記下 error。"""
        if not self.enabled or self._q is None:
            yield {}
            return
        sp = {"span_id": secrets.token_hex(8), "parent_id": self._stack[-1]["span_id"] if self._stack else None,
              "name": name, "kind": kind, "start_ns": time.time_ns(), "attrs": dict(attrs)}
        self._spans.append(sp)
        self._stack.append(sp)
        try:
            yield sp["attrs"]
        except BaseException as e:
            sp["attrs"]["error"] = f"{type(e).__name__}: {e}"[:200]
            raise
        finally:
            self._stack.pop()
            sp["end_ns"] = time.time_ns()
            sp["duration_ms"] = round((sp["end_ns"] - sp["start_ns"]) / 1e6, 1)

    def event(self, name: str, kind: str = "check", **attrs) -> None:
        """不花時間的步驟（例：程式直接改寫答案）。"""
        with self.span(name, kind, **attrs):
            pass


def say(tr: Tracer) -> None:
    if tr.enabled:
        print(f"追蹤檔：{rel_path(tr.path)}（看摘要：uv run python agent/trace_report.py {rel_path(tr.path)}）", file=sys.stderr)

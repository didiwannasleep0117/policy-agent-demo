"""評測分數的共用邏輯：重新評分已存的結果、跟基準比較、產生 README 分數區塊。

不需要模型（評分只看存下來的答案和工具呼叫紀錄），所以 CI（scripts/check_regression.py）和本機（scripts/run_eval.py）共用。

- eval/latest.json   最近一次本機評測的分數（README 最上面的分數區塊就是從這裡產生）
- eval/baseline.json 回歸基準：每一組最少要答對幾題（CI 檢查用）
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PROMPT_FILE", str(ROOT / "agent" / "system_prompt.md"))  # run_agent 載入時要讀 prompt
sys.path.insert(0, str(ROOT / "agent"))
from run_agent import grade  # noqa: E402  評分規則只有一份：agent/run_agent.py 的 grade()

RUNS, BOARD, CANDS = ROOT / "harness" / "runs", ROOT / "harness" / "scoreboard.csv", ROOT / "harness" / "candidates"
SPLITS = {s: ROOT / "eval" / f"{s}.yaml" for s in ("tune", "holdout", "holdout2", "holdout3", "holdout4", "holdout5")}  # 同 harness/loop.py
SPLIT_NAMES = {"tune": "調整組", "holdout": "第 1 批隱藏組", "holdout2": "第 2 批隱藏組", "holdout3": "第 3 批隱藏組",
               "holdout4": "第 4 批隱藏組", "holdout5": "第 5 批隱藏組"}
LATEST, BASELINE, README = ROOT / "eval" / "latest.json", ROOT / "eval" / "baseline.json", ROOT / "README.md"
START, END = "<!-- EVAL:START -->", "<!-- EVAL:END -->"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def write_text_keep_eol(path: Path, text: str) -> None:
    """寫回時保留原本的換行（Windows 上 git 可能把檔案轉成 CRLF），避免整份檔案都變成 diff。"""
    if path.exists() and b"\r\n" in path.read_bytes():
        text = text.replace("\n", "\r\n")
    path.write_bytes(text.encode("utf-8"))


def cand_env(cand: str) -> dict:
    return yaml.safe_load((CANDS / cand / "env.yaml").read_text(encoding="utf-8")) or {}


def model_name(cand: str) -> str:
    """模型名稱：環境變數 CHAT_MODEL（跟 run_agent.py 一樣，預設 qwen3:8b）；註明思考模式的開法。"""
    env = cand_env(cand)
    name = str(env.get("CHAT_MODEL") or os.getenv("CHAT_MODEL", "qwen3:8b"))
    if str(env.get("THINK", "0")) == "1":
        return name + "（思考模式）"
    return name + ("（只在驗算時思考）" if str(env.get("RECHECK_THINK", "0")) == "1" else "")


def _board_time(cand: str, split: str) -> str | None:
    if not BOARD.exists():
        return None
    with BOARD.open(encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["candidate"] == cand and r["split"] == split]
    return rows[-1]["time"] if rows else None


def regrade_saved(item: dict, r: dict) -> bool:
    """用現在的 grade() 重新評分一筆存下來的結果（同 harness/loop.py rescore）。

    結果檔沒有存工具輸出全文，「看過全文」沿用跑分當下的判定（why 裡的 看過全文=True）。
    """
    read = "看過全文=True" in r.get("why", "")
    outputs = [f"[[{item['expect_file']}]]\n《全文》"] if (read and item.get("expect_file")) else []
    return grade(item, {"answer": r["answer"], "calls": r["calls"], "outputs": outputs})[0]


def regrade(cand: str, split: str) -> tuple[int, int, float, list[str]]:
    """用「現在的」評分規則和題目重新評分 harness/runs/ 裡存的結果（不改檔案）。

    回傳（通過、總題數、平均秒數、問題清單）；結果檔的題目跟題庫不一致時，問題清單會寫原因。
    """
    path = RUNS / f"{cand}__{split}.json"
    if not path.exists():
        return 0, 0, 0.0, [f"找不到結果檔 {path.relative_to(ROOT).as_posix()}"]
    res = json.loads(path.read_text(encoding="utf-8"))
    items = {i["q"]: i for i in yaml.safe_load(SPLITS[split].read_text(encoding="utf-8"))}
    problems = []
    missing, extra = set(items) - {r["q"] for r in res}, {r["q"] for r in res} - set(items)
    if missing or extra:
        problems.append(f"{split} 題庫跟結果檔不一致（結果檔少 {len(missing)} 題、多 {len(extra)} 題），要在本機重跑評測")
    passed = sum(regrade_saved(items[r["q"]], r) for r in res if r["q"] in items)
    avg = round(sum(r.get("seconds") or 0 for r in res) / len(res), 1) if res else 0.0
    return passed, len(items), avg, problems


def summarize(cand: str, when: dict[str, str] | None = None) -> dict:
    """把候選各組的結果整理成 latest.json 的格式。時間用 when[split]，沒有就用 scoreboard 裡最後一次跑分的時間。"""
    out = {"candidate": cand, "model": model_name(cand), "splits": {}}
    for split in SPLITS:
        passed, total, avg, problems = regrade(cand, split)
        if problems:
            raise SystemExit("；".join(problems))
        out["splits"][split] = {"passed": passed, "total": total, "avg_seconds": avg,
                                "time": (when or {}).get(split) or _board_time(cand, split)
                                or time.strftime("%Y-%m-%d %H:%M")}
    return out


def compare(latest: dict, baseline: dict) -> list[str]:
    """回傳退步的原因（空清單 = 沒有退步）。allowed_drop：容許少幾題（同一題每次跑會有 ±1 的浮動）。"""
    drop = int(baseline.get("allowed_drop", 0))
    fails = []
    for split, b in baseline.get("splits", {}).items():
        cur = latest.get("splits", {}).get(split)
        name = SPLIT_NAMES.get(split, split)
        if not cur:
            fails.append(f"{name}（{split}）沒有最新結果")
        elif cur["total"] != b["total"]:
            fails.append(f"{name}題數變了（基準 {b['total']}，現在 {cur['total']}），確認後用 run_eval.py --accept 更新基準")
        elif cur["passed"] < b["passed"] - drop:
            fails.append(f"{name}退步：{cur['passed']}/{cur['total']}，基準 {b['passed']}/{b['total']}（容許少 {drop} 題）")
    return fails


def raise_baseline(latest: dict, baseline: dict, force: bool = False) -> dict:
    """基準只往上調：某一組這次答對的題數 ≥ 基準就換成這次的結果。force=True 全部換成這次的結果。"""
    new = {"allowed_drop": int(baseline.get("allowed_drop", 1)), "splits": dict(baseline.get("splits", {}))}
    for split, cur in latest["splits"].items():
        b = new["splits"].get(split)
        if force or not b or (cur["total"] == b["total"] and cur["passed"] >= b["passed"]):
            new["splits"][split] = {"candidate": latest["candidate"], "model": latest["model"], "time": cur["time"],
                                    "passed": cur["passed"], "total": cur["total"]}
    return new


def _pct(p: int, t: int) -> str:
    return f"{100 * p / t:.1f}".rstrip("0").rstrip(".") + "%" if t else "-"


def render_block(latest: dict, baseline: dict) -> str:
    lines = [START, "<!-- 這段由 scripts/run_eval.py 自動產生，請不要手動修改 -->",
             f"**最新評測**（Python 版 agent）：候選 `{latest['candidate']}`，模型 `{latest['model']}`"
             f"（[怎麼看這些分數](docs/evaluation.md#分數怎麼看)）", ""]
    for split, s in latest["splits"].items():
        b = baseline.get("splits", {}).get(split)
        base = f"，CI 基準 ≥ {b['passed'] - int(baseline.get('allowed_drop', 0))}" if b else ""
        lines.append(f"- {SPLIT_NAMES.get(split, split)} {split}：**{s['passed']}/{s['total']}**（{_pct(s['passed'], s['total'])}），"
                     f"平均 {s['avg_seconds']} 秒／題，{s['time'][:10]}{base}")
    lines.append(END)
    return "\n".join(lines)


# 標記要自己獨佔一行（README 說明文字裡用反引號提到標記時，不會被當成區塊）
_BLOCK = re.compile(r"^" + re.escape(START) + r"$.*?^" + re.escape(END) + r"$", re.S | re.M)


def readme_block(text: str) -> str | None:
    m = _BLOCK.search(text)
    return m.group(0) if m else None


def replace_block(text: str, block: str) -> str:
    """取代 README 裡的分數區塊；還沒有標記的話，放在第一段引言（⚠️ 示範專案…）後面。"""
    if _BLOCK.search(text):
        return _BLOCK.sub(lambda _: block, text, count=1)
    lines = text.split("\n")
    i = next((k for k, ln in enumerate(lines) if ln.startswith(">")), 0)
    while i < len(lines) and lines[i].startswith(">"):
        i += 1
    return "\n".join(lines[:i] + ["", block] + lines[i:])

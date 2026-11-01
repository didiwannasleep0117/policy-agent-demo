"""追蹤（agent/tracing.py）與摘要（agent/trace_report.py）的單元測試：不需要模型、不需要 MCP 伺服器。

- 開不開追蹤，ask() 送給模型的每一個請求、回傳的結果都一模一樣（用假的 Ollama 和假的 MCP session 跑）
- 搜尋結果解析、JSONL 格式、span 樹
- 錯題分類：查錯／答錯／應拒答／應回答找不到

uv run python -m pytest -q tests/test_tracing.py
"""
import asyncio
import copy
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PROMPT_FILE", str(ROOT / "agent" / "system_prompt.md"))
sys.path[:0] = [str(ROOT / "agent")]

import run_agent  # noqa: E402
import trace_report  # noqa: E402
import tracing  # noqa: E402

LEAVE, GIFT = "請假管理辦法.docx", "婚喪喜慶補助辦法.pdf"


def search_out(full: str, *snippets: str) -> str:
    """同 src/policy_mcp/server.py 的格式（tests/test_grade.py 也用同一種）。"""
    parts = ["搜尋「婚假」的結果（只回答使用者這次問的問題，不要整理其他文件）：",
             f"1. [[{full}]]（相關度 0.71，命中 5 段）\n《全文》[[{full}]]\n第五條 結婚者給予婚假八日。"]
    parts += [f"{i}. [[{f}]]（相關度 0.5，命中 1 段）\n《片段》（需要完整條文請用 read_specific_document）\n  > 片段內容"
              for i, f in enumerate(snippets, 2)]
    return "\n\n".join(parts)


def test_parse_search():
    hits = tracing.parse_search(search_out(LEAVE, GIFT))
    assert hits == [{"rank": 1, "file": LEAVE, "score": 0.71, "hits": 5, "mode": "full"},
                    {"rank": 2, "file": GIFT, "score": 0.5, "hits": 1, "mode": "snippets", "snippets": ["片段內容"]}]
    assert tracing.parse_search("找不到相關文件。") == []
    assert tracing.tool_attrs("read_specific_document", f"[[{GIFT}]]\n\n全文")["read_file"] == GIFT


# ---- 假的 Ollama 和 MCP：腳本化的回應 ----

def _tc(name, **args):
    return {"function": {"name": name, "arguments": args}}


SCRIPT = [  # 第一次沒查就回答 → DOC_CHECK 退回 → 查 → 回答
    {"role": "assistant", "content": "婚假八日。"},
    {"role": "assistant", "content": "", "tool_calls": [_tc("search_related_policies", query="婚假")]},
    {"role": "assistant", "content": f"結婚者給予婚假八日 [[{LEAVE}]]。"},
]


class FakeHTTP:
    def __init__(self, log):
        self.log, self.i = log, 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self.log.append(copy.deepcopy(json))  # 送出當下的內容（msgs 之後會被 append）
        msg = copy.deepcopy(SCRIPT[self.i])
        self.i += 1
        data = {"model": "qwen3:8b", "message": msg, "prompt_eval_count": 10, "eval_count": 5, "total_duration": 2_000_000}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)


class FakeSession:
    async def list_tools(self):
        t = SimpleNamespace(name="search_related_policies", description="搜尋", inputSchema={"type": "object"})
        return SimpleNamespace(tools=[t])

    async def call_tool(self, name, args):
        return SimpleNamespace(content=[SimpleNamespace(text=search_out(LEAVE, GIFT))])


def _run(monkeypatch, tracer):
    log = []
    monkeypatch.setattr(run_agent.httpx, "AsyncClient", lambda **kw: FakeHTTP(log))
    monkeypatch.setattr(run_agent, "TRACE", tracer)

    async def go():
        tracer.start_question(0, {"q": "婚假怎麼請？", "expect_file": LEAVE})
        with tracer.span("ask", "turn", question="婚假怎麼請？"):
            out = await run_agent.ask(FakeSession(), "婚假怎麼請？", verbose=False)
        ok, why = run_agent.grade({"q": "婚假怎麼請？", "expect_file": LEAVE, "must_contain": [["八日"]]}, out)
        tracer.end_question(ok=ok, why=why, answer=out["answer"])
        return out
    return asyncio.run(go()), log


def test_tracing_does_not_change_behavior(monkeypatch, tmp_path):
    off, log_off = _run(monkeypatch, tracing.Tracer())
    tr = tracing.Tracer(True, tmp_path / "t.jsonl")
    on, log_on = _run(monkeypatch, tr)
    assert on == off and log_on == log_off  # 回傳值、送給模型的每一個請求都一樣
    assert off["nudged"] and off["answer"].endswith("。")


def test_trace_records_steps(monkeypatch, tmp_path):
    tr = tracing.Tracer(True, tmp_path / "t.jsonl")
    _run(monkeypatch, tr)
    (rec,) = [json.loads(x) for x in (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rec["type"] == "question" and rec["ok"] and rec["total_ms"] >= 0
    names = [s["name"] for s in rec["spans"]]
    assert names == ["ask", "mcp.list_tools", "llm.chat", "check.DOC_CHECK", "llm.chat",
                     "tool.search_related_policies", "llm.chat"]
    by = {s["span_id"]: s for s in rec["spans"]}
    chk = next(s for s in rec["spans"] if s["name"] == "check.DOC_CHECK")
    search = next(s for s in rec["spans"] if s["kind"] == "tool" and s["name"] != "mcp.list_tools")
    assert search["parent_id"] == chk["span_id"] and by[chk["parent_id"]]["name"] == "ask"  # 重答的步驟掛在檢查底下
    assert chk["attrs"]["retry"] is True
    assert search["attrs"]["retrieved"][0]["file"] == LEAVE and search["attrs"]["by"] == "model"
    llm = next(s for s in rec["spans"] if s["kind"] == "llm")
    assert llm["attrs"]["input_tokens"] == 10 and llm["attrs"]["ollama_total_ms"] == 2.0
    assert trace_report.classify(rec)["label"] == "pass"


def test_disabled_tracer_is_noop(tmp_path):
    tr = tracing.Tracer()
    tr.header(model="x")
    tr.start_question(0, {"q": "x"})
    with tr.span("a", "tool") as a:
        a["k"] = 1
    tr.event("check.X")
    tr.end_question(ok=True)
    assert not tr.enabled and tr.path is None


def test_span_records_error_and_reraises(tmp_path):
    tr = tracing.Tracer(True, tmp_path / "t.jsonl")
    tr.start_question(0, {"q": "x"})
    with pytest.raises(ValueError):
        with tr.span("tool.x", "tool"):
            raise ValueError("boom")
    tr.end_question(ok=False)
    rec = json.loads((tmp_path / "t.jsonl").read_text(encoding="utf-8"))
    assert rec["spans"][0]["attrs"]["error"] == "ValueError: boom"


# ---- 錯題分類 ----

def _q(ok, spans, why="", **kw):
    return {"type": "question", "idx": 0, "q": "題目", "ok": ok, "why": why, "kind": "answer", "spans": spans, **kw}


def _turn(name="ask", sid="t"):
    return {"span_id": sid, "parent_id": None, "name": name, "kind": "turn", "duration_ms": 10, "attrs": {}}


def _search(parent, *files, by="model", sid="s"):
    hits = [{"rank": i, "file": f, "score": 0.6, "hits": 1, "mode": "full" if i == 1 else "snippets"}
            for i, f in enumerate(files, 1)]
    return {"span_id": sid, "parent_id": parent, "name": "tool.search_related_policies", "kind": "tool",
            "duration_ms": 5, "attrs": {"tool": "search_related_policies", "by": by, "retrieved": hits}}


def test_classify_retrieval_miss():
    c = trace_report.classify(_q(False, [_turn(), _search("t", GIFT)], expect_file=LEAVE))
    assert c["label"] == "retrieval_miss" and GIFT in c["detail"]
    c = trace_report.classify(_q(False, [_turn()], expect_file=LEAVE))
    assert c["label"] == "retrieval_miss" and "沒有呼叫任何查文件的工具" in c["detail"]


def test_classify_retrieval_miss_only_in_previous_turn():
    spans = [_turn("ask.before", "b"), _search("b", LEAVE, sid="s1"), _turn()]
    c = trace_report.classify(_q(False, spans, expect_file=LEAVE))
    assert c["label"] == "retrieval_miss" and "前一題" in c["detail"]


def test_classify_generation_error():
    why = "看過全文=True 引用正確=True 內容正確=False"
    c = trace_report.classify(_q(False, [_turn(), _search("t", GIFT, LEAVE)], why, expect_file=LEAVE))
    assert c["label"] == "generation_error" and "第 2 名" in c["detail"] and "內容" in c["detail"]


def test_classify_corpus_reads_do_not_count():
    s = _search("t", LEAVE, by="corpus")
    assert trace_report.classify(_q(False, [_turn(), s], expect_file=LEAVE))["label"] == "retrieval_miss"


def test_classify_refuse_and_not_found_and_missing_expect():
    assert trace_report.classify(_q(False, [_turn(), _search("t", LEAVE)], kind="refuse"))["label"] == "should_refuse"
    c = trace_report.classify(_q(False, [_turn(), _search("t", LEAVE)], kind="not_found"))
    assert c["label"] == "not_found_error" and "分不出" in c["detail"]
    assert trace_report.classify(_q(False, [_turn()]))["label"] == "unknown"
    # 追蹤檔沒記 expect_file 時，可以從題庫補
    assert trace_report.classify(_q(False, [_turn()]), {"expect_file": LEAVE})["label"] == "retrieval_miss"


def test_report_and_otlp(monkeypatch, tmp_path):
    tr = tracing.Tracer(True, tmp_path / "t.jsonl")
    _run(monkeypatch, tr)
    header, qs = trace_report.load(tmp_path / "t.jsonl")
    text = trace_report.report(header, qs, show_all=True)
    assert "通過 1/1" in text and "沒有錯題" in text and "llm.chat" in text
    otlp = trace_report.to_otlp(header, qs)
    spans = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == len(qs[0]["spans"]) + 1 and all(s["traceId"] == qs[0]["trace_id"] for s in spans)


def test_classify_only_calculator_is_retrieval_miss():
    calc = {"span_id": "c", "parent_id": "t", "name": "tool.calculate", "kind": "tool", "duration_ms": 1,
            "attrs": {"tool": "calculate", "by": "model"}}
    c = trace_report.classify(_q(False, [_turn(), calc], expect_file=LEAVE))
    assert c["label"] == "retrieval_miss" and "只用了 calculate" in c["detail"]


def test_example_trace_is_clean_and_readable():
    """traces/example.jsonl 是 commit 進 repo 的範例：讀得進來、沒有本機絕對路徑。"""
    path = ROOT / "traces" / "example.jsonl"
    text = path.read_text(encoding="utf-8")
    assert not any(s in text for s in (":\\\\", ":/", "/Users/", "/home/"))
    header, qs = trace_report.load(path)
    assert header["type"] == "run" and qs
    labels = {trace_report.classify(q)["label"] for q in qs}
    assert {"retrieval_miss", "generation_error"} <= labels

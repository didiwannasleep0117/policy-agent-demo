"""讀追蹤檔（traces/<run-id>.jsonl），印出：錯題是「查錯」還是「答錯」、每一步的耗時。

用法：
  uv run python agent/trace_report.py traces/<run-id>.jsonl                 錯題分類＋耗時摘要
  uv run python agent/trace_report.py traces/<run-id>.jsonl --all           每一題都列出來（含答對的）
  uv run python agent/trace_report.py traces/<run-id>.jsonl --questions eval/tune.yaml
                                                     追蹤檔沒記到預期文件時（舊檔），從題庫補上
  uv run python agent/trace_report.py traces/<run-id>.jsonl --json          機器可讀的分類結果
  uv run python agent/trace_report.py traces/<run-id>.jsonl --otlp out.json 轉成 OTLP JSON（可匯入支援 OTLP 的工具）

錯題分類（只看最後一問；追問題的前一題另外註明）：
  retrieval_miss    查錯：題目有預期文件（expect_file），但搜尋結果和讀過的文件裡都沒有它（含完全沒查文件）
  generation_error  答錯：預期文件有被檢索到，但答案錯（只看片段沒讀全文、沒引用、內容不對）
  should_refuse     應拒答的題目，卻查了文件或回答了
  not_found_error   應回答找不到的題目沒答好。題庫對這種題沒有預期文件，所以分不出查錯答錯，只列出查了什麼
程式自己讀整個文件庫（BENEFIT_CHECK，by=corpus）不算檢索到。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

DOC_TOOLS = ("search_related_policies", "read_specific_document", "list_all_filenames")
LABELS = {"retrieval_miss": "查錯", "generation_error": "答錯", "should_refuse": "應拒答",
          "not_found_error": "應回答找不到", "unknown": "無法判斷"}


def load(path: str | Path) -> tuple[dict, list[dict]]:
    header, qs = {}, []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("type") == "run":
            header = rec
        elif rec.get("type") == "question":
            qs.append(rec)
    return header, qs


def _subtree(spans: list[dict], root_name: str) -> list[dict]:
    """名稱為 root_name 的 turn span 底下的所有 span（不含它自己）。"""
    kids = defaultdict(list)
    for s in spans:
        kids[s.get("parent_id")].append(s)
    out, todo = [], [s["span_id"] for s in spans if s["name"] == root_name and s["kind"] == "turn"]
    while todo:
        for c in kids.get(todo.pop(), []):
            out.append(c)
            todo.append(c["span_id"])
    return out


def retrieval(spans: list[dict]) -> dict:
    """一組 span 裡查了什麼：搜尋次數、每份文件最好的名次與相關度、讀過哪些全文。"""
    searches, files, read = 0, {}, []
    for s in spans:
        a = s.get("attrs", {})
        if s["kind"] != "tool" or a.get("by") == "corpus":
            continue
        if a.get("tool", "").endswith("search_related_policies"):
            searches += 1
            for h in a.get("retrieved", []):
                f = files.setdefault(h["file"], {"rank": h["rank"], "score": h["score"], "full": False})
                f["rank"], f["score"] = min(f["rank"], h["rank"]), max(f["score"], h["score"])
                f["full"] |= h["mode"] == "full"
        elif a.get("tool", "").endswith("read_specific_document") and a.get("read_file"):
            read.append(a["read_file"])
    other = [s["attrs"]["tool"] for s in spans if s["kind"] == "tool" and s.get("attrs", {}).get("tool")
             and not s["attrs"]["tool"].endswith(DOC_TOOLS)]
    doc_calls = sum(1 for s in spans if s["kind"] == "tool" and s.get("attrs", {}).get("by") != "corpus"
                    and s.get("attrs", {}).get("tool", "").endswith(DOC_TOOLS))
    return {"searches": searches, "doc_calls": doc_calls, "files": files, "read": read, "other_tools": other}


def _top(r: dict, n: int = 3) -> str:
    fs = sorted(r["files"].items(), key=lambda kv: (kv[1]["rank"], -kv[1]["score"]))[:n]
    return "、".join(f"{f}（第 {v['rank']} 名 {v['score']}）" for f, v in fs) or "（沒有）"


def classify(q: dict, item: dict | None = None) -> dict:
    """一題的分類。q：追蹤檔的一行；item：題庫那一題（追蹤檔沒記 expect_file／kind 時才用）。"""
    item = item or {}
    expect = q.get("expect_file") or item.get("expect_file")
    kind = q.get("kind") or ("refuse" if item.get("refuse") else "not_found" if item.get("not_found") else "answer")
    spans = q.get("spans", [])
    final, before = retrieval(_subtree(spans, "ask")), retrieval(_subtree(spans, "ask.before"))
    res = {"idx": q.get("idx"), "q": q.get("q"), "ok": q.get("ok"), "kind": kind, "expect_file": expect,
           "searches": final["searches"], "doc_calls": final["doc_calls"], "retrieved": _top(final)}
    if q.get("ok") is None:
        return {**res, "label": "unknown", "detail": "追蹤檔沒有評分結果（不是 --eval 跑的）"}
    if q["ok"]:
        return {**res, "label": "pass", "detail": ""}
    if kind == "refuse":
        return {**res, "label": "should_refuse", "detail": f"應直接拒答，卻呼叫了 {final['doc_calls']} 次查文件工具"
                if final["doc_calls"] else "沒查工具，但答案不是拒答"}
    if kind == "not_found":
        d = (f"題庫沒有預期文件，分不出查錯或答錯；查了 {final['searches']} 次，拿到 {_top(final)}"
             if final["doc_calls"] else "沒查任何文件")
        return {**res, "label": "not_found_error", "detail": d}
    if not expect:
        return {**res, "label": "unknown", "detail": "題庫沒有 expect_file，無法判斷是不是查錯"}
    hit = final["files"].get(expect)
    got = hit is not None or expect in final["read"]
    if not got:
        if not final["doc_calls"]:
            d = "最後一問沒有呼叫任何查文件的工具" + (
                f"（只用了 {'、'.join(sorted(set(final['other_tools'])))}）" if final["other_tools"] else "")
        else:
            d = f"查了 {final['searches']} 次，預期文件 {expect} 沒出現；拿到的是 {_top(final)}"
        if expect in before["files"] or expect in before["read"]:
            d += "（前一題有查到它，這一問沒再查）"
        return {**res, "label": "retrieval_miss", "detail": d}
    where = (f"第 {hit['rank']} 名，相關度 {hit['score']}" + ("，附全文" if hit["full"] else "，只有片段")
             if hit else "用 read_specific_document 讀到")
    why = q.get("why", "")
    parts = []
    if "看過全文=False" in why:
        parts.append("只看到片段、沒讀全文")
    if "引用正確=False" in why:
        parts.append("沒有引用預期文件")
    if "內容正確=False" in why:
        parts.append("答案內容（關鍵字）不對")
    return {**res, "label": "generation_error", "detail": f"預期文件有查到（{where}），但" + ("、".join(parts) or why)}


def self_times(spans: list[dict]) -> dict[str, float]:
    """每個 span 自己花的時間（扣掉子 span），加起來約等於整題時間。"""
    child = defaultdict(float)
    for s in spans:
        if s.get("parent_id"):
            child[s["parent_id"]] += s.get("duration_ms", 0)
    return {s["span_id"]: max(0.0, s.get("duration_ms", 0) - child[s["span_id"]]) for s in spans}


def step_name(s: dict) -> str:
    a = s.get("attrs", {})
    if s["kind"] == "llm":
        return "llm.chat（思考）" if a.get("think") else "llm.chat"
    if s["kind"] == "tool" and a.get("by") in ("harness", "corpus"):
        return f"{s['name']}（{a['by']}）"
    if s["kind"] == "turn":
        return f"{s['name']}（自身）"  # turn 的 self 時間＝不在任何子步驟裡的程式時間（檢查、組訊息）
    return s["name"]


def latency(qs: list[dict]) -> list[dict]:
    rows = defaultdict(lambda: {"count": 0, "self_ms": 0.0, "durs": []})
    for q in qs:
        st = self_times(q.get("spans", []))
        for s in q.get("spans", []):
            if s["kind"] == "check":  # 檢查本身另外統計（check_stats）；底下的 llm／tool 照常算
                continue
            r = rows[step_name(s)]
            r["count"] += 1
            r["self_ms"] += st[s["span_id"]]
            r["durs"].append(s.get("duration_ms", 0))
    total = sum(q.get("total_ms", 0) for q in qs) or 1
    out = []
    for name, r in rows.items():
        d = sorted(r["durs"])
        out.append({"step": name, "count": r["count"], "self_s": round(r["self_ms"] / 1000, 1),
                    "share": round(100 * r["self_ms"] / total, 1), "mean_ms": round(sum(d) / len(d)),
                    "p50_ms": round(d[len(d) // 2]), "max_ms": round(d[-1])})
    return sorted(out, key=lambda r: -r["self_s"])


def check_stats(qs: list[dict]) -> list[dict]:
    """每個檢查：觸發幾次、幾題、退回重答幾次、重答花的時間（span 的總長，含底下的 llm／tool）。"""
    rows = defaultdict(lambda: {"count": 0, "qs": set(), "retries": 0, "ms": 0.0})
    for q in qs:
        for s in q.get("spans", []):
            if s["kind"] != "check" or not (s["attrs"].get("retry") or s["attrs"].get("action")):
                continue
            r = rows[s["name"].removeprefix("check.")]
            r["count"] += 1
            r["qs"].add(q.get("idx"))
            r["retries"] += bool(s["attrs"].get("retry"))
            r["ms"] += s.get("duration_ms", 0)
    return sorted(({"check": k, "count": v["count"], "questions": len(v["qs"]), "retries": v["retries"],
                    "retry_s": round(v["ms"] / 1000, 1)} for k, v in rows.items()), key=lambda r: -r["retry_s"])


def _pad(s: str, n: int) -> str:
    """中文字算兩格寬，表格才對得齊。"""
    import unicodedata
    w = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)
    return s + " " * max(1, n - w)


def checks_fired(q: dict) -> list[str]:
    return [s["name"].removeprefix("check.") for s in q.get("spans", []) if s["kind"] == "check"
            and (s["attrs"].get("retry") or s["attrs"].get("action"))]


def retries(q: dict) -> int:
    return sum(1 for s in q.get("spans", []) if s["kind"] == "check" and s["attrs"].get("retry"))


def report(header: dict, qs: list[dict], items: dict | None = None, show_all: bool = False) -> str:
    items = items or {}
    L = []
    graded = [q for q in qs if q.get("ok") is not None]
    passed = sum(1 for q in graded if q["ok"])
    total_s = sum(q.get("total_ms", 0) for q in qs) / 1000
    L.append(f"追蹤：{header.get('run_id', '?')}｜模型 {header.get('model')}｜Ollama {header.get('ollama_version')}"
             f"｜commit {header.get('git_commit')}｜題庫 {header.get('questions')}")
    L.append(f"通過 {passed}/{len(graded)}，共 {total_s:.1f} 秒，平均 {total_s / max(len(qs), 1):.1f} 秒／題")
    cls = [classify(q, items.get(q.get("q"))) for q in qs]
    # 檢索：有預期文件的題目，最後一問有沒有查到它
    exp = [(q, c) for q, c in zip(qs, cls) if c["expect_file"]]
    if exp:
        got = sum(1 for q, c in exp if c["expect_file"] in retrieval(_subtree(q["spans"], "ask"))["files"]
                  or c["expect_file"] in retrieval(_subtree(q["spans"], "ask"))["read"])
        top1 = sum(1 for q, c in exp if retrieval(_subtree(q["spans"], "ask"))["files"]
                   .get(c["expect_file"], {}).get("rank") == 1)
        L.append(f"檢索：有預期文件的 {len(exp)} 題，最後一問查到預期文件 {got} 題，其中排第 1 名 {top1} 題")
    else:
        L.append("檢索：這份題庫的題目都沒有 expect_file（或追蹤檔沒記到），無法判斷查錯；可加 --questions 指定題庫")
    bad = [c for c in cls if c["label"] != "pass" and c["ok"] is not None]
    if len(graded) < len(qs):
        L.append(f"沒有評分結果的題目 {len(qs) - len(graded)} 題（不是 --eval 跑的），不分類")
    bad_ids = {id(c) for c in bad}
    counts = defaultdict(int)
    for c in bad:
        counts[c["label"]] += 1
    L.append("\n錯題分類：" + ("、".join(f"{k}（{LABELS[k]}）{v} 題" for k, v in counts.items()) if bad else "沒有錯題"))
    for q, c in zip(qs, cls):
        if id(c) not in bad_ids and not show_all:
            continue
        mark = "❌" if id(c) in bad_ids else "✅" if c["ok"] else "·"
        L.append(f"\n{mark} [{c['idx']}] {c['q']}" + (f"（追問，前一題：{'／'.join(q['before'])}）" if q.get("before") else ""))
        if id(c) in bad_ids:
            L.append(f"   分類：{c['label']}（{LABELS[c['label']]}）— {c['detail']}")
        L.append(f"   預期文件：{c['expect_file'] or '（無）'}｜查文件工具 {c['doc_calls']} 次｜拿到：{c['retrieved']}")
        fired = checks_fired(q)
        L.append(f"   觸發的檢查：{'、'.join(fired) or '（無）'}｜退回重答 {retries(q)} 次｜耗時 {q.get('total_ms', 0) / 1000:.1f} 秒")
        st = self_times(q.get("spans", []))
        slow = sorted(q.get("spans", []), key=lambda s: -st[s["span_id"]])[:3]
        L.append("   最慢的步驟：" + "、".join(f"{step_name(s)} {st[s['span_id']] / 1000:.1f}s" for s in slow))
        if q.get("why"):
            L.append(f"   評分：{q['why']}")
    L.append("\n每一步的耗時（self 秒＝扣掉子步驟後自己花的時間，加起來約等於總時間；佔比＝佔全部題目總時間）：")
    L.append("   " + _pad("步驟", 40) + "次數  self秒   佔比   平均ms   中位ms   最大ms")
    for r in latency(qs):
        L.append("   " + _pad(r["step"], 40) + f"{r['count']:>4}{r['self_s']:>7}{r['share']:>6}%"
                 f"{r['mean_ms']:>9}{r['p50_ms']:>9}{r['max_ms']:>9}")
    cs = check_stats(qs)
    if cs:
        L.append(f"\n觸發的程式檢查（重答秒數＝退回模型重答花的時間，已含在上表的 llm／tool 裡）：退回重答共 {sum(r['retries'] for r in cs)} 次")
        L.append("   " + _pad("檢查", 26) + "次數  題數  重答  重答秒數")
        for r in cs:
            L.append("   " + _pad(r["check"], 26) + f"{r['count']:>4}{r['questions']:>6}{r['retries']:>6}{r['retry_s']:>10}")
    return "\n".join(L)


def to_otlp(header: dict, qs: list[dict]) -> dict:
    """轉成 OTLP/JSON（resourceSpans）。每題一個 trace，題目本身是根 span。"""
    def attrs(d: dict) -> list[dict]:
        out = []
        for k, v in d.items():
            if v is None:
                continue
            if isinstance(v, bool):
                val = {"boolValue": v}
            elif isinstance(v, int):
                val = {"intValue": str(v)}
            elif isinstance(v, float):
                val = {"doubleValue": v}
            elif isinstance(v, str):
                val = {"stringValue": v}
            else:
                val = {"stringValue": json.dumps(v, ensure_ascii=False)}
            out.append({"key": k, "value": val})
        return out

    spans = []
    for q in qs:
        root = q["trace_id"][:16]
        spans.append({"traceId": q["trace_id"], "spanId": root, "name": "question", "kind": 1,
                      "startTimeUnixNano": str(q["start_ns"]), "endTimeUnixNano": str(q["end_ns"]),
                      "attributes": attrs({"question": q.get("q"), "eval.ok": q.get("ok"), "eval.why": q.get("why"),
                                           "eval.expect_file": q.get("expect_file"), "gen_ai.request.model": header.get("model")})})
        for s in q.get("spans", []):
            a = dict(s.get("attrs", {}))
            if s["kind"] == "llm":
                a["gen_ai.request.model"] = a.pop("model", None)
                a["gen_ai.usage.input_tokens"] = a.pop("input_tokens", None)
                a["gen_ai.usage.output_tokens"] = a.pop("output_tokens", None)
            spans.append({"traceId": q["trace_id"], "spanId": s["span_id"], "parentSpanId": s.get("parent_id") or root,
                          "name": s["name"], "kind": 3 if s["kind"] in ("llm", "tool") else 1,
                          "startTimeUnixNano": str(s["start_ns"]), "endTimeUnixNano": str(s["end_ns"]),
                          "attributes": attrs({"step.kind": s["kind"], **a})})
    return {"resourceSpans": [{"resource": {"attributes": attrs({"service.name": "policy-agent-demo",
                                                                 "run.id": header.get("run_id")})},
                               "scopeSpans": [{"scope": {"name": "agent/tracing.py"}, "spans": spans}]}]}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="追蹤檔摘要：錯題是查錯還是答錯、每一步耗時")
    ap.add_argument("trace")
    ap.add_argument("--questions", help="題庫 yaml（追蹤檔沒記到 expect_file 時補上）")
    ap.add_argument("--all", action="store_true", help="答對的題目也列出來")
    ap.add_argument("--json", action="store_true", help="輸出每題的分類（JSON）")
    ap.add_argument("--otlp", help="另存成 OTLP JSON")
    a = ap.parse_args()
    header, qs = load(a.trace)
    items = {}
    if a.questions:
        import yaml
        items = {it["q"]: it for it in yaml.safe_load(Path(a.questions).read_text(encoding="utf-8"))}
    if a.otlp:
        Path(a.otlp).write_text(json.dumps(to_otlp(header, qs), ensure_ascii=False), encoding="utf-8")
        print(f"已寫入 {a.otlp}", file=sys.stderr)
    if a.json:
        print(json.dumps([classify(q, items.get(q.get("q"))) for q in qs], ensure_ascii=False, indent=1))
    else:
        print(report(header, qs, items, a.all))


if __name__ == "__main__":
    main()

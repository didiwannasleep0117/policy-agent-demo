"""評分程式的單元測試（不需要模型、不需要 MCP 伺服器）：直接造「模型的回答」和「工具輸出」來評分。

- agent/run_agent.py 的 grade()：Python 版驗收（run_agent.py --eval、harness/loop.py probe、scripts/run_eval.py）
- eval/run_n8n_eval.py 的 grade()：n8n 版驗收（看不到工具呼叫，只看引用和內容）
- eval/run_router_eval.py 的 answered()：分流評測「答覆正常」的判定

uv run python -m pytest -q tests/test_grade.py
"""
import os
import sys
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PROMPT_FILE", str(ROOT / "agent" / "system_prompt.md"))
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "eval")]

from run_agent import grade  # noqa: E402
from run_n8n_eval import grade as grade_n8n  # noqa: E402
from run_router_eval import answered  # noqa: E402

LEAVE = "請假管理辦法.docx"
GIFT = "婚喪喜慶補助辦法.pdf"
WEDDING = {"q": "婚假怎麼請？", "expect_file": LEAVE, "must_contain": [["八日"], ["8"]]}
GIFT_Q = {"q": "那結婚有禮金嗎？", "before": ["婚假怎麼請？"], "expect_file": GIFT,
          "must_contain": [["六千"], ["6,000"], ["6000"]]}
BONUS = {"q": "考績 A 年終幾個月？", "expect_file": "考績管理辦法.docx", "must_contain": [["1.5", "2.25"]]}
REFUSE = {"q": "今天台北天氣如何？", "refuse": True}
NOT_FOUND = {"q": "公司有提供停車位嗎？", "not_found": True}
SEARCH = [("search_related_policies", {"query": "婚假"})]
NOT_FOUND_ANS = "抱歉，目前的規章文件裡找不到這個問題的資訊，建議直接聯繫人資部。"


def search_out(full: str, *snippets: str) -> str:
    """模擬 search_related_policies：第 1 名附全文，其餘只有片段（同 src/policy_mcp/server.py 的格式）。"""
    parts = [f"搜尋「婚假」的結果（只回答使用者這次問的問題，不要整理其他文件）：",
             f"1. [[{full}]]（相關度 0.71，命中 5 段）\n《全文》[[{full}]]\n第五條 結婚者給予婚假八日。"]
    parts += [f"{i}. [[{f}]]（相關度 0.5，命中 1 段）\n《片段》（需要完整條文請用 read_specific_document）\n  > 片段"
              for i, f in enumerate(snippets, 2)]
    return "\n\n".join(parts)


def read_out(f: str) -> str:
    """模擬 read_specific_document 的輸出。"""
    return f"[[{f}]]\n\n全文內容"


def g(item, answer, calls=SEARCH, outputs=None):
    return grade(item, {"answer": answer, "calls": calls, "outputs": outputs if outputs is not None else []})


# ---- Python 版：一般題（看過全文 × 引用正確 × 內容正確，三個都要） ----

def test_full_text_from_search_cited_and_correct():
    ok, why = g(WEDDING, f"結婚者給予婚假八日，工資照給 [[{LEAVE}]]。", outputs=[search_out(LEAVE)])
    assert ok and why.startswith("看過全文=True 引用正確=True 內容正確=True")


def test_full_text_from_read_specific_document():
    out = [search_out("員工旅遊補助辦法.pdf", GIFT), read_out(GIFT)]
    assert g(GIFT_Q, f"結婚可申請禮金新台幣 6,000 元 [[{GIFT}]]", outputs=out)[0]


@pytest.mark.parametrize("answer", [
    f"婚假 8 日 [[{LEAVE}]]",           # 任一組關鍵字都可以（八日 或 8）
    f"婚假共八\n日 [[{LEAVE}]]",         # 空白、換行不影響比對
])
def test_alternative_keyword_groups_and_whitespace(answer):
    assert g(WEDDING, answer, outputs=[search_out(LEAVE)])[0]


def test_every_keyword_in_group_required():
    out = [read_out(BONUS["expect_file"])]
    assert g(BONUS, f"1.5 個月 × 1.5 倍 = 2.25 個月 [[{BONUS['expect_file']}]]", outputs=out)[0]
    ok, why = g(BONUS, f"年終 1.5 個月 [[{BONUS['expect_file']}]]", outputs=out)  # 沒乘倍數（v4 的錯）
    assert not ok and "內容正確=False" in why


def test_only_snippet_seen_graded_wrong():
    # 正確文件只出現在搜尋結果的片段（第 2 名），沒讀全文 → 可能漏掉但書
    ok, why = g(GIFT_Q, f"禮金 6,000 元 [[{GIFT}]]", outputs=[search_out(LEAVE, GIFT)])
    assert not ok and "看過全文=False" in why


def test_wrong_or_missing_citation_graded_wrong():
    out = [search_out(LEAVE), read_out(GIFT)]
    ok, why = g(WEDDING, f"婚假八日 [[{GIFT}]]", outputs=out)
    assert not ok and "引用正確=False" in why
    assert not g(WEDDING, "婚假八日。", outputs=out)[0]
    assert not g(WEDDING, f"婚假八日 {LEAVE}", outputs=out)[0]  # 沒用 [[檔名]] 格式不算引用


def test_wrong_content_graded_wrong():
    ok, why = g(WEDDING, f"婚假五日 [[{LEAVE}]]", outputs=[search_out(LEAVE)])
    assert not ok and "內容正確=False" in why


def test_nudge_flags_shown_in_reason():
    _, why = grade(WEDDING, {"answer": f"八日 [[{LEAVE}]]", "calls": SEARCH, "outputs": [search_out(LEAVE)],
                             "nudged": True, "calc_nudged": True})
    assert "（被提醒先查）" in why and "（被要求驗算）" in why


# ---- Python 版：應拒答／應回答找不到 ----

def test_refuse():
    refusal = "抱歉，我只能回答公司規章與福利相關的問題。"
    assert g(REFUSE, refusal, calls=[])[0]
    assert not g(REFUSE, refusal, calls=SEARCH)[0]                 # 查了才拒答：不算（無關問題不該查）
    assert not g(REFUSE, "今天台北晴天，28 度。", calls=[])[0]       # 沒拒答
    assert not g(REFUSE, NOT_FOUND_ANS, calls=[])[0]                 # 「找不到」不是拒答


@pytest.mark.parametrize("answer", [
    NOT_FOUND_ANS,
    "規章裡沒有相關規定，請洽人資部。",
    "文件未明確說明停車位，建議詢問財務部。",
    "規章沒有提到停車位，請聯繫人資。",
])
def test_not_found_phrasings_graded_correct(answer):
    assert g(NOT_FOUND, answer)[0]


@pytest.mark.parametrize("answer,calls", [
    (NOT_FOUND_ANS, []),                                   # 沒查就說找不到
    ("規章裡找不到停車位的規定。", SEARCH),                  # 沒請員工找人資／財務
    ("公司提供員工停車位，每月 1,500 元，請洽人資申請。", SEARCH),  # 自己編答案
    ("抱歉，我只能回答公司規章與福利相關的問題。", []),        # 拒答
])
def test_not_found_wrong_answers_graded_wrong(answer, calls):
    assert not g(NOT_FOUND, answer, calls=calls)[0]


# ---- n8n 版 ----

def test_n8n_citation_card_or_brackets():
    assert grade_n8n(WEDDING, f"婚假八日 [[{LEAVE}]]")[0]
    # 回覆節點把 [[檔名]] 換成卡片連結 /docs/<URL 編碼的檔名>
    assert grade_n8n(WEDDING, f"婚假八日\n\n📘 [請假管理辦法](http://localhost:8000/docs/{quote(LEAVE)})")[0]


def test_n8n_wrong_answers():
    ok, why = grade_n8n(WEDDING, "婚假八日。")
    assert not ok and "引用正確=False" in why
    ok, why = grade_n8n(WEDDING, f"婚假五日 [[{LEAVE}]]")
    assert not ok and "內容正確=False" in why
    assert not grade_n8n(GIFT_Q, f"禮金 6,000 元 [[{LEAVE}]]")[0]  # 引用錯文件


def test_n8n_refuse_and_not_found():
    assert grade_n8n(REFUSE, "抱歉，我只能回答公司規章與福利相關的問題。")[0]
    assert not grade_n8n(REFUSE, "今天台北晴天。")[0]
    assert grade_n8n(NOT_FOUND, NOT_FOUND_ANS)[0]
    assert not grade_n8n(NOT_FOUND, "規章裡找不到。")[0]
    assert not grade_n8n(NOT_FOUND, "公司有停車位，請洽人資。")[0]


# ---- 分流評測：「答覆正常」 ----

@pytest.mark.parametrize("route,out,want", [
    ("規章", {"output": "婚假八日\n📘 [請假管理辦法](http://x/docs/a)"}, True),
    ("規章", {"output": NOT_FOUND_ANS}, True),
    ("規章", {"output": "婚假八日。"}, False),
    ("數據", {"action": "sql", "output": "上個月營收 120 萬"}, True),
    ("數據", {"action": "clarify", "output": "請問是哪個 App？"}, True),
    ("數據", {"action": "error", "output": "查詢失敗"}, False),
    ("數據", {"action": "sql", "output": ""}, False),
    ("其他", {"output": "抱歉，我只能回答公司規章與福利相關的問題。"}, True),
    ("其他", {"output": "今天台北晴天。"}, False),
])
def test_router_answered(route, out, want):
    assert answered(route, out) is want


# ---- 題庫本身 ----

SPLIT_FILES = ["questions", "tune", "holdout", "holdout2", "holdout3", "holdout4"]
DOCS = {p.name for p in (ROOT / "data" / "docs").iterdir()}


def perfect(item: dict) -> dict:
    """照題目的標準造一個「完美回答」。"""
    if item.get("refuse"):
        return {"answer": "抱歉，我只能回答公司規章與福利相關的問題。", "calls": [], "outputs": []}
    if item.get("not_found"):
        return {"answer": NOT_FOUND_ANS, "calls": SEARCH, "outputs": []}
    f = item["expect_file"]
    return {"answer": " ".join(item["must_contain"][0]) + f" [[{f}]]", "calls": SEARCH, "outputs": [read_out(f)]}


@pytest.mark.parametrize("split", SPLIT_FILES)
def test_question_bank_valid_and_self_grades(split):
    items = yaml.safe_load((ROOT / "eval" / f"{split}.yaml").read_text(encoding="utf-8"))
    assert items and len({i["q"] for i in items}) == len(items), "題目不能重複（結果檔用題目當 key）"
    for it in items:
        assert isinstance(it.get("before", []), list), it["q"]
        if not (it.get("refuse") or it.get("not_found")):
            assert it["expect_file"] in DOCS, f"{it['q']}：{it['expect_file']} 不在 data/docs/"
            groups = it["must_contain"]
            assert groups and all(isinstance(gr, list) and gr for gr in groups), it["q"]
        assert grade(it, perfect(it))[0], it["q"]
        assert grade_n8n(it, perfect(it)["answer"])[0], it["q"]
        if not it.get("refuse"):  # 什麼都沒查、亂答一通 → 一定算錯
            assert not grade(it, {"answer": "我不知道。", "calls": [], "outputs": []})[0], it["q"]

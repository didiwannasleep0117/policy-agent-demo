"""程式化檢查與計算機的單元測試（不需要模型）：agent/checks.py、run_agent.py 的算式偵測、
src/policy_mcp/calc.py、src/policy_mcp/cn_numbers.py。

uv run python -m pytest -q tests/test_checks.py
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PROMPT_FILE", str(ROOT / "agent" / "system_prompt.md"))
sys.path.insert(0, str(ROOT / "agent"))

import checks  # noqa: E402
import run_agent  # noqa: E402
from policy_mcp.calc import calculate  # noqa: E402
from policy_mcp.cn_numbers import annotate, cn_to_number  # noqa: E402


# ---- 算式重算（ARITH_VERIFY）----

@pytest.mark.parametrize("text", [
    "加班費 200 × 4/3 × 2 = 533.33 元",
    "4 萬 × 1.2 = 48,000 元",
    "40,000 × 1.5 × 1.5 = 90,000 元",
    "1000/3 = 333",                       # 四捨五入誤差內
    "200元 × (1 + 1/3) × 2 = 533 元",
    "婚假共八日 = 8",                      # 沒有運算子，不是算式
])
def test_correct_or_non_equations_pass(text):
    assert checks.wrong_equations(text) == []


@pytest.mark.parametrize("text,stated,correct", [
    ("加班費 200 × 4/3 × 2 = 666.67 元", 666.67, 533.33),  # README 的例子：公式對、乘法算錯
    ("時薪 200 × 1.5 = 350", 350, 300),
])
def test_wrong_equations_caught(text, stated, correct):
    [(_, w, c)] = checks.wrong_equations(text)
    assert (w, c) == (stated, correct)


# ---- 申請動作對照（TERM_CHECK）、引用讀全文（CITE_READ）----

def test_term_in_evidence():
    assert checks.term_in_evidence(["預支", "預借"], "員工出差可 預 借 旅費") == "預借"  # 忽略空白
    assert checks.term_in_evidence(["分期"], "旅費於出差後十日內報銷") is None
    assert checks.term_in_evidence(["支"], "預支") is None  # 一個字太短，不算


def test_unread_citations():
    outs = ["搜尋「x」的結果\n\n1. [[a.pdf]]（相關度 0.6）\n《片段》\n  > ...", "[[b.docx]]\n\n全文"]
    assert checks.unread_citations("見 [[a.pdf]] 與 [[b.docx]]，再看 [[a.pdf]]", outs) == ["a.pdf"]
    assert checks.unread_citations("見 [[c.pdf]]", ["1. [[c.pdf]]（相關度 0.7）\n《全文》[[c.pdf]]\n內容"]) == []


# ---- 驗算觸發：答案裡有「數字 運算子 數字」（README：「婚假共八日」「4,000 元」不會誤判）----

@pytest.mark.parametrize("text,want", [
    ("4 萬元 × 1.2", True), ("200元 × (1 + 1/3)", True), ("40,000 × 1.2", True),
    ("婚假共八日", False), ("4,000 元", False), ("請於 3 日內申請", False),
])
def test_arith_trigger(text, want):
    assert bool(run_agent._ARITH.search(text)) is want


# ---- 計算機工具 ----

@pytest.mark.parametrize("expr,want", [
    ("40,000 × 1.5", "40,000 × 1.5 = 60000"),
    ("round(200*4/3*2, 2)", "round(200*4/3*2, 2) = 533.33"),
    ("35 >= 40", "35 >= 40 → 不成立（False）"),
    ("40 >= 40 且 3 > 1", "40 >= 40 且 3 > 1 → 成立（True）"),
    ("1/0", "錯誤：除以零"),
])
def test_calculate(expr, want):
    assert calculate(expr) == want


@pytest.mark.parametrize("expr", ["__import__('os').system('dir')", "open('x')", "2**1000", "a + 1", "1" * 201])
def test_calculate_rejects_unsafe_or_huge(expr):
    assert calculate(expr).startswith("錯誤")


# ---- 中文數字 ----

def test_cn_numbers():
    assert cn_to_number("一萬二千") == 12000 and cn_to_number("一點五") == 1.5 and cn_to_number("abc") is None
    assert annotate("新台幣四千元，依第二條，每年一次") == "新台幣四千（4,000）元，依第二條，每年一次"


# ---- 第 7 輪：條文公式（FORMULA_CHECK）、親屬名單對照（KIN_LOOKUP）----

BONUS_EV = ("搜尋「年終」的結果\n1. [[考績管理辦法.docx]]（相關度 0.7）\n《全文》[[考績管理辦法.docx]]\n"
            "第四條 年終獎金：以一點五個月薪資為基準，A 等乘以一點五倍、B+ 等乘以一點二倍、B 等乘以一倍、C 等乘以零點五倍。")
OT_EV = ("[[加班管理辦法.pdf]]\n第二條 平日延長工時：前二小時按平日每小時工資加給三分之一，第\n三小時起加給三分之二。\n"
         "第三條 休息日加班：前二小時加給一又三分之一，第三小時起加給一又三分之二。")
LEAVE_EV = ("[[請假管理辦法.docx]]\n第三條 喪假：父母、養父母、繼父母、配偶喪亡者，給予喪假八日；祖父母、子女、配偶之父母喪亡者，"
            "給予喪假六日；曾祖父母、兄弟姊妹、配偶之祖父母喪亡者，給予喪假三日。喪假工資照給，得於百日內分次申請。")
GIFT_EV = ("[[婚喪喜慶補助辦法.pdf]]\n第四條 喪葬慰問：同仁之父母、配偶、子女喪亡者，致贈奠儀新台幣五千元及花圈一對；"
           "祖父母喪亡者致贈奠儀新台幣二千元。")


def test_cn_value_and_money():
    assert checks.cn_value("三分之二") == pytest.approx(2 / 3) and checks.cn_value("一又三分之一") == pytest.approx(4 / 3)
    assert checks.cn_value("零點五") == 0.5 and checks.cn_value("1,080") == 1080
    assert checks.money_in_question("月薪 5 萬 2，考績 A", "月薪") == 52000
    assert checks.money_in_question("我月薪四萬，考績 B+", "月薪") == 40000
    assert checks.money_in_question("時薪 180 元，加班 4 小時", "時薪") == 180


@pytest.mark.parametrize("q,want", [
    ("我的月薪是 5 萬 2，今年考績打 A，年終可以領多少錢？", 117000),
    ("月薪 3 萬 6 的話，考績 B+ 年終會拿到多少？", 64800),
    ("考績拿 A 的話年終大概幾個月？", 2.25),
    ("時薪 180 元，平日下班後加班 4 小時，加班費總共多少？", 1080),
    ("我時薪 200 元，平日下班後加班 3 小時，加班費是多少？", 866.67),
    ("考績拿 A 的人數有限制嗎？", None),                      # 沒有數字可算
    ("每個月加班時數有上限嗎？", None),
    ("休息日加班 3 小時，時薪 200，加班費多少？", None),       # 只處理平日（休息日的算法不檢查）
])
def test_formula_expectation(q, want):
    exp = checks.formula_expectation(q, BONUS_EV + "\n\n" + OT_EV)
    assert (exp is None) if want is None else exp[0] == pytest.approx(want, abs=0.01)


def test_formula_explanation_and_value_match():
    _, why = checks.formula_expectation("時薪 180 元，平日加班 4 小時，加班費總共多少？", OT_EV)
    assert "三分之一" in why and "1,080" in why
    assert checks.has_value("加班費 1,080 元", 1080) and checks.has_value("年終 11.7 萬元", 117000)
    assert not checks.has_value("52,000 × 1.5 = 78,000 元", 117000)   # 第 7 輪 v9c 的錯：漏乘考績倍數
    assert not checks.has_value("總加班費 = 120 + 240 = 360 元", 1080)  # 只算加給


def test_kin_terms_leftmost_longest():
    kin = run_agent.KIN_MAP
    assert checks.kin_terms("如果是我太太呢？", kin) == [("太太", "配偶")]
    assert checks.kin_terms("曾祖母過世呢？", kin) == [("曾祖", "曾祖父母")]           # 不會再算一次「祖母」
    assert len(checks.kin_terms("我先生的祖父母過世呢？", kin)) == 2                  # 兩個稱謂 → 呼叫端跳過不檢查


def test_kin_rule_and_stated_values():
    val, unit, seg, allv = checks.kin_rule(LEAVE_EV, "配偶", "喪假")
    assert (val, unit, allv) == (8, "日", {8, 6, 3}) and seg.startswith("父母、養父母")
    assert checks.kin_rule(LEAVE_EV, "配偶之父母", "喪假")[0] == 6                  # 不會被「配偶」那一列搶走
    assert checks.kin_rule(GIFT_EV, "祖父母", "奠儀")[:2] == (2000, "元")
    assert checks.kin_rule(GIFT_EV, "配偶之父母", "奠儀") is None                    # 這一條沒列 → 不處理
    # 第 7 輪 v9c 的錯：引用條文寫到八日，結論又寫十四日（條文沒有的數字）
    assert checks.stated_values("配偶喪亡者給予喪假八日；但配偶喪亡為十四日，得於百日內分次申請", "日") == {8, 14}

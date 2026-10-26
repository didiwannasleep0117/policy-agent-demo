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

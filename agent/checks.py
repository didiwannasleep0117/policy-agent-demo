"""程式化的答案檢查（不靠模型自律）。

- wrong_equations：答案裡寫的「算式 = 數字」用安全計算機重算，抓出算錯的
- term_in_evidence：是非題問的動作／項目（含同義寫法）有沒有出現在查到的條文裡
- unread_citations：答案引用了某份文件，但只看過片段沒看過全文
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from policy_mcp.calc import _ev  # noqa: E402

# 「算式 ＝ 數字」：算式從數字或括號開始，中間可夾單位
_EQ = re.compile(
    r"(?<![\d.])([\d(（][\d.,\s×xX*＊÷/+\-－（）()元小時個月日天倍萬千]*?[\d)）元時月日天倍])"
    r"\s*[=＝]\s*(?:新台幣\s*)?([\d,]*\d(?:\.\d+)?)")
_OPS = re.compile(r"[×xX*＊÷/+\-－]")


def _to_expr(s: str) -> str:
    s = re.sub(r"(\d)\s*萬", r"(\1*10000)", s)
    s = re.sub(r"(\d)\s*千", r"(\1*1000)", s)
    s = re.sub(r"元|小時|個月|日|天|倍", "", s)
    s = s.translate(str.maketrans({"×": "*", "x": "*", "X": "*", "＊": "*", "÷": "/", "－": "-", "（": "(", "）": ")"}))
    s = re.sub(r"(?<=\d),(?=\d{3})", "", s)
    return s.strip()


def wrong_equations(text: str) -> list[tuple[str, float, float]]:
    """回傳 [(原算式, 寫的值, 正確值)]，只回報差距大於四捨五入誤差的。"""
    bad = []
    for m in _EQ.finditer(text):
        raw, stated = m.group(1).strip(), float(m.group(2).replace(",", ""))
        if not _OPS.search(raw.replace("-", "", 0)):
            continue
        try:
            val = _ev(ast.parse(_to_expr(raw), mode="eval"))
        except Exception:
            continue
        if isinstance(val, bool):
            continue
        if abs(val - stated) > max(0.6, abs(val) * 0.005):
            bad.append((raw, stated, round(val, 2)))
    return bad


def term_in_evidence(terms: list[str], evidence: str) -> str | None:
    """回傳在條文中找到的那個寫法；都找不到回傳 None。"""
    ev = re.sub(r"\s", "", evidence)
    for t in terms:
        t = re.sub(r"\s", "", t or "")
        if len(t) >= 2 and t in ev:
            return t
    return None


_FULL = lambda f, o: f"[[{f}]]\n" in o and ("《全文》" in o or o.startswith("[["))  # noqa: E731


def unread_citations(answer: str, outputs: list[str]) -> list[str]:
    cited = list(dict.fromkeys(re.findall(r"\[\[([^\]]+)\]\]", answer)))
    return [f for f in cited if not any(_FULL(f, o) for o in outputs)]

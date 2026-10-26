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


# ---- 第 7 輪：公式／名單由程式從條文解析（不靠模型自己列公式、自己對名單）----

from fractions import Fraction  # noqa: E402

from policy_mcp.cn_numbers import cn_to_number  # noqa: E402

_CN = "零〇一二兩三四五六七八九十百千萬點"
_NUMTXT = rf"[{_CN}\d.,]+"


def cn_value(s: str) -> float | None:
    """阿拉伯數字、中文數字（一點五、五千）、分數（三分之二、一又三分之一）→ 數值。"""
    s = (s or "").strip().replace(",", "")
    m = re.fullmatch(rf"(?:([{_CN}]+)又)?([{_CN}]+)分之([{_CN}]+)", s)
    if m:
        whole = cn_to_number(m.group(1)) if m.group(1) else 0
        den, num = cn_to_number(m.group(2)), cn_to_number(m.group(3))
        return None if not den or num is None or whole is None else float(whole + Fraction(num, den))
    try:
        return float(s)
    except ValueError:
        v = cn_to_number(s)
        return None if v is None else float(v)


def _flat(evidence: str) -> str:
    # 搜尋結果第一行會重複使用者的問題，先拿掉；條文的空白、換行（PDF 斷行）也拿掉
    return re.sub(r"\s", "", re.sub(r"^搜尋「.*?」的結果.*$", "", evidence, flags=re.M))


def money_in_question(q: str, label: str) -> float | None:
    """「月薪 5 萬 2」→ 52000、「月薪四萬」→ 40000、「時薪 180 元」→ 180。"""
    m = re.search(rf"{label}\s*(?:是|為|約)?\s*([\d.]+|[{_CN}]+)\s*(萬)?\s*(\d|[一二三四五六七八九])?(?![\d.])", q)
    if not m:
        return None
    v = cn_value(m.group(1))
    if v is None:
        return None
    if m.group(2):
        v = v * 10000 + (cn_value(m.group(3)) * 1000 if m.group(3) else 0)
    return v


def bonus_rule(evidence: str) -> tuple[float, dict[str, float], str] | None:
    """「年終獎金：以一點五個月薪資為基準，A 等乘以一點五倍、…」→ (1.5, {"A": 1.5, …}, 原句)。"""
    m = re.search(rf"年終獎金[：:]?以({_NUMTXT})個月(?:薪資|月薪)?為基準[，,](.+?)[。；]", _flat(evidence))
    if not m:
        return None
    mults = {g.replace("＋", "+"): cn_value(v) for g, v in re.findall(rf"([A-Z][+＋]?)等?乘以({_NUMTXT})倍", m.group(2))}
    base = cn_value(m.group(1))
    return (base, mults, m.group(0)) if base and mults else None


def overtime_rule(evidence: str) -> tuple[float, str, str, str, str] | None:
    """「平日延長工時：前二小時…加給三分之一，第三小時起加給三分之二」→ (2, "三分之一", "三分之二", 原句, "")。"""
    m = re.search(rf"平日延長工時[：:]?前({_NUMTXT})小時[^，,。]*?加給([^，,。；]+?)[，,]第({_NUMTXT})小時起[^，,。]*?加給([^，,。；]+?)[。；]",
                  _flat(evidence))
    if not m:
        return None
    first = cn_value(m.group(1))
    if not first or cn_value(m.group(2)) is None or cn_value(m.group(4)) is None:
        return None
    return first, m.group(2), m.group(4), m.group(0), ""


def formula_expectation(question: str, evidence: str) -> tuple[float, str] | None:
    """問題有足夠數字、條文解析得出公式時，回傳（應得數字, 給模型看的公式說明）；不適用回傳 None。"""
    q = question.replace("＋", "+")
    if "年終" in q:
        rule = bonus_rule(evidence)
        g = re.search(r"考績[^A-Z\d]{0,4}([A-C]\+?)|([A-C]\+?)\s*等", q)
        if not rule or not g:
            return None
        base, mults, clause = rule
        grade = g.group(1) or g.group(2)
        if grade not in mults:
            return None
        mult, salary = mults[grade], money_in_question(q, "月薪")
        if salary:
            val = salary * base * mult
            return val, (f"條文原文：「{clause}」。員工月薪 {salary:,.0f} 元、考績 {grade}，"
                         f"年終獎金＝月薪 {salary:,.0f} × 基準 {base:g} 個月 × {grade} 等 {mult:g} 倍＝{val:,.0f} 元")
        if "幾個月" in q:
            val = base * mult
            return val, f"條文原文：「{clause}」。考績 {grade} 的年終＝基準 {base:g} 個月 × {mult:g} 倍＝{val:g} 個月"
        return None
    if "加班" in q and not re.search(r"休息日|假日|週末|周末|例假|國定", q):
        rule, rate = overtime_rule(evidence), money_in_question(q, "時薪")
        h = re.search(rf"({_NUMTXT})\s*(?:個)?小時", q)
        hours = cn_value(h.group(1)) if h else None
        if not rule or not rate or not hours:
            return None
        first, p1, p2, clause, _ = rule
        h1, h2 = min(hours, first), max(hours - first, 0)
        val = rate * h1 * (1 + cn_value(p1)) + rate * h2 * (1 + cn_value(p2))
        steps = f"前 {h1:g} 小時：{rate:g} ×（1＋{p1}）× {h1:g}"
        if h2:
            steps += f"；第 {first + 1:g} 小時起：{rate:g} ×（1＋{p2}）× {h2:g}"
        return val, (f"條文原文：「{clause}」。「加給」是在原本的時薪之外另外多給，所以加班費要連原本的時薪一起算。"
                     f"員工時薪 {rate:g} 元、平日加班 {hours:g} 小時：{steps}，加班費總共 {val:,.2f} 元")
    return None


def numbers_in(text: str) -> list[float]:
    """答案裡的數字（52,000、11.7 萬、866.67）。"""
    out = []
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(萬)?", text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        out.append(v * 10000 if m.group(2) else v)
    return out


def has_value(text: str, val: float) -> bool:
    return any(abs(v - val) <= abs(val) * 0.005 + 0.01 for v in numbers_in(text))


# 親屬名單條文：「父母、配偶喪亡者，給予喪假八日；…」「祖父母喪亡者致贈奠儀新台幣二千元」
_LIST_CLAUSE = re.compile(rf"([^；。：:，,]+?)喪亡者[，,]?(給予喪假|致贈奠儀(?:新台幣)?)({_NUMTXT})(日|元)")


def kin_terms(text: str, kin_map: dict[str, str]) -> list[tuple[str, str]]:
    """由左到右、最長優先找出親屬稱謂（「曾祖母」→ 曾祖，不會再算一次「祖母」）。"""
    keys, found, i = sorted(kin_map, key=len, reverse=True), [], 0
    while i < len(text):
        k = next((k for k in keys if text.startswith(k, i)), None)
        if k:
            found.append((k, kin_map[k]))
            i += len(k)
        else:
            i += 1
    return found


def kin_rule(evidence: str, term: str, topic: str) -> tuple[float, str, str, set[float]] | None:
    """在條文的親屬名單裡找 term（條文用語，例：配偶）→（數值, 單位, 那一句, 這一條所有的數值）。名單裡沒有 → None。"""
    verb = "給予喪假" if topic == "喪假" else "致贈奠儀"
    rows = [(m.group(1), m.group(2), m.group(3), m.group(4), m.group(0)) for m in _LIST_CLAUSE.finditer(_flat(evidence))
            if m.group(2).startswith(verb)]
    values = {cn_value(r[2]) for r in rows}
    for who, _, num, unit, seg in rows:
        if term in re.sub(r"^(?:同仁|員工)之", "", who).split("、"):
            return cn_value(num), unit, seg, values
    return None


def stated_values(answer: str, unit: str) -> set[float]:
    """答案裡「N 日／N 天」或「N 元」的數值（不含「百日內分次申請」的 100）。"""
    rx = rf"({_NUMTXT})\s*(?:個)?(?:工作)?[日天]" if unit == "日" else rf"(?:新台幣)?\s*({_NUMTXT})\s*元"
    vals = {cn_value(m.group(1)) for m in re.finditer(rx, answer)}
    return {v for v in vals if v is not None and v != 100}

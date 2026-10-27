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


# ---- 第 8 輪：LaTeX 算式、兩個稱謂的親屬、文件沒寫的福利 ----

def latex_to_plain(text: str) -> str:
    """模型偶爾用 LaTeX 寫算式（`$240 \times (1 + \frac{2}{3}) = 400$`）：轉成一般算式，算式重算才讀得到，
    聊天視窗也不會顯示一堆反斜線。沒有 LaTeX 的文字原樣回傳。"""
    if "\\" not in text and "$" not in text:
        return text
    bs = chr(92)  # 反斜線（避免在 regex 字串裡層層跳脫）
    s = text.replace(bs + "dfrac", bs + "frac").replace(bs + "tfrac", bs + "frac")
    s = re.sub(r"[\\](?:text|mathrm|textbf|mathbf|operatorname)[{]([^{}]*)[}]", r"\1", s)
    for _ in range(4):  # 由內而外處理巢狀分數
        s2 = re.sub(r"[\\]frac[{]([^{}]*)[}][{]([^{}]*)[}]", r"(\1/\2)", s)
        s2 = re.sub(r"[\\]frac(\d)(\d)", r"(\1/\2)", s2)
        if s2 == s:
            break
        s = s2
    s = s.replace("{,}", ",")
    for cmd, rep in (("times", "×"), ("cdot", "×"), ("ast", "×"), ("div", "÷"), ("approx", "≈"),
                     ("left", ""), ("right", ""), ("displaystyle", ""), ("qquad", " "), ("quad", " ")):
        s = re.sub(r"[\\]" + cmd + r"(?![A-Za-z])", rep, s)
    for tok, rep in ((bs + ",", " "), (bs + ";", " "), (bs + "!", ""), (bs + "%", "%"),
                     (bs + "(", ""), (bs + ")", ""), (bs + "[", ""), (bs + "]", ""), ("$", "")):
        s = s.replace(tok, rep)
    s = re.sub(r"(?<![\[{])[{]([^{}\[\]]*)[}](?![}\]])", r"\1", s)  # 剩下的群組大括號（[[檔名]] 不動）
    return s


def formula_answer(question: str, evidence: str, outputs: list[str]) -> str | None:
    """FORMULA_CHECK 退回一次還是錯時的保底答案：直接用程式從條文算出的公式和數字回答（附出處）。"""
    exp = formula_expectation(question, evidence)
    if not exp:
        return None
    _, why = exp
    clause = re.search(r"條文原文：「(.+?)」", why)
    cite = ""
    if clause:
        for o in outputs:
            flat = re.sub(r"\s", "", o)
            k = flat.find(clause.group(1)[:20])
            if "[[" in o and k >= 0:
                files = re.findall(r"\[\[([^\]]+)\]\]", flat[:k])
                cite = f" [[{files[-1]}]]" if files else ""
                break
    return why.replace("條文原文：", "依規定").replace("。」", "」") + f"。{cite}".rstrip() + "\n\n（金額以人資部實際核算為準。）"


_SPOUSE_OF = re.compile(r"(太太|老婆|妻子|先生|老公|丈夫|配偶)(?:的|之)")


def compound_kin(question: str, kin_map: dict[str, str]) -> tuple[str, str] | None:
    """「老婆的爺爺」「先生的祖父母」「丈夫的姊姊」→（口語, 條文用語：配偶之祖父母／配偶之父母／配偶之兄弟姊妹）。
    只處理「配偶的 X」這種兩個稱謂；不是這種結構回傳 None。"""
    m = _SPOUSE_OF.search(question)
    if not m:
        return None
    rest = kin_terms(question[m.end():], kin_map)
    if not rest:
        other = re.match(r"(舅舅|舅媽|姑姑|姑丈|叔叔|嬸嬸|伯父|伯母|阿姨|姨丈|表[哥姐姊弟妹]|堂[哥姐姊弟妹]|姪[子女]|外甥)",
                         question[m.end():])
        return (m.group(1) + "的" + other.group(1), "配偶之" + other.group(1)) if other else None
    word, term = rest[0]
    if term == "配偶":
        return None
    return m.group(1) + "的" + word, "配偶之" + term


def kin_listed(evidence: str, topic: str) -> list[str]:
    """這一條（喪假／奠儀）名單上列的所有親屬條文用語；查到的條文裡沒有這一條 → 空 list。"""
    verb = "給予喪假" if topic == "喪假" else "致贈奠儀"
    out = []
    for m in _LIST_CLAUSE.finditer(_flat(evidence)):
        if m.group(2).startswith(verb):
            out += re.sub(r"^(?:同仁|員工)之", "", m.group(1)).split("、")
    return out


# 「公司有 X 津貼／補助嗎？」：X 在整個規章文件庫完全沒出現 → 文件沒有這項福利
_BENEFIT = r"津貼|補助|補貼|禮金|慰問金|獎金|保險|福利|優惠|折扣"
_STOP = r"公司|員工|同仁|有沒有|沒有|有|會|發放|發|給|提供|幫|投保|保(?!險)|每個月|每月|今年|我們|我|的|可以|申請|領|嗎|呢|請問|或|和|跟|與"
_EXIST_Q = re.compile(r"有沒有|有.{0,16}(?:嗎|？|\?)|會.{0,12}(?:嗎|？|\?)")


def _chunks(text: str) -> list[tuple[int, int, str]]:
    """去掉功能詞後剩下的中文片段（位置、片段）。"""
    marked = re.sub(rf"(?:{_BENEFIT}|{_STOP})", lambda m: "\0" * len(m.group(0)), text)
    return [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"[\u4e00-\u9fff]+", marked)]


def uncovered_benefit(question: str, corpus: str) -> str | None:
    """問「有沒有某項福利」，而問到的項目在整個文件庫都找不到 → 回傳那個項目；找得到或不是這種問題 → None。
    緊鄰福利詞的修飾語（「伙食津貼」的伙食）要整段出現在文件裡；中間隔了功能詞的（「員工生日公司會發禮金」的生日）
    只要任兩個字出現就算有（避免「看電影有員工優惠嗎」被誤判：文件有「電影票」）。"""
    if not _EXIST_Q.search(question):
        return None
    q = re.sub(r"[\s\d]", "", question)
    flat = re.sub(r"\s", "", corpus)
    chunks = _chunks(q)
    asked: list[tuple[str, bool, str]] = []  # (項目, 緊鄰福利詞, 顯示用的說法)
    for b in re.finditer(_BENEFIT, q):
        before = [c for c in chunks if c[1] <= b.start()]
        after = [c for c in chunks if c[0] >= b.end()]
        if before and before[-1][1] == b.start():         # 「伙食津貼」「子女教育補助」
            # 「托兒或育兒津貼」：「或」前面那一段也算問到的項目
            if len(before) >= 2 and q[before[-2][1]:before[-1][0]] in ("或", "和", "跟", "、"):
                asked.append((before[-2][2], True, before[-2][2] + b.group(0)))
            asked.append((before[-1][2], True, before[-1][2] + b.group(0)))
        elif after and after[0][0] == b.end() and re.search(r"補貼|補助", b.group(0)):  # 「補貼手機通訊費」
            asked.append((after[0][2], True, after[0][2]))
        elif before:                                       # 「員工生日公司會發禮金」
            asked.append((before[-1][2], False, before[-1][2] + b.group(0)))
    asked = [(re.sub(r"費用?$", "", a) or a, adj, show) for a, adj, show in asked]
    asked = [x for x in asked if len(x[0]) >= 2]
    if not asked:
        return None

    def covered(a: str, adj: bool) -> bool:
        return a in flat if adj else any(a[i:i + 2] in flat for i in range(len(a) - 1))

    return None if any(covered(a, adj) for a, adj, _ in asked) else "、".join(dict.fromkeys(s for *_, s in asked))

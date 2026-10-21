"""把條文裡的中文數字加註阿拉伯數字，例如「新台幣四千元」→「新台幣四千（4,000）元」。

動機：8B 模型把「四千」換成阿拉伯數字時寫成 40,000（改進循環 v2 的錯題）。
只處理後面接單位的數字（元、日、天、小時、個月、歲、晚、年、倍、次、萬元…），不動「第二條」這類序號。
"""
from __future__ import annotations

import re

_D = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_U = {"十": 10, "百": 100, "千": 1000}
_NUM = "零〇一二兩三四五六七八九十百千萬點"
_UNITS = "元|日|天|小時|個月|歲|晚|年|倍|次|胎|人"
_RX = re.compile(rf"(?<![第{_NUM}])([{_NUM}]+)(?=({_UNITS}))")


def _int(s: str) -> int:
    total, sec, num = 0, 0, 0
    for ch in s:
        if ch in _D:
            num = _D[ch]
        elif ch in _U:
            sec += (num or 1) * _U[ch]
            num = 0
        elif ch == "萬":
            total += (sec + num) * 10000
            sec = num = 0
    return total + sec + num


def cn_to_number(s: str) -> float | int | None:
    if not s or not any(c in _D or c in _U or c == "萬" for c in s):
        return None
    if "點" in s:
        a, b = s.split("點", 1)
        if not b or any(c not in _D for c in b):
            return None
        return float(f"{_int(a) if a else 0}.{''.join(str(_D[c]) for c in b)}")
    return _int(s)


def annotate(text: str) -> str:
    def rep(m: re.Match) -> str:
        v = cn_to_number(m.group(1))
        if v is None or m.group(1) in ("一",) and m.group(2) in ("日", "天", "次", "年"):
            return m.group(1)  # 「一日」「每年一次」這種常見說法不加註，避免太吵
        txt = f"{v:,}" if isinstance(v, int) else f"{v:g}"
        return f"{m.group(1)}（{txt}）"
    return _RX.sub(rep, text)

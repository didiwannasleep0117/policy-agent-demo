"""安全的計算機：只允許數字、四則運算、次方、比較與 round/min/max/abs，不執行任意程式碼。"""
from __future__ import annotations

import ast
import operator as op

_BIN = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
        ast.FloorDiv: op.floordiv, ast.Mod: op.mod, ast.Pow: op.pow}
_UNARY = {ast.UAdd: op.pos, ast.USub: op.neg, ast.Not: op.not_}
_CMP = {ast.Lt: op.lt, ast.LtE: op.le, ast.Gt: op.gt, ast.GtE: op.ge, ast.Eq: op.eq, ast.NotEq: op.ne}
_FUNCS = {"round": round, "min": min, "max": max, "abs": abs}
_TRANS = str.maketrans({"×": "*", "＊": "*", "÷": "/", "－": "-", "＋": "+", "（": "(", "）": ")",
                        "＝": "=", "≥": ">=", "≤": "<=", "，": ",", "　": " "})


def _ev(n):
    if isinstance(n, ast.Expression):
        return _ev(n.body)
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
        return n.value
    if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
        a, b = _ev(n.left), _ev(n.right)
        if isinstance(n.op, ast.Pow) and abs(b) > 100:
            raise ValueError("次方太大")
        return _BIN[type(n.op)](a, b)
    if isinstance(n, ast.UnaryOp) and type(n.op) in _UNARY:
        return _UNARY[type(n.op)](_ev(n.operand))
    if isinstance(n, ast.Compare):
        left = _ev(n.left)
        for o, c in zip(n.ops, n.comparators):
            right = _ev(c)
            if type(o) not in _CMP or not _CMP[type(o)](left, right):
                return False
            left = right
        return True
    if isinstance(n, ast.BoolOp):
        vals = [_ev(v) for v in n.values]
        return all(vals) if isinstance(n.op, ast.And) else any(vals)
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
        return _FUNCS[n.func.id](*[_ev(a) for a in n.args])
    raise ValueError(f"不支援的運算：{ast.dump(n)[:60]}")


def calculate(expression: str) -> str:
    expr = expression.translate(_TRANS).replace(",", "") if "(" not in expression else expression.translate(_TRANS)
    expr = expr.replace("且", " and ").replace("或", " or ")
    if len(expr) > 200:
        return "錯誤：算式太長"
    try:
        v = _ev(ast.parse(expr, mode="eval"))
    except ZeroDivisionError:
        return "錯誤：除以零"
    except Exception as e:
        return f"錯誤：{e}"
    if isinstance(v, bool):
        return f"{expression} → {'成立（True）' if v else '不成立（False）'}"
    if isinstance(v, float):
        v = round(v, 6)
        v = int(v) if v.is_integer() else v
    return f"{expression} = {v}"

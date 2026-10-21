"""把 PDF / Word / Excel / Markdown 轉成純文字。"""
from __future__ import annotations

import re
from pathlib import Path

import docx
import openpyxl
from pypdf import PdfReader

SUPPORTED = {".pdf", ".docx", ".xlsx", ".md", ".txt"}
_CJK = r"\u3000-\u303f\u4e00-\u9fff\uff00-\uffef"


def _join_cjk_lines(text: str) -> str:
    # PDF 抽字會在中文句子中間斷行，把「中文\n中文」接回去
    return re.sub(rf"(?<=[{_CJK}])\n(?=[{_CJK}])", "", text)


def load_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        pages = [p.extract_text() or "" for p in PdfReader(str(path)).pages]
        return _join_cjk_lines("\n".join(pages)).strip()
    if ext == ".docx":
        d = docx.Document(str(path))
        return "\n".join(p.text for p in d.paragraphs if p.text.strip())
    if ext == ".xlsx":
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        lines = []
        for ws in wb.worksheets:
            rows = [[("" if c is None else str(c)) for c in r] for r in ws.iter_rows(values_only=True)]
            if not rows:
                continue
            header, *body = rows
            lines.append(f"# 工作表：{ws.title}")
            # 每列寫成「欄位：值」，切塊後每一塊仍看得懂
            for r in body:
                lines.append("；".join(f"{h}：{v}" for h, v in zip(header, r) if v))
        return "\n".join(lines)
    if ext in {".md", ".txt"}:
        return path.read_text(encoding="utf-8")
    raise ValueError(f"不支援的格式：{path.name}")

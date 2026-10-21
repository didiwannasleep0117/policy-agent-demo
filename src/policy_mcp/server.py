"""公司規章 MCP 伺服器（Streamable HTTP，路徑 /mcp）。

環境變數：
  DOCS_DIR            規章文件資料夾（預設 data/docs）
  CACHE_DIR           索引與全文快取（預設 .cache）
  OLLAMA_URL          預設 http://127.0.0.1:11434
  EMBED_MODEL         預設 bge-m3
  HOST / PORT         預設 0.0.0.0 / 8000
  SYNC_INTERVAL_SEC   背景重新掃描間隔，預設 3600
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .calc import calculate as _calculate
from .cn_numbers import annotate as _annotate
from .indexer import PolicyIndex, Settings

ROOT = Path(__file__).resolve().parents[2]
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("policy_mcp")

cfg = Settings(
    docs_dir=Path(os.getenv("DOCS_DIR", ROOT / "data" / "docs")),
    cache_dir=Path(os.getenv("CACHE_DIR", ROOT / ".cache")),
    ollama_url=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"),
    embed_model=os.getenv("EMBED_MODEL", "bge-m3"),
)
index = PolicyIndex(cfg)

mcp = FastMCP(
    "policy-docs",
    instructions="公司規章與福利文件庫。先用 search_related_policies 或 list_all_filenames 找檔案，再用 read_specific_document 讀全文後回答。",
    host=os.getenv("HOST", "0.0.0.0"),
    port=int(os.getenv("PORT", "8000")),
    # Demo 在本機與 Docker（host.docker.internal）之間連線，關掉 Host 標頭檢查
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


NUM_ANNOTATE = os.getenv("NUM_ANNOTATE", "0") == "1"  # 中文數字加註阿拉伯數字
_fmt = _annotate if NUM_ANNOTATE else (lambda t: t)
FULLTEXT_DOCS = int(os.getenv("FULLTEXT_DOCS", "1"))      # small-to-big：前幾名直接附全文
FULLTEXT_MAX_CHARS = int(os.getenv("FULLTEXT_MAX_CHARS", "4000"))


@mcp.tool()
def search_related_policies(query: str) -> str:
    """用自然語言搜尋公司規章與福利文件（例如「婚假怎麼請」「出差住宿上限」）。
    用小片段找出最相關的文件，最相關的前幾份直接附上全文，其餘附相關段落。"""
    hits = index.search(query, top_files=int(os.getenv("TOP_FILES", "3")))
    if not hits:
        return "找不到相關文件。"
    out = [f"搜尋「{query}」的結果（只回答使用者這次問的問題，不要整理其他文件）："]
    for i, h in enumerate(hits, 1):
        head = f"{i}. [[{h['file']}]]（相關度 {h['best']}，命中 {h['hits']} 段）"
        r = index.read(h["file"]) if i <= FULLTEXT_DOCS else None
        if r and len(r[1]) <= FULLTEXT_MAX_CHARS:
            # Small-to-big：用小切塊找、回傳整份文件，條件與但書不會被切掉
            out.append(f"{head}\n《全文》[[{r[0]}]]\n{_fmt(r[1])}")
        else:
            snip = "\n".join(f"  > {_fmt(s)}" for s in h["snippets"])
            out.append(f"{head}\n《片段》（需要完整條文請用 read_specific_document）\n{snip}")
    return "\n\n".join(out)


@mcp.tool()
def read_specific_document(filename: str) -> str:
    """讀取指定文件的完整內容。filename 用 list_all_filenames 或搜尋結果中的檔名（可省略副檔名）。"""
    r = index.read(filename)
    if r is None:
        return f"找不到「{filename}」。目前的文件：{', '.join(index.files())}"
    name, text = r
    return f"[[{name}]]\n\n{_fmt(text)}"


@mcp.tool()
def list_all_filenames() -> str:
    """列出目前已索引的所有規章與福利文件檔名。"""
    return "\n".join(index.files()) or "目前沒有文件。"


@mcp.tool()
def calculate(expression: str) -> str:
    """計算機。任何計算或數字比較都用這個工具，不要自己心算。
    支援 + - * / ** ( )、比較（>= <= > < ==）、and / or、round/min/max/abs。
    例：「1.5 * 1.5」「35 >= 40」「30000 / 30 * 8」。"""
    return _calculate(expression)


@mcp.custom_route("/docs/{name}", methods=["GET"])
async def open_doc(request):
    """讓聊天視窗的「參考文件」卡片可以點開原始檔（只開放 DOCS_DIR 裡的檔案）。"""
    from urllib.parse import unquote
    from starlette.responses import FileResponse, PlainTextResponse
    name = Path(unquote(request.path_params["name"])).name
    path = cfg.docs_dir / name
    if not path.is_file():
        return PlainTextResponse("not found", status_code=404)
    inline = path.suffix.lower() == ".pdf"
    return FileResponse(path, filename=name, content_disposition_type="inline" if inline else "attachment")


def _background_sync(interval: int) -> None:
    stop = threading.Event()
    while not stop.wait(interval):
        try:
            index.sync()
        except Exception:
            log.exception("背景同步失敗")


def main() -> None:
    log.info("初始索引中：%s", cfg.docs_dir)
    log.info("初始索引完成：%s", index.sync())
    interval = int(os.getenv("SYNC_INTERVAL_SEC", "3600"))
    threading.Thread(target=_background_sync, args=(interval,), daemon=True).start()
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()

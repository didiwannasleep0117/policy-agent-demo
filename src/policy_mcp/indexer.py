"""向量索引：切塊、用 Ollama 的 BGE-M3 向量化、存進 ChromaDB，依檔案修改時間增量更新。"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import chromadb
import httpx
import tiktoken

from .loaders import SUPPORTED, load_text

log = logging.getLogger("policy_mcp.indexer")
_ENC = tiktoken.get_encoding("cl100k_base")


def n_tokens(s: str) -> int:
    return len(_ENC.encode(s))


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？；\n])", text)
    return [p.strip() for p in parts if p.strip()]


def chunk_text(text: str, size: int = 256, overlap: int = 30) -> list[str]:
    """以句子為單位裝箱到 size token；下一塊帶上前一塊結尾約 overlap token 的句子。"""
    chunks: list[str] = []
    cur: list[str] = []
    cur_tok = 0
    for sent in split_sentences(text):
        t = n_tokens(sent)
        if cur and cur_tok + t > size:
            chunks.append("".join(cur))
            tail: list[str] = []
            tail_tok = 0
            for s in reversed(cur):
                st = n_tokens(s)
                if tail_tok + st > overlap:
                    break
                tail.insert(0, s)
                tail_tok += st
            cur, cur_tok = tail, tail_tok
        cur.append(sent)
        cur_tok += t
    if cur:
        chunks.append("".join(cur))
    return chunks


@dataclass
class Settings:
    docs_dir: Path
    cache_dir: Path
    ollama_url: str = "http://127.0.0.1:11434"
    embed_model: str = "bge-m3"
    chunk_size: int = 256
    chunk_overlap: int = 30


class PolicyIndex:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        self._manifest_path = cfg.cache_dir / "manifest.json"
        self._texts_path = cfg.cache_dir / "texts.json"
        self.manifest: dict[str, float] = self._read_json(self._manifest_path)
        self.texts: dict[str, str] = self._read_json(self._texts_path)
        self._db = chromadb.PersistentClient(path=str(cfg.cache_dir / "chroma"))
        self.col = self._db.get_or_create_collection("policies", metadata={"hnsw:space": "cosine"})
        self._lock = threading.Lock()
        self._http = httpx.Client(timeout=120)

    @staticmethod
    def _read_json(p: Path) -> dict:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _save(self) -> None:
        self._manifest_path.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        self._texts_path.write_text(json.dumps(self.texts, ensure_ascii=False), encoding="utf-8")

    def embed(self, texts: list[str]) -> list[list[float]]:
        r = self._http.post(f"{self.cfg.ollama_url}/api/embed", json={"model": self.cfg.embed_model, "input": texts})
        r.raise_for_status()
        return r.json()["embeddings"]

    def _current_files(self) -> dict[str, float]:
        return {
            p.name: p.stat().st_mtime
            for p in sorted(self.cfg.docs_dir.iterdir())
            if p.is_file() and p.suffix.lower() in SUPPORTED and not p.name.startswith("~$")
        }

    def sync(self) -> dict[str, list[str]]:
        """新增或修改的檔案重建索引，已刪除的檔案從索引移除。"""
        with self._lock:
            now = self._current_files()
            added = [f for f in now if f not in self.manifest]
            changed = [f for f in now if f in self.manifest and now[f] != self.manifest[f]]
            removed = [f for f in self.manifest if f not in now]
            for f in changed + removed:
                self.col.delete(where={"file": f})
                self.manifest.pop(f, None)
                self.texts.pop(f, None)
            for f in added + changed:
                try:
                    text = load_text(self.cfg.docs_dir / f)
                except Exception as e:  # 單一壞檔不要拖垮整批
                    log.warning("讀取失敗 %s：%s", f, e)
                    continue
                chunks = chunk_text(text, self.cfg.chunk_size, self.cfg.chunk_overlap)
                if chunks:
                    self.col.add(
                        ids=[f"{f}::{i}" for i in range(len(chunks))],
                        documents=chunks,
                        embeddings=self.embed(chunks),
                        metadatas=[{"file": f, "pos": i} for i in range(len(chunks))],
                    )
                self.manifest[f] = now[f]
                self.texts[f] = text
            self._save()
            stats = {"added": added, "changed": changed, "removed": removed}
            if any(stats.values()):
                log.info("索引更新：%s", stats)
            return stats

    def search(self, query: str, recall_k: int = 50, top_files: int = 5, snippets_per_file: int = 3) -> list[dict]:
        """Small-to-big：先召回大量小片段，再依檔案聚合排序。"""
        total = self.col.count()
        if total == 0:
            return []
        res = self.col.query(query_embeddings=self.embed([query]), n_results=min(recall_k, total))
        groups: dict[str, dict] = {}
        for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
            sim = 1 - dist
            g = groups.setdefault(meta["file"], {"file": meta["file"], "hits": 0, "best": 0.0, "score": 0.0, "chunks": []})
            g["hits"] += 1
            g["best"] = max(g["best"], sim)
            g["chunks"].append((sim, meta["pos"], doc))
        for g in groups.values():
            # 最佳片段為主，命中次數小幅加分（同一份文件多處相關 → 更可能是答案所在）
            top = sorted(g["chunks"], reverse=True)[:snippets_per_file]
            g["score"] = g["best"] + 0.02 * min(g["hits"], 5)
            g["snippets"] = [d for _, _, d in sorted(top, key=lambda x: x[1])]
            del g["chunks"]
        ranked = sorted(groups.values(), key=lambda g: g["score"], reverse=True)[:top_files]
        for g in ranked:
            g["best"] = round(g["best"], 3)
            g["score"] = round(g["score"], 3)
        return ranked

    def files(self) -> list[str]:
        return sorted(self.manifest)

    def read(self, name: str) -> tuple[str, str] | None:
        """回傳 (檔名, 全文)。允許省略副檔名或只給部分檔名。"""
        if name in self.texts:
            return name, self.texts[name]
        cands = [f for f in self.texts if name in f or f.rsplit(".", 1)[0] in name]
        if len(cands) == 1:
            return cands[0], self.texts[cands[0]]
        return None

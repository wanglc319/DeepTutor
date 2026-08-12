"""Retrieval-only pipeline backed by an external Qdrant collection.

Mirrors :class:`ImaPipeline`: DeepTutor stores a connection pointer (host,
port, collection name) per KB and delegates indexing entirely to an external
writer (typically Dify + Dify-Qdrant integration). :meth:`search` is the only
method that does real work — it embeds the query with DeepTutor's active
embedding client and hits Qdrant's search API.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from deeptutor.runtime.home import get_runtime_data_root
from deeptutor.services.embedding.client import get_embedding_client
from deeptutor.services.rag.provider_binding import load_kb_config_entry

from .config import QdrantConfig, QdrantNotConfiguredError, config_from_entry

logger = logging.getLogger(__name__)

PROVIDER = "qdrant"
DEFAULT_KB_BASE_DIR = str(get_runtime_data_root() / "knowledge_bases")

_DEFAULT_TOP_K = 8


class QdrantPipeline:
    """Query a Qdrant collection on behalf of a connected KB."""

    def __init__(self, kb_base_dir: Optional[str] = None, **_: Any) -> None:
        self.logger = logger
        self.kb_base_dir = kb_base_dir or DEFAULT_KB_BASE_DIR

    # ----- helpers --------------------------------------------------------

    @staticmethod
    def _top_k(kwargs: dict[str, Any]) -> int:
        try:
            requested = int(kwargs.get("top_k") or _DEFAULT_TOP_K)
        except (TypeError, ValueError):
            return _DEFAULT_TOP_K
        return max(1, min(requested, 50))

    @staticmethod
    def _client(cfg: QdrantConfig):
        from qdrant_client import QdrantClient

        return QdrantClient(host=cfg.host, port=cfg.port)

    @staticmethod
    async def _embed_query(query: str) -> List[float]:
        client = get_embedding_client()
        vectors = await client.embed([query])
        if not vectors:
            raise RuntimeError("Embedding client returned an empty vector for the query.")
        return vectors[0]

    # ----- retrieval ------------------------------------------------------

    async def search(self, query: str, kb_name: str, **kwargs) -> Dict[str, Any]:
        try:
            cfg = config_from_entry(load_kb_config_entry(self.kb_base_dir, kb_name))
        except QdrantNotConfiguredError as exc:
            return self._error_result(query, exc, error_type="not_configured")

        try:
            query_vector = await self._embed_query(query)
        except Exception as exc:
            self.logger.error("Failed to embed query for KB '%s': %s", kb_name, exc)
            return self._error_result(query, exc, error_type="embedding_error")

        try:
            hits = self._search_qdrant(cfg, query_vector, self._top_k(kwargs))
        except Exception as exc:
            self.logger.error("Qdrant search failed for '%s': %s", kb_name, exc)
            return self._error_result(query, exc, error_type="retrieval_error")

        sources = _sources_from_hits(hits)
        content = _render_context(sources)
        return {
            "query": query,
            "answer": content,
            "content": content,
            "sources": sources,
            "provider": PROVIDER,
        }

    def _search_qdrant(self, cfg: QdrantConfig, query_vector: List[float], top_k: int) -> list:
        client = self._client(cfg)
        extra: dict[str, Any] = {}
        if cfg.vector_name:
            extra["vector_name"] = cfg.vector_name

        # qdrant_client >= 1.10 移除了 client.search，改用 query_points
        if hasattr(client, "query_points"):
            resp = client.query_points(
                collection_name=cfg.collection_name,
                query=query_vector,
                limit=top_k,
                with_payload=True,
                **extra,
            )
            return list(resp.points)

        # 旧版 fallback (qdrant_client < 1.10)
        search_kwargs: dict[str, Any] = {
            "collection_name": cfg.collection_name,
            "query_vector": query_vector,
            "limit": top_k,
            "with_payload": True,
            **extra,
        }
        return list(client.search(**search_kwargs))

    def _error_result(self, query: str, exc: Exception, *, error_type: str) -> Dict[str, Any]:
        return {
            "query": query,
            "answer": str(exc),
            "content": "",
            "sources": [],
            "provider": PROVIDER,
            "error_type": error_type,
        }

    # ----- indexing (not applicable — owned by Dify / external writer) ----

    async def initialize(self, kb_name: str, file_paths: List[str], **kwargs) -> bool:
        raise RuntimeError(
            "Qdrant knowledge bases are indexed externally (e.g. Dify). "
            "DeepTutor does not build their index. Add documents in Dify directly."
        )

    async def add_documents(self, kb_name: str, file_paths: List[str], **kwargs) -> bool:
        return await self.initialize(kb_name, file_paths, **kwargs)

    # ----- lifecycle ------------------------------------------------------

    async def delete(self, kb_name: str, **kwargs) -> bool:
        return True


def _sources_from_hits(hits: list) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for point in hits:
        payload = point.payload or {}

        # page_content (Dify) vs faq_text (我们的 FAQ KB)
        page_content = str(
            payload.get("page_content") or payload.get("faq_text") or ""
        ).strip()

        # metadata 嵌套或顶层字段都照顾到
        metadata = payload.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                import json
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}

        # 标题优先级: original_title (Dify) > metadata.filename > metadata.name > topic (章节) > fallback
        title = str(
            payload.get("original_title")
            or metadata.get("filename")
            or metadata.get("name")
            or payload.get("topic")
            or ""
        ).strip()

        # 文件名/来源路径: source_file (Dify) > filename > source
        source = str(
            payload.get("source_file")
            or payload.get("filename")
            or payload.get("source")
            or metadata.get("filename")
            or ""
        ).strip()

        # 从 source_file 里提取纯文件名
        source_basename = ""
        if source:
            import os
            source_basename = os.path.basename(source)

        page = payload.get("page") or metadata.get("page")

        doc_id = str(
            metadata.get("doc_id")
            or payload.get("group_id")
            or source_basename
            or title
            or ""
        )

        sources.append(
            {
                "title": title or source_basename or f"Point {point.id}",
                "content": page_content[:500],
                "source": source_basename or doc_id,
                "source_path": source,
                "topic": str(payload.get("topic") or ""),
                "chunk_id": str(point.id),
                "score": round(point.score, 4) if point.score is not None else 0.0,
            }
        )
    return sources


def _render_context(sources: list[dict[str, Any]]) -> str:
    blocks = [
        f"[{idx}] {src['title']} (score={src['score']})\n{src['content']}".rstrip()
        for idx, src in enumerate(sources, start=1)
        if src["content"]
    ]
    return "\n\n".join(blocks)


__all__ = ["QdrantPipeline", "PROVIDER"]

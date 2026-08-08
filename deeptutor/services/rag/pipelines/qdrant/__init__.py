"""Qdrant-backed RAG pipeline — retrieval-only, mirroring the IMA / LightRAG
server pipelines. DeepTutor owns the KB pointer; documents live in Qdrant and
are curated externally (typically Dify). The pipeline only generates query
embeddings (via DeepTutor's active embedding client) and hits Qdrant's search
API.
"""

from __future__ import annotations

SUPPORTED_MODES: tuple[str, ...] = ()
DEFAULT_MODE = ""

__all__ = ["SUPPORTED_MODES", "DEFAULT_MODE"]

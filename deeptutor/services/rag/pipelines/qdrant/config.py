"""Per-KB Qdrant connection config.

A KB bound to the ``qdrant`` provider is a connection pointer: the user sets
(host, port, collection_name, vector_name) once at connect time and later
Dify (or any writer) populates that collection. DeepTutor never writes vectors
here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class QdrantNotConfiguredError(RuntimeError):
    """Raised when a KB is missing required Qdrant connection fields."""


@dataclass(frozen=True)
class QdrantConfig:
    host: str
    port: int
    collection_name: str
    vector_name: str = ""


def config_from_entry(entry: dict[str, Any]) -> QdrantConfig:
    host = str(entry.get("host") or "").strip()
    port_raw = entry.get("port")
    collection_name = str(entry.get("collection_name") or "").strip()

    port = 6333
    if port_raw is not None:
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            raise QdrantNotConfiguredError(
                f"Invalid Qdrant port '{port_raw}' for KB '{entry.get('name', '?')}'."
            )

    missing = [
        label
        for label, value in (
            ("host", host),
            ("collection_name", collection_name),
        )
        if not value
    ]
    if missing:
        raise QdrantNotConfiguredError(
            "This knowledge base is not fully connected to Qdrant "
            f"(missing {', '.join(missing)}). Re-create it with host and collection_name."
        )

    return QdrantConfig(
        host=host,
        port=port,
        collection_name=collection_name,
        vector_name=str(entry.get("vector_name") or "").strip(),
    )


__all__ = ["QdrantNotConfiguredError", "QdrantConfig", "config_from_entry"]

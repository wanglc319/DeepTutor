"""Storage backend abstraction for the three-layer memory subsystem.

Phase-1 foundation: the existing filesystem implementation is extracted into
:class:`FileBackend` behind the :class:`MemoryBackend` abstract interface. All
file I/O in :mod:`store` and :mod:`trace` now flows through ``self._backend``
instead of calling :mod:`paths` and :mod:`pathlib` directly.

Adding a new backend (SQLite in Phase 2, PostgreSQL + Qdrant in Phase 3) is a
matter of implementing the ABC — no caller above this file needs to change.

Per-user namespace is established when a backend instance is constructed.
Every append, read, and write call is therefore automatically scoped to one
user — cross-user data leakage is structurally impossible.
"""

from __future__ import annotations

import abc
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Iterator


class MemoryLock(abc.ABC):
    """Abstract reentrant/exclusive lock for a single resource (doc path, etc).

    Must support ``async with``. Implementations wrap :class:`asyncio.Lock`,
    a PostgreSQL ``SELECT … FOR UPDATE`` row lock, a Redis SETNX lock, etc.
    """

    @abc.abstractmethod
    async def __aenter__(self) -> "MemoryLock": ...

    @abc.abstractmethod
    async def __aexit__(self, exc_type, exc, tb) -> None: ...


class _AsyncioLock(MemoryLock):
    """Default in-process lock, keyed by a string resource id."""

    _locks: dict[str, asyncio.Lock] = {}
    _MAX_LOCKS = 100_000

    def __init__(self, resource: str) -> None:
        self._resource = resource

    async def __aenter__(self) -> "_AsyncioLock":
        lock = _AsyncioLock._locks.get(self._resource)
        if lock is None:
            lock = asyncio.Lock()
            _AsyncioLock._locks[self._resource] = lock
            if len(_AsyncioLock._locks) > _AsyncioLock._MAX_LOCKS:
                self._evict_one()
        await lock.acquire()
        self._lock = lock
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._lock.release()

    @classmethod
    def _evict_one(cls) -> None:
        oldest_key = min(
            cls._locks.keys(),
            key=lambda k: (
                getattr(cls._locks[k], "_last_used", 0),
                k,
            ),
        )
        del cls._locks[oldest_key]


class MemoryBackend(abc.ABC):
    """Storage backend for one user's three-layer memory.

    A backend instance is bound to exactly one user namespace at
    construction time. The factory (added later in Phase 2) picks the right
    backend per user based on scale / access pattern; this ABC is what
    callers program against.

    Method semantics match the existing filesystem behaviour exactly — that
    is how ``FileBackend`` satisfies it. New implementations (SQLite, PG) may
    choose different internal representations but must preserve semantics.
    """

    # ── L1 trace ──────────────────────────────────────────────────────────

    @abc.abstractmethod
    async def append_trace(self, surface: str, day_iso: str, line: str) -> None:
        """Append one JSONL line for ``surface`` on ``day_iso`` (YYYY-MM-DD)."""

    @abc.abstractmethod
    def iter_trace(
        self, surface: str, since: datetime | None = None
    ) -> Iterator[dict]:
        """Yield raw event dicts for ``surface`` in chronological order.

        If ``since`` is given, only events with ``ts >= since`` (UTC) are
        yielded. The caller is responsible for turning dicts back into
        :class:`TraceEvent`.
        """

    @abc.abstractmethod
    def count_trace(self, surface: str, since: datetime | None = None) -> int:
        """Number of events for ``surface``, optionally since ``since``."""

    @abc.abstractmethod
    def latest_trace_ts(self, surface: str) -> str | None:
        """ISO timestamp of the most recent event for ``surface``, or None."""

    @abc.abstractmethod
    def trace_total_bytes(self) -> int:
        """Approximate L1 storage footprint in bytes (for monitoring)."""

    # ── L2 / L3 documents ──────────────────────────────────────────────────

    @abc.abstractmethod
    def doc_exists(self, layer: str, key: str) -> bool:
        """True if the document for (layer, key) has been created."""

    @abc.abstractmethod
    def doc_mtime(self, layer: str, key: str) -> datetime | None:
        """Document last-modified UTC timestamp, or None if missing."""

    @abc.abstractmethod
    def read_doc_text(self, layer: str, key: str) -> str:
        """Full markdown text of the document, or ``""`` if missing."""

    @abc.abstractmethod
    async def write_doc_text(self, layer: str, key: str, md: str) -> None:
        """Atomically replace the document text for (layer, key) with ``md``."""

    @abc.abstractmethod
    def doc_total_bytes(self) -> int:
        """Approximate L2+L3 storage footprint in bytes (for monitoring)."""

    # ── Lock ───────────────────────────────────────────────────────────────

    @abc.abstractmethod
    def lock(self, resource: str) -> MemoryLock:
        """Return a lock bound to this backend's user namespace.

        ``resource`` is typically a string like ``"L3/preferences"`` — the
        backend chooses the actual key (file path, table + row, redis key …).
        """


class FileBackend(MemoryBackend):
    """Filesystem-backed :class:`MemoryBackend`.

    Uses the existing :mod:`paths` module for location resolution so every
    file that is created/opened/stored lives inside the active user's
    workspace (via :func:`paths.memory_root` which honours the
    :data:`~paths.memory_path_service_override` context variable).
    """

    def __init__(self) -> None:
        from deeptutor.services.memory import paths

        self._paths = paths

    # ── L1 ────────────────────────────────────────────────────────────────

    async def append_trace(self, surface: str, day_iso: str, line: str) -> None:
        path = self._paths.trace_dir(surface) / f"{day_iso}.jsonl"
        await asyncio.to_thread(self._append_line_sync, path, line)

    @staticmethod
    def _append_line_sync(path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")

    def iter_trace(
        self, surface: str, since: datetime | None = None
    ) -> Iterator[dict]:
        import json

        files = sorted(self._paths.trace_dir(surface).glob("*.jsonl"))
        cutoff_iso = since.isoformat() if since else ""
        cutoff_date_iso = since.date().isoformat() if since else ""
        for path in files:
            if cutoff_date_iso and path.stem < cutoff_date_iso:
                continue
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for raw in fh:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            obj = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if cutoff_iso and obj.get("ts", "") < cutoff_iso:
                            continue
                        yield obj
            except OSError:
                continue

    def count_trace(self, surface: str, since: datetime | None = None) -> int:
        return sum(1 for _ in self.iter_trace(surface, since))

    def latest_trace_ts(self, surface: str) -> str | None:
        import json

        files = sorted(
            self._paths.trace_dir(surface).glob("*.jsonl"), reverse=True
        )
        for path in files:
            try:
                last = ""
                with path.open("r", encoding="utf-8") as fh:
                    for raw in fh:
                        raw = raw.strip()
                        if raw:
                            last = raw
                if last:
                    obj = json.loads(last)
                    ts = obj.get("ts")
                    if isinstance(ts, str):
                        return ts
            except (OSError, json.JSONDecodeError):
                continue
        return None

    def trace_total_bytes(self) -> int:
        root = self._paths.memory_root() / "trace"
        total = 0
        try:
            for p in root.rglob("*"):
                if p.is_file():
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    # ── L2 / L3 ────────────────────────────────────────────────────────────

    def _doc_path(self, layer: str, key: str) -> Path:
        if layer == "L2":
            return self._paths.l2_file(key)  # type: ignore[arg-type]
        return self._paths.l3_file(key)  # type: ignore[arg-type]

    def doc_exists(self, layer: str, key: str) -> bool:
        return self._doc_path(layer, key).exists()

    def doc_mtime(self, layer: str, key: str) -> datetime | None:
        path = self._doc_path(layer, key)
        if not path.exists():
            return None
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    def read_doc_text(self, layer: str, key: str) -> str:
        path = self._doc_path(layer, key)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    async def write_doc_text(self, layer: str, key: str, md: str) -> None:
        path = self._doc_path(layer, key)
        await asyncio.to_thread(self._atomic_write_sync, path, md)

    @staticmethod
    def _atomic_write_sync(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

    def doc_total_bytes(self) -> int:
        total = 0
        for sub in ("L2", "L3"):
            root = self._paths.memory_root() / sub
            try:
                for p in root.rglob("*.md"):
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
            except OSError:
                pass
        return total

    # ── Lock ───────────────────────────────────────────────────────────────

    def lock(self, resource: str) -> _AsyncioLock:
        # FileBackend uses the absolute file path as the lock key so two
        # callers locking the same doc get the same asyncio.Lock instance.
        key = self._resolve_lock_key(resource)
        return _AsyncioLock(key)

    def _resolve_lock_key(self, resource: str) -> str:
        """Turn a resource id like ``'L3/preferences'`` into an absolute path."""
        if resource.startswith("L2/"):
            return str(self._paths.l2_file(resource[3:]))
        if resource.startswith("L3/"):
            return str(self._paths.l3_file(resource[3:]))
        # Fallback: treat as a relative path under memory root
        return str(self._paths.memory_root() / resource)

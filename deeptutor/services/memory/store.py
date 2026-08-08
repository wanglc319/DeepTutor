"""High-level facade for the three-layer memory subsystem.

All callers — API routers, LLM tools, surface event hooks — go through
:class:`MemoryStore`. The store is thin: every I/O operation now flows
through :class:`~deeptutor.services.memory.backend.MemoryBackend`, making
it trivial to swap the persistence layer (filesystem → SQLite → PostgreSQL)
without touching any caller above this module.

Per-user isolation is inherited from the backend instance which is bound to
a single user namespace at construction time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
import shutil
from typing import Literal

from deeptutor.services.memory import consolidator, paths
from deeptutor.services.memory.backend import FileBackend, MemoryBackend
from deeptutor.services.memory.consolidator import ConsolidateResult, OnEvent
from deeptutor.services.memory.document import Document, parse, serialize
from deeptutor.services.memory.ops import AddOp, ApplyReport, EditOp, OpResult
from deeptutor.services.memory.ops import apply as ops_apply
from deeptutor.services.memory.paths import L3Slot, Surface
from deeptutor.services.memory.trace import TraceEvent

logger = logging.getLogger(__name__)

Layer = Literal["L2", "L3"]

_V1_FILES = ("PROFILE.md", "SUMMARY.md")


def _normalize_pref_text(text: str) -> str:
    """Whitespace/case-insensitive key for duplicate-preference detection."""
    return " ".join(str(text or "").split()).casefold()


def _find_duplicate_preference(doc: Document, section: str, text: str):
    """Return an existing entry in ``section`` whose text matches ``text``.

    Read-only — does not create the section if it is absent.
    """
    target = _normalize_pref_text(text)
    if not target:
        return None
    for entry in doc.all_entries():
        if entry.section == section and _normalize_pref_text(entry.text) == target:
            return entry
    return None


_NO_MEMORY = (
    "(No memory available — interact with DeepTutor and update from the Memory page to build one.)"
)


@dataclass
class DocOverview:
    layer: Layer
    key: str  # surface name (L2) or slot name (L3)
    exists: bool
    updated_at: str | None
    entry_count: int
    backlog: int  # L1 events since last update (L2 only; 0 for L3)


def _default_title(layer: Layer, key: str) -> str:
    if layer == "L2":
        return f"{key} memory"
    return {
        "recent": "Recent summary",
        "profile": "User profile",
        "scope": "Knowledge scope",
        "preferences": "Preferences",
    }.get(key, key)


class MemoryStore:
    """Facade that delegates all storage to a :class:`MemoryBackend`.

    Defaults to :class:`FileBackend` so existing installs keep working with
    no config change. Other backends (SQLite, PostgreSQL) are wired in
    later through :func:`get_memory_store`.
    """

    def __init__(self, backend: MemoryBackend | None = None) -> None:
        self._backend: MemoryBackend = backend or FileBackend()

    # ── L1 ────────────────────────────────────────────────────────────────

    async def emit(self, event: TraceEvent) -> None:
        """Write one trace event to L1. Never raises."""
        import json
        from dataclasses import asdict

        try:
            line = json.dumps(
                asdict(event), ensure_ascii=False, separators=(",", ":")
            )
            day_iso = datetime.now(tz=timezone.utc).date().isoformat()
            await self._backend.append_trace(event.surface, day_iso, line)
        except Exception:
            logger.warning(
                "memory trace append failed surface=%s kind=%s",
                event.surface,
                event.kind,
                exc_info=True,
            )

    # ── L2 / L3 read ──────────────────────────────────────────────────────

    def read_doc(self, layer: Layer, key: str) -> Document:
        md = self._backend.read_doc_text(layer, key)
        if not md:
            return Document(title=_default_title(layer, key))
        return parse(md)

    def read_raw(self, layer: Layer, key: str) -> str:
        return self._backend.read_doc_text(layer, key)

    def read_l3_concat(self) -> str:
        """Concatenate all four L3 docs for the ``read_memory`` tool."""
        parts: list[str] = []
        for slot in paths.L3_SLOTS:
            body = self._backend.read_doc_text("L3", slot).strip()
            if body:
                parts.append(body)
        if not parts:
            return _NO_MEMORY
        return "\n\n---\n\n".join(parts) + "\n"

    # ── L2 / L3 write (manual paths) ──────────────────────────────────────

    async def overwrite_doc(self, layer: Layer, key: str, md: str) -> None:
        """Direct user-driven save from the workbench editor."""
        async with self._backend.lock(f"{layer}/{key}"):
            await self._backend.write_doc_text(layer, key, md)

    async def delete_entry(self, layer: Layer, key: str, entry_id: str) -> bool:
        if not self._backend.doc_exists(layer, key):
            return False
        async with self._backend.lock(f"{layer}/{key}"):
            md = self._backend.read_doc_text(layer, key)
            doc = parse(md)
            if not doc.remove(entry_id):
                return False
            await self._backend.write_doc_text(layer, key, serialize(doc))
            return True

    # ── L2 / L3 write (consolidator paths) ────────────────────────────────

    async def update_l2(
        self,
        surface: Surface,
        *,
        language: str = "en",
        user_label: str = "anonymous",
        on_event: OnEvent | None = None,
        apply_ops: bool = True,
    ) -> ConsolidateResult:
        async with self._backend.lock(f"L2/{surface}"):
            return await consolidator.consolidate_l2(
                surface,
                language=language,
                user_label=user_label,
                on_event=on_event,
                apply_ops=apply_ops,
            )

    async def update_l3(
        self,
        slot: L3Slot,
        *,
        language: str = "en",
        user_label: str = "anonymous",
        on_event: OnEvent | None = None,
        apply_ops: bool = True,
    ) -> ConsolidateResult:
        if slot == "preferences":
            raise ValueError("preferences.md is not auto-consolidated")
        async with self._backend.lock(f"L3/{slot}"):
            return await consolidator.consolidate_l3(
                slot,
                language=language,
                user_label=user_label,
                on_event=on_event,
                apply_ops=apply_ops,
            )

    async def apply_ops_payload(
        self, layer: Layer, key: str, ops_payload: list[dict]
    ) -> ApplyReport:
        """Apply a list of ops-as-JSON to a layer doc atomically.

        Used by the workbench's preview → apply two-step flow. The
        payload typically comes from a previous ``apply_ops=False``
        consolidate call surfaced to the user for review.
        """
        from deeptutor.services.memory.consolidator import _parse_ops_response

        import json as _json

        ops = _parse_ops_response(
            _json.dumps({"ops": ops_payload}, ensure_ascii=False)
        )
        async with self._backend.lock(f"{layer}/{key}"):
            default_title = _default_title(layer, key)
            md = self._backend.read_doc_text(layer, key)
            doc = parse(md) if md else Document(title=default_title)
            report = ops_apply(doc, ops)
            if report.accepted and ops:
                await self._backend.write_doc_text(layer, key, serialize(doc))
            return report

    async def write_preference(
        self,
        *,
        op: Literal["add", "edit"],
        text: str,
        target_id: str | None = None,
        reason: str | None = None,
        trace_id: str,
    ) -> ApplyReport:
        """Write the chat-mode preference signal. The ``write_memory`` tool
        is the only caller; ``trace_id`` is the current chat turn's L1 id
        injected by runtime."""
        async with self._backend.lock("L3/preferences"):
            md = self._backend.read_doc_text("L3", "preferences")
            doc = (
                parse(md)
                if md
                else Document(title=_default_title("L3", "preferences"))
            )
            section = "Preferences"
            if op == "add":
                duplicate = _find_duplicate_preference(doc, section, text)
                if duplicate is not None:
                    return ApplyReport(
                        accepted=True,
                        results=[
                            OpResult(
                                op=AddOp(
                                    section=section, text=text, refs=[trace_id]
                                ),
                                status="applied",
                                entry_id=duplicate.id,
                                detail="duplicate",
                            )
                        ],
                    )
                report = ops_apply(
                    doc,
                    [AddOp(section=section, text=text, refs=[trace_id])],
                )
            else:
                if not target_id:
                    return ApplyReport(
                        accepted=False, reason="edit requires target_id"
                    )
                report = ops_apply(
                    doc,
                    [
                        EditOp(
                            target_id=target_id,
                            new_text=text,
                            new_refs=[trace_id],
                        )
                    ],
                )
            if report.accepted:
                await self._backend.write_doc_text(
                    "L3", "preferences", serialize(doc)
                )
            if reason:
                logger.info(
                    "write_memory %s id=%s reason=%s", op, target_id or "new", reason
                )
            return report

    # ── Workbench overview ────────────────────────────────────────────────

    def overview(self) -> list[DocOverview]:
        rows: list[DocOverview] = []
        for surface in paths.SURFACES:
            rows.append(self._overview_for("L2", surface))
        for slot in paths.L3_SLOTS:
            rows.append(self._overview_for("L3", slot))
        return rows

    def _overview_for(self, layer: Layer, key: str) -> DocOverview:
        exists = self._backend.doc_exists(layer, key)
        if not exists:
            backlog = (
                self._backend.count_trace(key)  # type: ignore[arg-type]
                if layer == "L2"
                else 0
            )
            return DocOverview(
                layer=layer,
                key=key,
                exists=False,
                updated_at=None,
                entry_count=0,
                backlog=backlog,
            )

        mtime = self._backend.doc_mtime(layer, key)
        updated_at_iso = mtime.isoformat() if mtime else None

        md = self._backend.read_doc_text(layer, key)
        try:
            doc = parse(md) if md else Document(title=_default_title(layer, key))
            entry_count = len(doc.all_entries())
        except Exception:
            entry_count = 0

        backlog = 0
        if layer == "L2" and mtime is not None:
            try:
                backlog = self._backend.count_trace(
                    key, since=mtime  # type: ignore[arg-type]
                )
            except Exception:
                backlog = 0

        return DocOverview(
            layer=layer,
            key=key,
            exists=True,
            updated_at=updated_at_iso,
            entry_count=entry_count,
            backlog=backlog,
        )

    # ── Introspection ──────────────────────────────────────────────────────

    @property
    def backend(self) -> MemoryBackend:
        return self._backend


# ── v1 → v2 startup migration ─────────────────────────────────────────────


def migrate_v1_if_needed() -> Path | None:
    """If any v1 memory files are present under the memory root, move the
    whole memory directory's loose files into ``memory/backup/<ts>/``.

    Idempotent: if there's nothing v1-shaped at the root, this is a no-op.

    Returns the backup directory path on migration, or ``None`` otherwise.
    """
    root = paths.memory_root()
    if not root.exists():
        return None
    v1_present = [name for name in _V1_FILES if (root / name).exists()]
    if not v1_present:
        return None

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = paths.backup_root() / ts
    backup_dir.mkdir(parents=True, exist_ok=True)
    for item in list(root.iterdir()):
        if item.name in {"trace", "L2", "L3", "backup"}:
            continue
        try:
            shutil.move(str(item), str(backup_dir / item.name))
        except OSError:
            logger.warning("v1 memory migration: failed to move %s", item, exc_info=True)
    logger.info("v1 memory migrated to %s", backup_dir)
    return backup_dir


def migrate_partner_surface_if_needed() -> bool:
    """Rename the legacy ``tutorbot`` memory surface to ``partner``.

    The partner surface key used to be ``tutorbot`` — so footnote refs read
    ``tutorbot:<id>`` and the consolidator even wrote "tutorbot" into L2
    prose. It is now ``partner``. This moves any on-disk artifacts (L2 doc +
    meta, snapshot dir, trace dir) to the new name, rewrites the ``tutorbot``
    token to ``partner`` inside the L2 doc/meta (both the ``tutorbot:`` ref
    prefix and the bare prose word), and renames the per-surface key inside
    every L3 meta.

    Idempotent: skips any target that already exists; a no-op when nothing
    tutorbot-shaped lives under the memory root.
    """
    import json
    import re

    root = paths.memory_root()
    if not root.exists():
        return False

    moved = False

    l2 = paths.l2_dir()
    old_md, new_md = l2 / "tutorbot.md", l2 / "partner.md"
    if old_md.exists() and not new_md.exists():
        text = old_md.read_text(encoding="utf-8")
        text = text.replace("tutorbot:", "partner:")  # footnote/inline refs
        text = re.sub(r"\btutorbot\b", "partner", text)  # bare prose word
        text = re.sub(r"\bTutorbot\b", "Partner", text)
        new_md.write_text(text, encoding="utf-8")
        old_md.unlink()
        moved = True
    old_meta, new_meta = l2 / "tutorbot.meta.json", l2 / "partner.meta.json"
    if old_meta.exists() and not new_meta.exists():
        text = old_meta.read_text(encoding="utf-8").replace("tutorbot:", "partner:")
        new_meta.write_text(text, encoding="utf-8")
        old_meta.unlink()
        moved = True

    # snapshot/<surface>/ and trace/<surface>/ — plain directory moves
    # (entity ids carry no surface prefix, so no content rewrite needed).
    for sub in ("snapshot", "trace"):
        old_dir, new_dir = root / sub / "tutorbot", root / sub / "partner"
        if old_dir.is_dir() and not new_dir.exists():
            shutil.move(str(old_dir), str(new_dir))
            moved = True

    # L3 metas track seen L2 entry ids per surface — rename that key.
    l3 = paths.l3_dir()
    if l3.is_dir():
        for meta_path in l3.glob("*.meta.json"):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            seen = data.get("seen_l2_entry_ids")
            if isinstance(seen, dict) and "tutorbot" in seen and "partner" not in seen:
                seen["partner"] = seen.pop("tutorbot")
                meta_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                moved = True

    if moved:
        logger.info("migrated legacy 'tutorbot' memory surface to 'partner'")
    return moved


# ── Singleton accessor ────────────────────────────────────────────────────


_singleton: MemoryStore | None = None


def get_memory_store(backend: MemoryBackend | None = None) -> MemoryStore:
    """Return the process-wide :class:`MemoryStore` singleton.

    Passing ``backend`` on the first call wires a custom backend (used for
    testing and future Phase-2/3 rollouts). Subsequent calls return the
    existing singleton — the backend argument is ignored.
    """
    global _singleton
    if _singleton is None:
        _singleton = MemoryStore(backend)
    return _singleton


def reset_memory_store_for_testing() -> None:
    """Reset the singleton. Tests call this in ``setUp``/``tearDown``."""
    global _singleton
    _singleton = None

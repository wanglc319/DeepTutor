"""SOUL.md version history — append-only snapshots + unified diffs.

Storage layout under the partner workspace::

    <workspace>/
      SOUL.md                ← current live file (unchanged from v1)
      soul_versions/
        versions.jsonl       ← every version is one JSON line
        publish_log.jsonl    ← audit log of publish events

Each versions.jsonl record::

    {
      "version": 3,
      "timestamp": "2026-08-08T10:12:33Z",
      "user_id": "admin@office",
      "action": "update",          # create | update | rollback | publish
      "note": "让 Lisa 语气更活泼",
      "content_hash": "sha256:...",
      "previous_version": 2,
      "diff_from_previous": "--- a/SOUL.md\\n+++ b/SOUL.md\\n@@ -3,7 +3,7 @@\\n-...\\n+...",
      "path": "soul_versions/v0003.md"
    }

The full snapshot is stored as ``vNNNN.md`` next to versions.jsonl so
version N can be reconstructed by simply reading the file — no need to
replay diffs. This makes rollback O(1) and eliminates any risk of
patch-format drift over time.

All writes are append-only to the jsonl file (file-level locking via
``fcntl`` on Unix / ``msvcrt`` on Windows) so concurrent edits from
multiple admin UIs can't corrupt the log.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)


VERSIONS_DIR = "soul_versions"
VERSIONS_LOG = "versions.jsonl"
PUBLISH_LOG = "publish_log.jsonl"


@dataclass
class SoulVersion:
    """One immutable snapshot of a partner's SOUL.md."""

    version: int
    timestamp: str
    user_id: str = ""
    username: str = ""
    action: str = ""  # create | update | rollback | publish
    note: str = ""
    content_hash: str = ""
    previous_version: int | None = None
    diff_from_previous: str = ""
    path: str = ""  # relative path of the snapshot file

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PublishEvent:
    """Audit record for a publish / rollback action."""

    version: int
    timestamp: str
    user_id: str = ""
    username: str = ""
    action: str = ""  # publish | rollback | revert
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Path helpers ──────────────────────────────────────────────────────────

def _versions_dir(partner_workspace: Path) -> Path:
    return partner_workspace / VERSIONS_DIR


def _versions_log(partner_workspace: Path) -> Path:
    return _versions_dir(partner_workspace) / VERSIONS_LOG


def _publish_log(partner_workspace: Path) -> Path:
    return _versions_dir(partner_workspace) / PUBLISH_LOG


def _snapshot_path(partner_workspace: Path, version: int) -> Path:
    return _versions_dir(partner_workspace) / f"v{version:04d}.md"


def _ensure_dirs(partner_workspace: Path) -> Path:
    d = _versions_dir(partner_workspace)
    d.mkdir(parents=True, exist_ok=True)
    vlog = _versions_log(partner_workspace)
    if not vlog.exists():
        vlog.touch()
    plog = _publish_log(partner_workspace)
    if not plog.exists():
        plog.touch()
    return d


# ── Low-level append (thread / process safe) ──────────────────────────────

def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    line = json.dumps(obj, ensure_ascii=False, default=str) + "\n"
    try:
        import fcntl  # type: ignore[attr-defined]

        with open(path, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    except (ImportError, OSError):
        try:
            import msvcrt  # type: ignore[attr-defined]

            with open(path, "a", encoding="utf-8") as f:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                except OSError:
                    pass
                f.write(line)
                f.flush()
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        except (ImportError, OSError):
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()


# ── Read helpers ──────────────────────────────────────────────────────────

def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("skipping corrupt line in %s", path)


def list_versions(partner_workspace: Path) -> list[SoulVersion]:
    """Return every known version, oldest first."""
    _ensure_dirs(partner_workspace)
    return [SoulVersion(**row) for row in _iter_jsonl(_versions_log(partner_workspace))]


def get_version(partner_workspace: Path, version: int) -> SoulVersion | None:
    for v in list_versions(partner_workspace):
        if v.version == version:
            return v
    return None


def latest_version_number(partner_workspace: Path) -> int:
    versions = list_versions(partner_workspace)
    return versions[-1].version if versions else 0


def read_snapshot(partner_workspace: Path, version: int) -> str | None:
    path = _snapshot_path(partner_workspace, version)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def list_publish_events(partner_workspace: Path) -> list[PublishEvent]:
    _ensure_dirs(partner_workspace)
    return [PublishEvent(**row) for row in _iter_jsonl(_publish_log(partner_workspace))]


# ── Public API ────────────────────────────────────────────────────────────

def create_snapshot(
    partner_workspace: Path,
    content: str,
    *,
    user_id: str = "system",
    username: str = "",
    action: str = "update",
    note: str = "",
    previous_version: int | None = None,
) -> SoulVersion:
    """Append a new snapshot for ``content``.

    If this is the very first snapshot (no previous_version and no versions
    exist) the action is automatically upgraded to ``"create"``.
    """
    _ensure_dirs(partner_workspace)

    current_latest = latest_version_number(partner_workspace)
    next_version = (previous_version + 1) if previous_version else (current_latest + 1)

    if current_latest == 0 and previous_version is None:
        action = "create"

    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    previous_content: str = ""
    if next_version > 1:
        prev = read_snapshot(partner_workspace, next_version - 1)
        if prev is not None:
            previous_content = prev

    diff = unified_diff(previous_content, content, from_version=next_version - 1, to_version=next_version)

    snapshot_file = _snapshot_path(partner_workspace, next_version)
    snapshot_file.write_text(content, encoding="utf-8")

    record = SoulVersion(
        version=next_version,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        user_id=user_id,
        username=username,
        action=action,
        note=note,
        content_hash=content_hash,
        previous_version=(next_version - 1) if next_version > 1 else None,
        diff_from_previous=diff,
        path=str(snapshot_file.relative_to(_versions_dir(partner_workspace))),
    )
    _append_jsonl(_versions_log(partner_workspace), record.to_dict())
    return record


def rollback_to(
    partner_workspace: Path,
    version: int,
    *,
    user_id: str = "system",
    username: str = "",
    note: str = "手动回滚",
) -> SoulVersion | None:
    """Snapshot the content at ``version`` as a new rollback version.

    Returns the new ``SoulVersion`` that was created (its action is
    ``"rollback"``), or ``None`` if ``version`` doesn't exist.
    """
    content = read_snapshot(partner_workspace, version)
    if content is None:
        return None
    return create_snapshot(
        partner_workspace,
        content,
        user_id=user_id,
        username=username,
        action="rollback",
        note=f"{note} → v{version}",
    )


def record_publish(
    partner_workspace: Path,
    version: int,
    *,
    user_id: str = "system",
    username: str = "",
    action: str = "publish",
    note: str = "",
) -> PublishEvent:
    event = PublishEvent(
        version=version,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        user_id=user_id,
        username=username,
        action=action,
        note=note,
    )
    _ensure_dirs(partner_workspace)
    _append_jsonl(_publish_log(partner_workspace), event.to_dict())
    return event


def unified_diff(a: str, b: str, *, from_version: int | None = None, to_version: int | None = None) -> str:
    """Return a unified diff between two SOUL contents. Empty when equal."""
    if a == b:
        return ""
    from_label = f"v{from_version}" if from_version is not None else "a/SOUL.md"
    to_label = f"v{to_version}" if to_version is not None else "b/SOUL.md"
    return "\n".join(
        difflib.unified_diff(
            a.splitlines(keepends=True),
            b.splitlines(keepends=True),
            fromfile=from_label,
            tofile=to_label,
        )
    )


def diff_between(partner_workspace: Path, v_a: int, v_b: int) -> str:
    a = read_snapshot(partner_workspace, v_a) or ""
    b = read_snapshot(partner_workspace, v_b) or ""
    return unified_diff(a, b, from_version=v_a, to_version=v_b)

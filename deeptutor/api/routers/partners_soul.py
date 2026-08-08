"""SOUL.md version-management REST API.

Endpoints live under ``/api/v1/partners/souls/{partner_id}/...`` to keep
version history discoverable and grouped with the partner they describe.

All mutating endpoints require ``require_admin`` — editing a partner's
soul is a production change, not a casual user action.

Also provides :func:`snapshot_before_write` which the caller hooks into
any SOUL edit so every change is captured automatically before the disk
write happens.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from deeptutor.api.routers.auth import require_admin
from deeptutor.multi_user.context import get_current_user_or_none
from deeptutor.multi_user.paths import local_admin_user
from deeptutor.services.partners.workspace import soul_path
from deeptutor.services.soul import (
    create_snapshot,
    diff_between,
    get_version,
    list_publish_events,
    list_versions,
    record_publish,
    read_snapshot,
    rollback_to,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])


def _partner_workspace(partner_id: str) -> Path:
    """Return the partner workspace root (parent of SOUL.md)."""
    return soul_path(partner_id).parent


def _actor() -> str:
    """Who is calling this endpoint? Falls back to the local admin."""
    user = get_current_user_or_none()
    return (user or local_admin_user()).username or "local-admin"


# ── Schema ──────────────────────────────────────────────────────────────────

class SoulWriteRequest(BaseModel):
    content: str = Field(..., min_length=1, description="Full replacement SOUL.md text")
    note: str = Field(default="", description="Human-readable change description")


class RollbackRequest(BaseModel):
    note: str = Field(default="手动回滚", description="Optional note")


class PublishRequest(BaseModel):
    note: str = Field(default="", description="Optional publish note")


# ── Read endpoints ─────────────────────────────────────────────────────────

@router.get("/souls/{partner_id}/versions")
def list_partner_versions(partner_id: str) -> dict[str, Any]:
    ws = _partner_workspace(partner_id)
    versions = list_versions(ws)
    return {
        "partner_id": partner_id,
        "version_count": len(versions),
        "versions": [v.to_dict() for v in versions],
    }


@router.get("/souls/{partner_id}/versions/{version}")
def get_partner_version(partner_id: str, version: int) -> dict[str, Any]:
    ws = _partner_workspace(partner_id)
    meta = get_version(ws, version)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Version {version} not found")
    content = read_snapshot(ws, version)
    return {
        "partner_id": partner_id,
        "version": meta.to_dict(),
        "content": content,
    }


@router.get("/souls/{partner_id}/versions/{version_a}/diff/{version_b}")
def diff_partner_versions(
    partner_id: str,
    version_a: int,
    version_b: int,
) -> dict[str, Any]:
    ws = _partner_workspace(partner_id)
    a_meta = get_version(ws, version_a)
    b_meta = get_version(ws, version_b)
    if a_meta is None or b_meta is None:
        raise HTTPException(status_code=404, detail="One or both versions not found")
    patch = diff_between(ws, version_a, version_b)
    return {
        "partner_id": partner_id,
        "from": version_a,
        "to": version_b,
        "from_note": a_meta.note,
        "to_note": b_meta.note,
        "diff": patch,
        "empty": not bool(patch),
    }


@router.get("/souls/{partner_id}/versions/publish")
def list_publish_history(partner_id: str) -> dict[str, Any]:
    ws = _partner_workspace(partner_id)
    events = list_publish_events(ws)
    return {
        "partner_id": partner_id,
        "event_count": len(events),
        "events": [e.to_dict() for e in events],
    }


# ── Write endpoints ────────────────────────────────────────────────────────

@router.post("/souls/{partner_id}/versions/{version}/rollback")
def rollback_partner_soul(
    partner_id: str,
    version: int,
    body: RollbackRequest,
) -> dict[str, Any]:
    from deeptutor.services.partners.workspace import read_soul, write_soul

    ws = _partner_workspace(partner_id)
    target_content = read_snapshot(ws, version)
    if target_content is None:
        raise HTTPException(status_code=404, detail=f"Version {version} not found")

    # Snapshot current state *before* we roll back.
    create_snapshot(
        ws,
        read_soul(partner_id) or "",
        user_id=_actor(),
        action="update",
        note=f"回滚前快照 (即将回滚到 v{version})",
    )

    # Actually write the old content back to SOUL.md.
    write_soul(partner_id, target_content)

    # Record the rollback as a new version.
    new_version = rollback_to(ws, version, user_id=_actor(), note=body.note or "手动回滚")
    if new_version is None:
        raise HTTPException(status_code=500, detail="Rollback failed")

    record_publish(
        ws,
        version=new_version.version,
        user_id=_actor(),
        action="rollback",
        note=f"回滚到 v{version}",
    )
    return {
        "partner_id": partner_id,
        "rollback_target": version,
        "new_version": new_version.version,
        "meta": new_version.to_dict(),
    }


@router.post("/souls/{partner_id}/snapshot")
def snapshot_and_publish(partner_id: str, body: SoulWriteRequest) -> dict[str, Any]:
    """Write new SOUL.md content AND create a version snapshot + publish log.

    This is the safe one-shot endpoint used by the conversation-based SOUL
    editor — callers provide the full new content and we atomically write
    + snapshot + record-publish.
    """
    from deeptutor.services.partners.workspace import read_soul, write_soul

    ws = _partner_workspace(partner_id)
    old = read_soul(partner_id) or ""
    new = body.content

    if old == new:
        raise HTTPException(status_code=400, detail="Content unchanged — nothing to snapshot")

    # Snapshot the old state first so the diff is meaningful.
    create_snapshot(
        ws,
        old,
        user_id=_actor(),
        action="update",
        note="本次修改前的状态",
    )

    write_soul(partner_id, new)

    version = create_snapshot(
        ws,
        new,
        user_id=_actor(),
        action="update",
        note=body.note,
    )

    record_publish(
        ws,
        version=version.version,
        user_id=_actor(),
        action="publish",
        note=body.note,
    )
    return {
        "partner_id": partner_id,
        "version": version.version,
        "meta": version.to_dict(),
    }


# ── Hook helper ────────────────────────────────────────────────────────────

def snapshot_before_write(partner_id: str, new_content: str, *, note: str = "") -> None:
    """Create a version snapshot of *new_content* before writing it to disk.

    This is the helper that callers should invoke BEFORE calling
    ``write_soul`` to ensure every edit is captured. Example::

        snapshot_before_write(partner_id, new_content, note="改成活泼语气")
        write_soul(partner_id, new_content)
    """
    from deeptutor.services.partners.workspace import read_soul

    ws = _partner_workspace(partner_id)
    old = read_soul(partner_id) or ""
    if old == new_content:
        return
    create_snapshot(
        ws,
        new_content,
        user_id=_actor(),
        action="update",
        note=note or "unsaved edit",
    )

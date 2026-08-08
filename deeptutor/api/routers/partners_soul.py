"""SOUL.md version-management REST API.

Endpoints live under ``/api/v1/partners/souls/{partner_id}/...`` to keep
version history discoverable and grouped with the partner they describe.

All mutating endpoints require ``require_admin`` — editing a partner's
soul is a production change, not a casual user action.

Every version record stores both ``user_id`` (machine-safe opaque id) and
``username`` (human-readable display name) so audit logs show exactly who
made each change, even if the id format evolves over time.
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
    unified_diff,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])


def _partner_workspace(partner_id: str) -> Path:
    """Return the partner workspace root (parent of SOUL.md)."""
    return soul_path(partner_id).parent


def _actor() -> tuple[str, str]:
    """Return (user_id, username) of the current caller.

    Falls back to the local admin when AUTH_ENABLED=false.
    """
    user = get_current_user_or_none() or local_admin_user()
    return (user.id or "", user.username or "local-admin")


# ── Schema ──────────────────────────────────────────────────────────────────

class SoulWriteRequest(BaseModel):
    content: str = Field(..., min_length=1, description="Full replacement SOUL.md text")
    note: str = Field(default="", description="Human-readable change description")


class RollbackRequest(BaseModel):
    note: str = Field(default="手动回滚", description="Optional note")


class ChatEditRequest(BaseModel):
    """Natural-language SOUL edit request — the core of the conversation editor.

    ``instruction`` is a free-form Chinese sentence describing what to change.
    The backend sends it to the LLM along with the current SOUL.md and gets
    back a complete replacement SOUL.md.

    When ``preview_only=true`` we generate + return the diff preview but do
    NOT snapshot or write to disk — the caller reviews and confirms via
    the regular ``/snapshot`` endpoint.

    When ``preview_only=false`` we snapshot + write atomically (one-shot).
    """

    instruction: str = Field(..., min_length=1, description="自然语言修改意图，例如「把语气改活泼点」")
    preview_only: bool = Field(default=True, description="True=只预览不落盘，False=直接写盘")
    note: str = Field(default="", description="可选，覆盖自动生成的 note")


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
        "from_username": a_meta.username,
        "to_note": b_meta.note,
        "to_username": b_meta.username,
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

    actor_id, actor_name = _actor()
    ws = _partner_workspace(partner_id)
    target_content = read_snapshot(ws, version)
    if target_content is None:
        raise HTTPException(status_code=404, detail=f"Version {version} not found")

    # Snapshot current state *before* we roll back.
    create_snapshot(
        ws,
        read_soul(partner_id) or "",
        user_id=actor_id,
        username=actor_name,
        action="update",
        note=f"回滚前快照 (即将回滚到 v{version})",
    )

    write_soul(partner_id, target_content)

    new_version = rollback_to(
        ws, version,
        user_id=actor_id, username=actor_name,
        note=body.note or "手动回滚",
    )
    if new_version is None:
        raise HTTPException(status_code=500, detail="Rollback failed")

    record_publish(
        ws,
        version=new_version.version,
        user_id=actor_id,
        username=actor_name,
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

    actor_id, actor_name = _actor()
    ws = _partner_workspace(partner_id)
    old = read_soul(partner_id) or ""
    new = body.content

    if old == new:
        raise HTTPException(status_code=400, detail="Content unchanged — nothing to snapshot")

    create_snapshot(
        ws, old,
        user_id=actor_id, username=actor_name,
        action="update",
        note="本次修改前的状态",
    )

    write_soul(partner_id, new)

    version = create_snapshot(
        ws, new,
        user_id=actor_id, username=actor_name,
        action="update",
        note=body.note,
    )

    record_publish(
        ws,
        version=version.version,
        user_id=actor_id,
        username=actor_name,
        action="publish",
        note=body.note,
    )
    return {
        "partner_id": partner_id,
        "version": version.version,
        "meta": version.to_dict(),
    }


@router.post("/souls/{partner_id}/chat-edit")
async def chat_edit_soul(partner_id: str, body: ChatEditRequest) -> dict[str, Any]:
    """对话式修改 SOUL.md — 用户说自然语言，LLM 生成新 SOUL，返回 diff 预览。

    当 preview_only=true 时不写盘、不创建版本，只返回建议内容 + diff。
    前端拿到 diff 高亮展示给用户确认，确认后再调 /snapshot 发布。
    当 preview_only=false 时直接 snapshot + write + publish（同上一个接口）。
    """
    from deeptutor.services.partners.workspace import read_soul, write_soul

    actor_id, actor_name = _actor()
    ws = _partner_workspace(partner_id)

    current = read_soul(partner_id) or ""

    new_content, prompt_used = await _llm_generate_new_soul(partner_id, current, body.instruction)

    if new_content == current:
        raise HTTPException(
            status_code=422,
            detail="LLM 未对 SOUL.md 产生实际变更 — 请换一种更具体的修改说法",
        )

    auto_note = body.note or f"对话式修改：{body.instruction[:60]}"

    patch = unified_diff(current, new_content, from_version=None, to_version=None)

    if body.preview_only:
        return {
            "partner_id": partner_id,
            "mode": "preview",
            "preview_content": new_content,
            "note": auto_note,
            "diff": patch,
            "prompt_used": prompt_used,
            "empty": not bool(patch),
        }

    # ── Write path ──────────────────────────────────────────────────────
    create_snapshot(
        ws, current,
        user_id=actor_id, username=actor_name,
        action="update",
        note="对话式修改前的状态",
    )

    write_soul(partner_id, new_content)

    version = create_snapshot(
        ws, new_content,
        user_id=actor_id, username=actor_name,
        action="update",
        note=auto_note,
    )

    record_publish(
        ws,
        version=version.version,
        user_id=actor_id, username=actor_name,
        action="publish",
        note=auto_note,
    )
    return {
        "partner_id": partner_id,
        "mode": "published",
        "version": version.version,
        "meta": version.to_dict(),
        "note": auto_note,
        "diff": patch,
    }


# ── LLM call for chat-edit ────────────────────────────────────────────────

_SOUL_EDIT_SYSTEM_PROMPT = """你是 SOUL.md 编辑助手。你的唯一任务是根据用户的修改意图，
输出修改后的完整 SOUL.md 内容。

硬性规则：
1. 只输出 SOUL.md 的 markdown 正文，不要任何解释、前言、代码块标记（```）或尾部注释
2. 保持原有的 markdown 结构（标题层级、列表、加粗等），只改被要求改的部分
3. 如果修改意图不明确，保持原样
4. 保留所有没被要求修改的段落——不要自作主张重写整个文件

直接输出 SOUL.md 全文即可。"""


async def _llm_generate_new_soul(
    partner_id: str,
    current_content: str,
    instruction: str,
) -> tuple[str, str]:
    """Call the configured LLM to generate a new SOUL.md from + instruction.

    Returns (new_content, prompt_used_for_debug).
    Falls back to a deterministic rule-based transformer when no LLM is
    configured (so this endpoint is always functional during local dev).
    """
    # Lazy import — LLM wiring is deep and optional.
    try:
        from deeptutor.services.partners.model_runtime import resolve_partner_llm_config
        cfg = resolve_partner_llm_config(partner_id)
        provider_name = (cfg.provider or "").strip()
        model_name = (cfg.model or "").strip()

        if provider_name and model_name:
            new_content = await _call_llm(
                provider_name=provider_name,
                model_name=model_name,
                current_soul=current_content,
                instruction=instruction,
            )
            if new_content and new_content.strip():
                return new_content.strip(), f"{provider_name}/{model_name}"
    except Exception:
        logger.exception("LLM path failed for SOUL chat-edit — falling back to rule-based")

    # ── Fallback: rule-based transformer ─────────────────────────────────
    return _rule_based_edit(current_content, instruction), "fallback:rule-based"


async def _call_llm(
    *,
    provider_name: str,
    model_name: str,
    current_soul: str,
    instruction: str,
) -> str:
    """Low-level LLM chat call — thin wrapper over provider registry."""
    from deeptutor.services.provider_registry import find_by_name

    spec = find_by_name(provider_name)
    if spec is None:
        raise RuntimeError(f"Provider '{provider_name}' not registered")

    # Build messages payload
    user_prompt = (
        f"# 当前 SOUL.md\n\n```markdown\n{current_soul}\n```\n\n"
        f"# 修改意图\n\n{instruction}\n\n"
        f"# 输出要求\n\n直接输出修改后的 SOUL.md 全文，不要任何其他文字。"
    )

    # Different providers expose different client shapes; dispatch by name.
    if "openai" in provider_name.lower() or "doubao" in provider_name.lower() or "ark" in provider_name.lower():
        return await _call_openai_compatible(
            spec.base_url or "",
            spec.api_key or "",
            model_name,
            user_prompt,
        )
    if "qwen" in provider_name.lower() or "dashscope" in provider_name.lower():
        return await _call_openai_compatible(
            spec.base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1",
            spec.api_key or "",
            model_name,
            user_prompt,
        )

    # Generic OpenAI-compatible last resort
    return await _call_openai_compatible(
        spec.base_url or "",
        spec.api_key or "",
        model_name,
        user_prompt,
    )


async def _call_openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
    user_prompt: str,
) -> str:
    """Call any OpenAI-compatible /chat/completions endpoint via httpx."""
    import httpx

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": _SOUL_EDIT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        r = await client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    return data["choices"][0]["message"]["content"]


def _rule_based_edit(current: str, instruction: str) -> str:
    """Deterministic fallback when no LLM is configured.

    Not smart at all — it just tries a couple of common edits (prepend /
    append a remark). This keeps the endpoint functional locally so the
    UI can be developed against it even without an API key.
    """
    needle = instruction.strip()
    remark = f"\n\n> 💡 自动修改标记：{needle}\n"
    if remark in current:
        return current  # already applied
    return current.rstrip() + remark


# ── Hook helper ────────────────────────────────────────────────────────────

def snapshot_before_write(partner_id: str, new_content: str, *, note: str = "") -> None:
    """Create a version snapshot of *new_content* before writing it to disk.

    This is the helper that callers should invoke BEFORE calling
    ``write_soul`` to ensure every edit is captured. Example::

        snapshot_before_write(partner_id, new_content, note="改成活泼语气")
        write_soul(partner_id, new_content)
    """
    from deeptutor.services.partners.workspace import read_soul

    actor_id, actor_name = _actor()
    ws = _partner_workspace(partner_id)
    old = read_soul(partner_id) or ""
    if old == new_content:
        return
    create_snapshot(
        ws, new_content,
        user_id=actor_id, username=actor_name,
        action="update",
        note=note or "unsaved edit",
    )

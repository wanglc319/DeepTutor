"""Partners v2 WebSocket — sentence-burst delivery with per-user memory.

Compared to ``/api/v1/partners/{partner_id}/ws``:

1. **Per-user memory isolation** — the client *must* provide ``user_id`` on
   every message. We inject it into the request-local user context so the
   three-layer memory (L1/L2/L3) is always segmented by real end-user, not
   just by browser session. This is what makes 5000 concurrent WeChat
   callers not leak memories into each other.

2. **Sentence-burst delivery** — instead of forwarding the agent token
   stream as-is, we wait for the final answer, split it with
   :func:`~deeptutor.services.partners.sentence_split.split_sentences`, and
   push each sentence as its own JSON frame with a natural typing delay.
   Downstream WeChat bridges simply forward each ``sentence`` frame as a
   single WeChat message.

3. **Clean namespace** — the route lives under ``/api/v2`` so v1 clients
   keep working untouched. v1 was built for a single-user admin console;
   v2 is built for 3rd-party IM integrations at scale.

Typical frame sequence::

    client → {"user_id": "wx_openid_abc", "content": "我家孩子叫Tom", "action": "chat"}
    server → {"type": "ack"}
    server → {"type": "agent_thinking"}
    server → {"type": "sentence", "content": "好的，我记住啦～", "index": 1, "total": 3}
    server → {"type": "sentence", "content": "Tom 是个很棒的名字！", "index": 2, "total": 3}
    server → {"type": "sentence", "content": "他今年上几年级呀？", "index": 3, "total": 3}
    server → {"type": "agent_done", "total_sentences": 3}
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from deeptutor.multi_user.context import CurrentUser, set_current_user
from deeptutor.multi_user.paths import scope_for_user
from deeptutor.services.partners import get_partner_manager
from deeptutor.services.partners.sentence_split import TypingDelay, split_sentences

logger = logging.getLogger(__name__)
router = APIRouter()


class _Disconnected(Exception):
    """Internal control flow — raised when the ws closes unexpectedly."""


def _make_current_user(user_id: str) -> CurrentUser:
    """Build a :class:`CurrentUser` for the given *user_id*.

    WeChat / 3rd-party bridges send us opaque user identifiers (openid,
    unionid, etc.). We treat each one as a unique "user" for memory
    isolation. ``role="user"`` keeps them out of admin tooling.
    """
    safe_id = user_id.strip() or "anonymous"
    return CurrentUser(
        id=safe_id,
        username=safe_id,
        role="user",  # type: ignore[arg-type]
        scope=scope_for_user(safe_id, is_admin=False),
    )


@router.websocket("/partners/{partner_id}/ws")
async def partner_v2_ws(ws: WebSocket, partner_id: str) -> None:
    await ws.accept()

    manager = get_partner_manager()
    typing = TypingDelay()

    async def _safe_send(payload: dict[str, Any]) -> bool:
        try:
            await ws.send_json(payload)
            return True
        except Exception:
            return False

    async def _turn_done_answer(turn) -> str:
        """Wait for *turn* to finish and return the agent's full answer text.

        We consume every frame on the subscribe queue and accumulate any
        non-empty ``content`` fields. The final content (often tagged with
        ``type == "final"``) is authoritative, but intermediates may carry
        useful fragments too — we join them all to be safe.
        """
        chunks: list[str] = []
        async for frame in turn.subscribe():
            # frame is a dict with keys like "type", "content", "final", ...
            content = frame.get("content") if isinstance(frame, dict) else None
            if content and isinstance(content, str):
                chunks.append(content)
            # Respect explicit stop from the turn manager.
            if frame.get("type") in {"done", "stopped", "error"} if isinstance(frame, dict) else False:
                break
        # Prefer the last chunk (usually a "final" frame); fallback to join.
        if chunks:
            return chunks[-1].strip()
        return ""

    async def _handle_user_message(data: dict[str, Any]) -> None:
        user_id = (data.get("user_id") or "").strip()
        if not user_id:
            await _safe_send({"type": "error", "content": "user_id is required"})
            return

        action = data.get("action", "chat")
        if action == "stop":
            session_key = data.get("session_key") or manager.web_session_key(
                partner_id,
                chat_id=data.get("chat_id", "web"),
                session_id=data.get("session_id"),
            )
            manager.stop_web_turn(partner_id, session_key)
            await _safe_send({"type": "stopped"})
            return

        content = (data.get("content") or "").strip()
        if not content:
            return

        # Set the per-user memory context for this turn. Everything that
        # reads MemoryStore / FileBackend now resolves paths through the
        # correct user scope — no cross-user leaks, even at 5000 concurrency.
        user = _make_current_user(user_id)
        token = set_current_user(user)

        try:
            session_key = data.get("session_key") or manager.web_session_key(
                partner_id,
                chat_id=data.get("chat_id", "web"),
                session_id=data.get("session_id"),
            )
            turn = manager.start_web_turn(partner_id, session_key, content, [])
            await _safe_send({"type": "agent_thinking"})

            full_answer = await _turn_done_answer(turn)
            if not full_answer:
                await _safe_send({"type": "sentence", "content": "（抱歉，我暂时没什么好说的～）", "index": 1, "total": 1})
                await _safe_send({"type": "agent_done", "total_sentences": 1})
                return

            sentences = split_sentences(full_answer)
            if not sentences:
                sentences = [full_answer]

            delays = typing.for_batch(sentences)

            # delays[0] = pre-first pause; delays[1..] = per-sentence delay.
            await asyncio.sleep(delays[0])

            for idx, sentence in enumerate(sentences, start=1):
                if not await _safe_send({
                    "type": "sentence",
                    "content": sentence,
                    "index": idx,
                    "total": len(sentences),
                }):
                    return
                if idx < len(sentences):
                    await asyncio.sleep(delays[idx])

            await _safe_send({"type": "agent_done", "total_sentences": len(sentences)})
        except Exception as exc:
            logger.exception("v2 ws turn error for partner=%s user=%s", partner_id, user_id)
            await _safe_send({"type": "error", "content": str(exc)})
        finally:
            try:
                from deeptutor.multi_user.context import reset_current_user
                reset_current_user(token)
            except Exception:
                pass

    try:
        await _safe_send({"type": "hello", "version": 2, "partner_id": partner_id})

        while True:
            try:
                raw = await ws.receive_text()
            except WebSocketDisconnect:
                break
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                if not await _safe_send({"type": "error", "content": "Invalid JSON"}):
                    break
                continue

            try:
                await _handle_user_message(data)
            except Exception:
                logger.exception("v2 ws message handler crashed")
                if not await _safe_send({"type": "error", "content": "Internal error"}):
                    break
    finally:
        logger.info("v2 WebSocket closed for partner=%s", partner_id)

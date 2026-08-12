"""
WebSocket v2 — 逐句打字机流式协议（简化版）
=============================================

端点（2 个等价，任选）::

    ws://127.0.0.1:8001/api/v2/ws/chat                              # partner 走默认值或信封里带
    ws://127.0.0.1:8001/api/v2/ws/partners/{partner_id}/chat      # partner_id 写在 URL 里（推荐）

Client → Server —— 只发用户消息，最简信封::

    { "content": "三年级小朋友不爱学习英语" }   ← 就这一行也行！

完整兼容格式（可选，字段全有默认值）::

    {
        "partner_id": "lisa",
        "chat_id":    "c2c_user42",          // 可选，留空自动生成
        "user_id":    "user42",              // 可选，留空自动生成
        "content":    "三年级小朋友不爱学习英语"
    }

旧飞书格式也兼容（带 schema/header/event 那套）——不删，老客户端不炸。

Server → Client（event_type 不变，保持统一 schema）::

    { "type": "ack",     "message_id": "m1", "status": "accepted" }
    { "type": "patch",   "message_id": "m1", "seq": 1, "content": "三年级确实是个坎儿..." }
    { "type": "patch",   "message_id": "m1", "seq": 2, "content": "您家宝贝具体是哪种表现呀？" }
    { "type": "finish",  "message_id": "m1", "content": "...完整全文...", "finish_reason": "stop" }
    { "type": "error",   "message_id": "m1", "code": "...", "msg": "..." }

延迟策略:
    每句 ``max(1000ms, 字数 × 150ms)`` ± 12% 抖动，保证至少 1 秒、约 6~7 字/秒（比正常打字快一倍）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
logger = logging.getLogger(__name__)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?\n；;])")
# 英文句号/叹号/问号后面跟空格或引号也算一句结尾（避免 a.b.c. 这种首字母缩写被误切）
_EN_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'“‘\(【《])")

# 句间打字机延迟范围（毫秒）
_TYPING_MIN_MS = 120
_TYPING_MAX_MS = 280


# ───────────────────────── helpers ─────────────────────────────────────

async def _wait_json(ws: WebSocket, timeout: float = 90.0) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.receive_text(), timeout=timeout)
    return json.loads(raw)


def _extract_user_text(envelope: dict[str, Any]) -> tuple[str, str, str, str]:
    """从任意格式的 envelope 里抠出 (message_id, user_id, chat_id, user_text)。

    支持的输入格式（越靠前越精简）::

        # 极简格式（只一行也行）
        { "content": "xxx" }

        # 简化信封
        { "chat_id": "c1", "user_id": "u1", "content": "xxx" }

        # 旧飞书格式（完整兼容）
        { "schema":"2.0","header":{...},
          "event":{"message":{"content":"{\"text\":\"xxx\"}"},
                   "sender":{"sender_id":{"open_id":"u1"}}}}

        # 旧格式也兼容（向后兼容）
        { "text": "xxx" }

    任何字段缺省都会自动生成，客户端没理由传错。
    """
    import uuid

    event = envelope.get("event") or envelope
    msg = event.get("message") or {}

    # ── message_id ──
    message_id = str(msg.get("message_id") or msg.get("id") or envelope.get("message_id") or "").strip()
    if not message_id:
        message_id = f"m_{uuid.uuid4().hex[:12]}"

    # ── user_id ── 优先 user_id，其次旧 open_id，最后自动生成
    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") or {}
    user_id = str(
        envelope.get("user_id")
        or event.get("user_id")
        or sender.get("user_id")
        or sender_id.get("user_id")
        or sender_id.get("open_id")          # 兼容旧格式
        or ""
    ).strip()
    if not user_id:
        user_id = f"anon_{uuid.uuid4().hex[:6]}"

    # ── chat_id ──
    chat_id = str(
        msg.get("chat_id")
        or msg.get("session_id")
        or envelope.get("chat_id")
        or event.get("chat_id")
        or ""
    ).strip()

    # ── user_text ──
    text = ""
    content_raw = msg.get("content") or envelope.get("content") or ""
    if isinstance(content_raw, dict):
        text = str(content_raw.get("text") or "")
    elif isinstance(content_raw, str) and content_raw.startswith("{"):
        try:
            text = str(json.loads(content_raw).get("text") or "")
        except Exception:
            text = content_raw
    else:
        text = str(content_raw or "")
    # 顶层 content / text / message 兜底（content 优先，text 做向后兼容）
    if not text:
        text = str(envelope.get("content") or envelope.get("text") or envelope.get("message") or event.get("content") or event.get("text") or "")

    return message_id, user_id, chat_id, text.strip()


def _split_sentences(full: str) -> list[str]:
    """把完整回复拆成"句/段"，每段一条 patch。

    策略:
      1. 先按 ``\n\n`` 空行切段（Lisa 这类 partner 常用空行分段表达不同话题）。
      2. 再对剩余段落按句末标点（。！？!?；;）和英文句号细切。
      3. 保证返回列表不空、每项至少 1 个字符。
    """
    if not full:
        return []
    raw_parts: list[str] = []
    for para in full.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        # 段落本身就是一句（以句末标点结尾）→ 直接当一条
        if _ENDS_WITH_PUNCT.search(para) or len(para) <= 30:
            raw_parts.append(para)
            continue
        # 段落较长、内部还有多个句号/问号 → 再细切
        for sub in _SENTENCE_SPLIT_RE.split(para):
            for subsub in _EN_SPLIT_RE.split(sub):
                subsub = subsub.strip()
                if subsub:
                    raw_parts.append(subsub)
    # 最后一层兜底：按单个换行切
    out: list[str] = []
    for p in raw_parts:
        if "\n" in p and not _ENDS_WITH_PUNCT.search(p):
            for line in p.split("\n"):
                line = line.strip()
                if line:
                    out.append(line)
        else:
            out.append(p)
    return out


_ENDS_WITH_PUNCT = re.compile(r"[。！？!?；;…~]$")


def _typing_delay_ms(sentence: str) -> int:
    """按句长决定间隔：至少 1 秒，约 6~7 字/秒（比正常打字快一倍）。"""
    n = max(1, len(sentence or ""))
    base = max(1000, int(n * 150))                  # 每字 150ms，封顶不低于 1s
    jitter = random.randint(-int(base * 0.12), int(base * 0.12))
    return max(1000, base + jitter)


async def _maybe_run_sales_intent(user_id: str, user_text: str) -> str | None:
    """委托给 sales.service.maybe_run_sales_intent 公共入口."""
    from deeptutor.sales.service import maybe_run_sales_intent as _maybe
    return await _maybe(user_id, user_text)


# ───────────────────────── event builders ──────────────────────────────

_SCHEMA = "2.0"


def _ack(message_id: str, chat_id: str, turn_id: str) -> dict[str, Any]:
    return {
        "type": "ack",
        "message_id": message_id,
        "status": "accepted",
        "turn_id": turn_id,
        "chat_id": chat_id,
    }


def _patch(message_id: str, seq: int, sentence_only: str) -> dict[str, Any]:
    """独立句 patch —— 每次只推这一句本身，不带之前的内容。"""
    return {
        "type": "patch",
        "message_id": message_id,
        "seq": seq,
        "content": sentence_only,
    }


def _finish(message_id: str, content: str, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "type": "finish",
        "message_id": message_id,
        "content": content,
        "finish_reason": finish_reason,
    }


def _err(message_id: str, code: str, msg: str) -> dict[str, Any]:
    return {
        "type": "error",
        "message_id": message_id,
        "code": code,
        "msg": msg,
    }


# ───────────────────────── main handler ────────────────────────────────

async def _ws_loop(ws: WebSocket, default_partner_id: str = "lisa") -> None:
    """公共 WS 消息循环。两个端点（/chat 和 /partners/{pid}/chat）都复用它。"""
    from deeptutor.api.routers.auth import ws_auth_failed, ws_require_auth
    from deeptutor.multi_user.context import reset_current_user

    user_token = await ws_require_auth(ws)
    if user_token is ws_auth_failed:
        return

    await ws.accept()
    closed = False
    active_task: asyncio.Task[None] | None = None

    async def safe_send(data: dict[str, Any]) -> None:
        nonlocal closed
        if closed:
            return
        try:
            await ws.send_text(json.dumps(data, ensure_ascii=False, default=str))
        except Exception:
            closed = True

    try:
        while not closed:
            try:
                envelope = await _wait_json(ws, timeout=120)
            except asyncio.TimeoutError:
                await safe_send({"schema": _SCHEMA, "header": {"event_type": "im.ping_v1"}})
                continue
            except WebSocketDisconnect:
                break

            hdr = envelope.get("header") or {}
            ev_type = hdr.get("event_type") or envelope.get("type") or ""

            # 消息类型判定: 显式标记 or 信封里有 content/text 字段就当作"发消息"
            is_message = (
                ev_type in ("im.message.receive_v1", "message", "start_turn", "im.message", "im.message_v1")
                or bool(envelope.get("content"))
                or bool(envelope.get("text"))
                or bool((envelope.get("event") or {}).get("message"))
            )

            if is_message:
                msg_id, user_id, chat_id, user_text = _extract_user_text(envelope)
                if not user_text:
                    await safe_send(_err(msg_id or "?", "EMPTY_TEXT", "message text is empty"))
                    continue

                # partner_id 优先级: 路由参数 > envelope 显式传 > 默认值
                partner_id = str(
                    envelope.get("partner_id")
                    or (envelope.get("event") or {}).get("partner_id")
                    or default_partner_id
                ).strip()

                # chat_id 留空就用 user_id 关联，保证一个用户在一个 partner 下只有一个会话
                if not chat_id:
                    chat_id = f"c2c_{partner_id}_{user_id}"

                if active_task and not active_task.done():
                    active_task.cancel()

                active_task = asyncio.create_task(
                    _handle_turn(ws, safe_send, partner_id, chat_id, msg_id, user_id, user_text)
                )
                continue

            if ev_type in ("im.ping_v1", "ping"):
                await safe_send({"type": "pong"})
                continue

            if ev_type in ("im.cancel_v1", "cancel_turn"):
                if active_task and not active_task.done():
                    active_task.cancel()
                continue

            if ev_type in ("im.close_v1",):
                break

            await safe_send(_err("?", "UNKNOWN_EVENT", f"unknown event_type: {ev_type}"))

    except WebSocketDisconnect:
        logger.debug("v2/ws client disconnected")
    except Exception as exc:
        logger.exception("v2/ws error")
        try:
            await safe_send(_err("?", "INTERNAL", str(exc)[:200]))
        except Exception:
            pass
    finally:
        closed = True
        if active_task and not active_task.done():
            active_task.cancel()
        if user_token is not None:
            reset_current_user(user_token)


# 端点 A: 客户端信封里带 partner_id（默认 lisa）
@router.websocket("/chat")
async def ws_v2_chat(ws: WebSocket) -> None:
    await _ws_loop(ws, default_partner_id="lisa")


# 端点 B: 路由参数传 partner_id —— 和 REST /api/v1/partners/{partner_id}/chat 对齐
@router.websocket("/partners/{partner_id}/chat")
async def ws_v2_chat_partner(ws: WebSocket, partner_id: str) -> None:
    await _ws_loop(ws, default_partner_id=partner_id.strip() or "lisa")


# ───────────────────────── turn runner ──────────────────────────────────

async def _handle_turn(
    ws: WebSocket,
    safe_send,
    partner_id: str,
    chat_id: str,
    message_id: str,
    user_id: str,
    user_text: str,
) -> None:
    """走 partner_manager.send_message 路径，拿到完整回复后逐句打字机推送。

    这样才能正确加载 partner 的 SOUL.md / persona / capability，
    和 REST ``POST /api/v1/partners/{partner_id}/chat`` 完全一致。
    """
    from deeptutor.api.routers.partners import _ensure_running_partner
    from deeptutor.services.partners import get_partner_manager

    session_id = chat_id or f"c2c_{partner_id}_{user_id}"

    # 1) ack
    await safe_send(_ack(message_id, session_id, ""))

    # 2) 确保 partner 在跑
    try:
        await _ensure_running_partner(partner_id)
    except Exception as exc:
        await safe_send(_err(message_id, "PARTNER_DOWN", str(exc)[:200]))
        return

    # 3) 调用 partner_manager 拿完整回复（这里会自动套用 SOUL.md + persona）
    try:
        mgr = get_partner_manager()
        final_text = await mgr.send_message(
            partner_id,
            user_text,
            chat_id=session_id,
            session_id=session_id,
            session_key=f"{partner_id}:{user_id}",   # 让同一用户在同一 partner 下独立 session
        )
    except asyncio.CancelledError:
        await safe_send(_err(message_id, "CANCELLED", "turn cancelled by client"))
        return
    except Exception as exc:
        logger.exception("v2/ws partner send_message failed")
        await safe_send(_err(message_id, "SEND_FAILED", str(exc)[:200]))
        return

    final_text = (final_text or "").strip()
    if not final_text:
        await safe_send(_err(message_id, "EMPTY_REPLY", "partner returned empty text"))
        return

    # ── 销售意向度打分（可选，环境变量 SALES_INTENT_ENABLED=true 启用） ──
    sales_action_text = await _maybe_run_sales_intent(user_id, user_text)
    if sales_action_text:
        final_text = final_text.rstrip() + sales_action_text

    # 4) 逐句打字机推送 —— 独立句，每条只含当前这一句
    sentences = _split_sentences(final_text)
    seq = 0
    for s in sentences:
        seq += 1
        await safe_send(_patch(message_id, seq, s))
        await asyncio.sleep(_typing_delay_ms(s) / 1000.0)

    # 兜底：整段一起推
    if seq == 0:
        await safe_send(_patch(message_id, 1, final_text))

    # 5) finish
    await safe_send(_finish(message_id, final_text, "stop"))

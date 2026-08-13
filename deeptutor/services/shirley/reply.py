"""
回复通知域 —— Shirley MCP 6

  - notify_lisa_reply → reply_lisa_message
    写操作，会真实发送企微消息；同一回复不要重复调用。
    msgType: 1=文本, 119=转人工标识。
"""

from __future__ import annotations

import logging
from typing import Any

from ._client import _call_tool

logger = logging.getLogger(__name__)

MSG_TYPE_TEXT = 1
MSG_TYPE_HUMAN_HANDOVER = 119


async def notify_lisa_reply(
    third_sale_uuid: str,
    third_user_id: int,
    answer: str,
    session_id: str | None = None,
    msg_type: int = MSG_TYPE_TEXT,
) -> dict[str, Any] | None:
    """通知 Sales 将回复发送给对应企微客户。

    Args:
        third_sale_uuid: Sales 账号 uuid
        third_user_id:   第三方系统客户 ID（正整数）
        answer:          要发送的文本内容或媒体 URL
        session_id:      Lisa 会话 ID（可空）
        msg_type:        1=文本 / 119=转人工

    Returns:
        Sales 发送结果 { msgId, serverId, sender, receiver, ... } 或 None
    """
    if not third_sale_uuid or not third_user_id or not answer:
        return None
    args: dict[str, Any] = {
        "thirdSaleUuid": third_sale_uuid,
        "thirdUserId": third_user_id,
        "msgType": msg_type,
        "answer": answer,
    }
    if session_id:
        args["sessionId"] = session_id
    try:
        return await _call_tool("reply_lisa_message", args)
    except Exception as e:
        logger.warning("Shirley notify_lisa_reply failed: %s", e)
        return None

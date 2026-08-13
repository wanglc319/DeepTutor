"""
聊天历史域 —— Shirley MCP 3

  - query_sales_chat_history → query_user_sales_chat_history
    参数是销售视角: vid / uuid / qywxUserid / userId / limit / offset
"""

from __future__ import annotations

import logging
from typing import Any

from ._client import _call_tool

logger = logging.getLogger(__name__)


async def query_sales_chat_history(
    vid: int,
    uuid: str,
    qywx_userid: str,
    user_id: int,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any] | None:
    """调用 Shirley MCP query_user_sales_chat_history 拉取销售与用户的聊天记录。

    返回 { list: [...], seq: int }，list 为空不代表失败（销售和该用户暂无聊天）。
    分页: 返回的 seq 可作为下一次 offset。
    """
    try:
        return await _call_tool("query_user_sales_chat_history", {
            "vid": vid,
            "uuid": uuid,
            "qywxUserid": qywx_userid,
            "userId": user_id,
            "limit": max(1, min(100, limit)),
            "offset": offset,
        })
    except Exception as e:
        logger.warning("Shirley query_sales_chat_history failed: %s", e)
        return None

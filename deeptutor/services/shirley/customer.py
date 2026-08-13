"""
客户标签域 —— Shirley MCP 1

  - mark_qywx_tags → mark_qywx_customer_tags
    写操作，会真实修改企微客户标签，仅在用户明确要求打标签时调用。
"""

from __future__ import annotations

import logging
from typing import Any

from ._client import _call_tool

logger = logging.getLogger(__name__)


async def mark_qywx_tags(
    corpid: str,
    external_userid: str,
    follow_userid: str,
    tag_ids: list[str],
) -> dict[str, Any] | None:
    """给企微客户添加标签。写操作，会真实修改客户标签。

    参数都是企微真实 ID，不能用手机号/昵称/标签名代替。
    返回 { success: bool, message: str } 或 None。
    """
    if not corpid or not external_userid or not follow_userid or not tag_ids:
        return None
    try:
        return await _call_tool("mark_qywx_customer_tags", {
            "corpid": corpid,
            "externalUserid": external_userid,
            "followUserid": follow_userid,
            "tagIds": tag_ids,
        })
    except Exception as e:
        logger.warning("Shirley mark_qywx_tags failed: %s", e)
        return None

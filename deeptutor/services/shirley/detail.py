"""
客户详情域 —— Shirley MCP 7

  - get_qywx_detail → get_qywx_external_detail_v2
    只读，返回 Sales V2 客户详情（基本资料/标签/服务员工/聊天/订单）。
"""

from __future__ import annotations

import logging
from typing import Any

from ._client import _call_tool

logger = logging.getLogger(__name__)


async def get_qywx_detail(third_user_id: int) -> dict[str, Any] | None:
    """查询 Sales 服务中的企微客户详情。

    返回客户详情 dict（含 id/userid/corpid/unionid/name/avatar/tags/follows/mobile/orders 等），
    或 None（客户不存在 / 工具不可用 / 网络错误）。
    """
    if not third_user_id:
        return None
    try:
        return await _call_tool("get_qywx_external_detail_v2", {
            "thirdUserId": third_user_id,
        })
    except Exception as e:
        logger.warning("Shirley get_qywx_detail failed: %s", e)
        return None

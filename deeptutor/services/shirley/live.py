"""
直播域 —— Shirley MCP 4

  - get_mantis_live_link → get_mantis_live_link
    生成螳螂官方直播链接（pageUrl / shortUrl），支持分享人专属链接。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from ._client import _call_tool

logger = logging.getLogger(__name__)

Scene = Literal["SITE_COURSE", "CAMP_COURSE"]
LinkType = Literal["LINK", "SHORT", "LINK_AND_SHORT"]


async def get_mantis_live_link(
    live_id: int,
    scene: Scene,
    domain: str,
    corp_id: str | None = None,
    share_user_id: str | None = None,
    link_type: LinkType = "LINK",
) -> dict[str, Any] | None:
    """获取螳螂官方直播链接。

    Args:
        live_id:       螳螂直播房间号 liveNum（正整数）
        scene:         SITE_COURSE 小课直播 / CAMP_COURSE 训练营直播
        domain:        访问域名，不含协议和路径（如 "xl.shirleyclass.com"）
        corp_id:       企微 corpId/aliasId；多企微且指定分享人时必填
        share_user_id: 分享人账号，传入后生成该分享人的专属直播链接
        link_type:     LINK / SHORT / LINK_AND_SHORT，默认 LINK

    Returns:
        { pageUrl, shortUrl?, shortUrlExpireTime? } 或 None
    """
    if not live_id or not scene or not domain:
        return None
    args: dict[str, Any] = {
        "liveId": live_id,
        "scene": scene,
        "domain": domain,
        "linkType": link_type,
    }
    if corp_id:
        args["corpId"] = corp_id
    if share_user_id:
        args["shareUserId"] = share_user_id
    try:
        return await _call_tool("get_mantis_live_link", args)
    except Exception as e:
        logger.warning("Shirley get_mantis_live_link failed: %s", e)
        return None

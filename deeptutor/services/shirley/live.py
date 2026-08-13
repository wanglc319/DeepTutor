"""
直播 Skill —— Shirley MCP 4

  get_mantis_live_link  → 生成螳螂官方直播链接

业务侧便捷入口: get_live_url_from_env() 自动从环境变量取配置（live_id/domain/scene），
MCP 不可用时返回 None，调用方自行降级（mock URL 或 env 变量）。

环境变量:
  SHIRLEY_LIVE_ID       螳螂直播房间号（正整数，必填）
  SHIRLEY_LIVE_DOMAIN   访问域名，默认 xl.shirleyclass.com
  SHIRLEY_LIVE_SCENE    SITE_COURSE（默认）或 CAMP_COURSE
  SHIRLEY_LIVE_CORP_ID  企微 corpId（可选，多企微时指定）
  SHIRLEY_LIVE_SHARE_UID 分享人账号（可选，生成专属链接）
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from ._client import _call_tool

logger = logging.getLogger(__name__)

Scene = Literal["SITE_COURSE", "CAMP_COURSE"]
LinkType = Literal["LINK", "SHORT", "LINK_AND_SHORT"]

DEFAULT_DOMAIN = "xl.shirleyclass.com"
DEFAULT_SCENE: Scene = "SITE_COURSE"


async def get_mantis_live_link(
    live_id: int,
    scene: Scene,
    domain: str,
    corp_id: str | None = None,
    share_user_id: str | None = None,
    link_type: LinkType = "LINK",
) -> dict[str, Any] | None:
    """获取螳螂官方直播链接。

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


async def get_live_url_from_env() -> str | None:
    """便捷入口 —— 从环境变量读配置，调 MCP 拿直播链接。

    Returns:
        直播 pageUrl 字符串（MCP 返回第一个），或 None（MCP 不可用 / 配置缺失）。
    """
    live_id_str = os.getenv("SHIRLEY_LIVE_ID", "").strip()
    if not live_id_str:
        logger.info("SHIRLEY_LIVE_ID 未配置，跳过 MCP 直播链接获取")
        return None
    try:
        live_id = int(live_id_str)
    except ValueError:
        logger.warning("SHIRLEY_LIVE_ID 必须是整数，当前值: %s", live_id_str)
        return None
    domain = os.getenv("SHIRLEY_LIVE_DOMAIN", DEFAULT_DOMAIN).strip() or DEFAULT_DOMAIN
    scene = os.getenv("SHIRLEY_LIVE_SCENE", DEFAULT_SCENE).strip() or DEFAULT_SCENE
    if scene not in ("SITE_COURSE", "CAMP_COURSE"):
        scene = DEFAULT_SCENE
    corp_id = os.getenv("SHIRLEY_LIVE_CORP_ID", "").strip() or None
    share_uid = os.getenv("SHIRLEY_LIVE_SHARE_UID", "").strip() or None

    result = await get_mantis_live_link(
        live_id=live_id,
        scene=scene,  # type: ignore[arg-type]
        domain=domain,
        corp_id=corp_id,
        share_user_id=share_uid,
    )
    if not result:
        return None
    for key in ("pageUrl", "page_url", "url"):
        val = result.get(key)
        if isinstance(val, str) and val:
            return val
    short_url = result.get("shortUrl") or result.get("short_url")
    if isinstance(short_url, str) and short_url:
        return short_url
    return None

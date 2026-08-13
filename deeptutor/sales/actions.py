"""
动作决策器
========

根据温度档 + 追问次数，决定 AI 回复里要追加什么主动动作。

直播链接获取策略（优先级从高到低）:
  1. Shirley MCP get_mantis_live_link（通过 get_live_url_from_env）
  2. 环境变量 SALES_LIVE_URL（手动注入覆盖）
  3. 内置 mock URL（纯测试 / MCP 不可用时降级）
"""
from __future__ import annotations

import logging
import os

from .schemas import (
    CustomerProfile,
    TEMP_BLAZING, TEMP_HOT, TEMP_WARM, TEMP_COOL, TEMP_COLD,
    DAY_GATE_3, DAY_GATE_7, DAY_GATE_10,
)
from .temperature import compute_time_factor, compute_stop_loss

logger = logging.getLogger(__name__)

# mock URL —— 纯测试 / MCP 不可用时的最后降级
# 生产环境应通过 SHIRLEY_LIVE_ID 让 MCP 真正调螳螂接口
_FALLBACK_LIVE_URL = "https://xl.shirleyclass.com/s/64Y8vGI"

# 推送门槛：delivery_q_count（追问标签触发次数）>= 2 时触发直播邀约
MIN_DELIVERY_Q_COUNT_FOR_LIVE = 2


def _grade_hint(profile: CustomerProfile) -> str:
    g = profile.profile.get("grade") or ""
    if g:
        return f"{g} 专属"
    return "全年级"


def decide_next_action(profile: CustomerProfile) -> str:
    """根据温度档 + 追问次数判定 next_action.

    直播推送门槛：delivery_q_count >= 2.
    极热档（q>=4）优先走临门一脚，不再推直播。
    """
    temp = profile.intent_temperature
    days = profile.days_since_add
    q_count = profile.delivery_q_count

    if compute_stop_loss(profile):
        return "low_freq_maintenance"

    if temp == TEMP_BLAZING:
        return "closing_nudge"

    if temp == TEMP_HOT:
        if q_count >= MIN_DELIVERY_Q_COUNT_FOR_LIVE:
            return "proactive_live_push"
        return "targeted_objection"

    if temp == TEMP_WARM:
        return "deepen_discovery"

    if temp == TEMP_COLD:
        return "low_freq_maintenance"

    if temp == TEMP_COOL:
        if days > DAY_GATE_7:
            return "low_freq_maintenance"

    return "none"


# ── 同步取直播链接（纯内存测试 / 兜底用）──

def _get_live_url_sync() -> str:
    """同步取直播链接：环境变量 > mock。不碰 MCP。"""
    return os.getenv("SALES_LIVE_URL", _FALLBACK_LIVE_URL)


def build_action_builder(profile: CustomerProfile) -> str | None:
    """同步版 —— 给 run_without_db 等纯测试入口用。"""
    action = decide_next_action(profile)
    profile.next_action = action
    if action == "proactive_live_push":
        return _build_live_push_text(profile, _get_live_url_sync())
    return None


# ── 异步取直播链接（优先 MCP）──

async def _get_live_url_async() -> str:
    """异步取直播链接：MCP > 环境变量 > mock。"""
    # 1. 先尝试 MCP
    try:
        from deeptutor.services.shirley import get_live_url_from_env
        mcp_url = await get_live_url_from_env()
        if mcp_url:
            logger.info("直播链接来自 Shirley MCP: %s", mcp_url[:60])
            return mcp_url
        logger.info("MCP 未返回直播链接，降级到 env/mock")
    except Exception as e:
        logger.warning("MCP 直播链接获取异常，降级: %s", e)

    # 2. 环境变量
    env_url = os.getenv("SALES_LIVE_URL", "").strip()
    if env_url:
        return env_url

    # 3. 最后降级
    return _FALLBACK_LIVE_URL


async def build_action_builder_async(profile: CustomerProfile) -> str | None:
    """异步版 —— 生产主链路用，支持 MCP 拉直播链接。"""
    action = decide_next_action(profile)
    profile.next_action = action
    if action == "proactive_live_push":
        url = await _get_live_url_async()
        return _build_live_push_text(profile, url)
    return None


def _build_live_push_text(profile: CustomerProfile, url: str) -> str:
    """构造直播推送的具体文案."""
    grade = _grade_hint(profile)
    return (
        f"\n\n👉 对了，{grade}的家长都在关注本周的直播课，"
        f"我把链接发您：{url}，"
        f"开播前 15 分钟进群还能拿专属预习资料~"
    )

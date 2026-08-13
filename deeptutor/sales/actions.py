"""
动作决策器
========

根据温度档 + 时间窗 + 止损状态，决定 AI 回复里要追加什么主动动作。

当前阶段：只做"推直播链接"这一个消费方。
后续可扩展：临门一脚、止损提醒、转人工建议等。
"""
from __future__ import annotations

from .schemas import (
    CustomerProfile,
    TEMP_BLAZING, TEMP_HOT, TEMP_WARM, TEMP_COOL, TEMP_COLD,
    DAY_GATE_3, DAY_GATE_7, DAY_GATE_10,
)
from .temperature import compute_time_factor, compute_stop_loss


# ── 直播链接配置 ──
# mock URL，后续从 MCP 获取并通过环境变量覆盖
# SALES_LIVE_URL 环境变量或 MCP 注入会覆盖这里
DEFAULT_LIVE_URL = "https://xl.shirleyclass.com/s/64Y8vGI"

# 推送门槛：delivery_q_count（追问标签触发次数）≥ 2 时触发直播邀约
MIN_DELIVERY_Q_COUNT_FOR_LIVE = 2


def _grade_hint(profile: CustomerProfile) -> str:
    g = profile.profile.get("grade") or ""
    if g:
        return f"{g} 专属"
    return "全年级"


def decide_next_action(profile: CustomerProfile) -> str:
    """根据温度档 + 追问次数判定 next_action.

    直播推送门槛：delivery_q_count（追问标签触发次数）>= 2。
    极热档（q>=4）优先走临门一脚，不再推直播。
    """
    temp = profile.intent_temperature
    days = profile.days_since_add
    q_count = profile.delivery_q_count

    if compute_stop_loss(profile):
        return "low_freq_maintenance"

    if temp == TEMP_BLAZING:
        return "closing_nudge"

    # 热档：q_count 自然为 2-3，全部满足 >=2 → 推直播邀约
    if temp == TEMP_HOT:
        if q_count >= MIN_DELIVERY_Q_COUNT_FOR_LIVE:
            return "proactive_live_push"
        return "targeted_objection"

    # 温档：q_count=1，暂不够直播门槛，继续挖需求
    if temp == TEMP_WARM:
        return "deepen_discovery"

    if temp == TEMP_COLD:
        return "low_freq_maintenance"

    if temp == TEMP_COOL:
        if days > DAY_GATE_7:
            return "low_freq_maintenance"

    return "none"


def build_action_builder(profile: CustomerProfile) -> str | None:
    """返回要追加到 AI 回复尾部的动作文本，或 None."""
    import os as _os
    action = decide_next_action(profile)
    profile.next_action = action

    if action == "proactive_live_push":
        return _build_live_push_text(profile)
    return None


def _get_live_url() -> str:
    """取直播链接：环境变量 > mock."""
    import os as _os
    return _os.getenv("SALES_LIVE_URL", DEFAULT_LIVE_URL)


def _build_live_push_text(profile: CustomerProfile) -> str:
    """构造直播推送的具体文案."""
    grade = _grade_hint(profile)
    url = _get_live_url()
    return (
        f"\n\n👉 对了，{grade}的家长都在关注本周的直播课，"
        f"我把链接发您：{url}，"
        f"开播前 15 分钟进群还能拿专属预习资料~"
    )

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
import os as _os_env
from .temperature import compute_time_factor, compute_stop_loss


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


async def build_action_builder(
    profile: CustomerProfile,
    *,
    corpid: str | None = None,
    external_userid: str | None = None,
    qywx_userid: str | None = None,
) -> str | None:
    """返回要追加到 AI 回复尾部的动作文本，或 None."""
    action = decide_next_action(profile)
    profile.next_action = action

    if action == "proactive_live_push":
        return await _build_live_push_text(
            profile, corpid=corpid, external_userid=external_userid,
            qywx_userid=qywx_userid,
        )
    return None


async def _get_live_url(
    corpid: str | None = None,
    external_userid: str | None = None,
    qywx_userid: str | None = None,
) -> str | None:
    """取直播链接: 只走 Shirley MCP live skill (4.1 + 4)。

    4.1 可能返回多个 liveId → 逐场调 4 拿链接 → 按
    "campPeriodName name：链接" 每场一行拼接返回。
    拿不到就返回 None, 调用方跳过直播推送 —— 绝不回落到写死的链接。
    环境变量 SALES_LIVE_URL 仅作人工应急覆盖。
    """
    import logging as _logging
    import os as _os

    _log = _logging.getLogger(__name__)
    env_url = _os.getenv("SALES_LIVE_URL")
    if env_url:
        return env_url

    for cid in (corpid, _os.getenv("SHIRLEY_CORPID", "").strip()):
        if not (cid and external_userid):
            continue
        try:
            from deeptutor.services.shirley import live as shirley_live
            sessions = await shirley_live.list_weekly_lives(
                cid, external_userid, qywx_userid=qywx_userid
            )
            lines = shirley_live.format_live_lines(sessions)
            if lines:
                _log.info(
                    "[live] MCP hit | corpid=%s | sessions=%d | lines=%d",
                    cid, len(sessions), len(lines.splitlines()),
                )
                return lines
            _log.warning("[live] MCP returned no play_url | corpid=%s", cid)
        except Exception as e:
            _log.warning("[live] MCP failed | corpid=%s | err=%s", cid, e)

    _log.error("[live] no live url from MCP, skip live push (no hardcoded fallback)")
    return None


async def _build_live_push_text(
    profile: CustomerProfile,
    *,
    corpid: str | None = None,
    external_userid: str | None = None,
    qywx_userid: str | None = None,
) -> str | None:
    """构造直播推送的具体文案; MCP 拿不到链接时返回 None (跳过推送).

    url 可能是多行 (每场直播一行 "campPeriodName name：链接")。
    """
    url = await _get_live_url(corpid=corpid, external_userid=external_userid, qywx_userid=qywx_userid)
    if not url:
        return None
    grade = _grade_hint(profile)
    return (
        f"\n\n👉 对了，{grade}的家长都在关注本周的直播课，我把链接发您：\n"
        f"{url}\n"
        f"开播前 15 分钟进群还能拿专属预习资料~"
    )

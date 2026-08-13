"""
温度计算 + 时间窗 + 止损判定
==========================

全部基于文档 6.x 节的实测数据阈值，无主观加权。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .schemas import (
    CustomerProfile,
    TEMP_BLAZING, TEMP_HOT, TEMP_WARM, TEMP_COOL, TEMP_COLD, TEMP_UNKNOWN,
    DAY_GATE_3, DAY_GATE_5, DAY_GATE_7, DAY_GATE_10, DAY_GATE_14, DAY_GATE_28,
)


def compute_temperature(profile: CustomerProfile) -> str:
    """按 delivery_q_count + explicit_refusal 计算温度档。

    文档 6.1:
      q_count >= 4 → 极热（口头拒绝失效）
      q_count 2-3  → 热
      q_count = 1  → 温
      q_count = 0 + 拒绝 → 冷
      q_count = 0 + 无拒绝 → 凉
    """
    n = profile.delivery_q_count
    refusal = profile.explicit_refusal

    if n >= 4:
        return TEMP_BLAZING      # 极热忽略口头拒绝
    if n >= 2:
        return TEMP_HOT
    if n == 1:
        return TEMP_WARM
    # n == 0
    if refusal:
        return TEMP_COLD
    return TEMP_COOL


def compute_days_since_add(first_seen_at: datetime | None, now: datetime | None = None) -> int:
    """客户加微距今天数（暂用 first_seen_at 代替）."""
    if not first_seen_at:
        return 0
    now = now or datetime.now(timezone.utc)
    if first_seen_at.tzinfo is None:
        first_seen_at = first_seen_at.replace(tzinfo=timezone.utc)
    return max(0, (now - first_seen_at).days)


def compute_silent_days(last_active_at: datetime | None, now: datetime | None = None) -> int:
    """客户连续沉默天数：now - last_active_at（上一次客户发消息的时间）."""
    if not last_active_at:
        return 0
    now = now or datetime.now(timezone.utc)
    if last_active_at.tzinfo is None:
        last_active_at = last_active_at.replace(tzinfo=timezone.utc)
    return max(0, (now - last_active_at).days)


def compute_time_factor(days_since_add: int) -> str:
    """把天数映射到运营节奏档位.

    文档 6.2 节奏窗口:
      0-3 天 → 铺垫期（抛钩子，不逼单）
      4-6 天 → 主战场（直播 + 封班，58.3% 成交在这里）
      7-9 天 → 追单窗口
      10 天+ → 转低频
    """
    if days_since_add <= DAY_GATE_3:
        return "pre_live"      # 铺垫期
    if days_since_add <= 6:
        return "live_peak"     # 主战场
    if days_since_add <= 9:
        return "post_live"     # 追单窗口
    if days_since_add <= DAY_GATE_10:
        return "degrade"       # 降级
    return "low_freq"          # 低频维护


def compute_stop_loss(profile: CustomerProfile) -> bool:
    """是否应该止损？（文档 6.3 的精判）

    规则: 封班后（days_since_closing >= 4 或简单用 days_since_add >= 10）且客户沉默 3 天以上 → 止损

    但有例外: 客户只要还在回话（silent_days < 3），即使过了 7 天也继续跟进。
    """
    # 还在回话 → 不止损
    if profile.silent_days < 3:
        return False

    # 沉默 3+ 天，且已经过了主战场窗口
    days = profile.days_since_add
    if days >= DAY_GATE_10:
        return True
    # 刚过 7 天但还在 7-9 天窗口，再等等
    if days >= DAY_GATE_7:
        return True   # 过了 7 天断崖 + 沉默 = 止损
    return False


def apply_temperature_to_profile(
    profile: CustomerProfile,
    first_seen_at: datetime | None,
    last_active_at: datetime | None,
    now: datetime | None = None,
) -> None:
    """一站式：算温度 + 算时间窗 + 算止损 → 写回 profile."""
    now = now or datetime.now(timezone.utc)
    profile.days_since_add = compute_days_since_add(first_seen_at, now)
    profile.silent_days = compute_silent_days(last_active_at, now)
    profile.intent_temperature = compute_temperature(profile)

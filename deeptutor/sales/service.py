"""
顶层编排：一条客户消息进来 → 正则打标签 → 算 delivery_q_count → 算温度 → 算动作 → 写回 DB → 返回要追加的动作文本.

所有纯逻辑都在 tagger / temperature / actions 里，这个文件是胶水。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .schemas import CustomerProfile
from .tagger import (
    regex_tag, regex_profile_extract,
    merge_signals, merge_profile, compute_delivery_count,
)
from .temperature import apply_temperature_to_profile
from .actions import build_action_builder
from . import db as sales_db

logger = logging.getLogger(__name__)


async def process_customer_message(
    customer_msg: str,
    customer_external_id: str,
    channel: str = "wecom",
    nickname: str | None = None,
    llm_client: Any = None,
    force_llm: bool = False,
) -> tuple[CustomerProfile, str | None]:
    """处理一条客户消息。返回 (更新后的 profile, 要追加到 AI 回复的动作文本或 None).

    流程:
      1. upsert customer → 拿已有 profile
      2. 正则打标签（快路径，0 LLM 调用）
      3. 如果正则全 miss 或 force_llm → LLM 兜底
      4. 合并 signals + profile + 算 delivery_q_count
      5. 算温度档 + 时间窗 + 止损
      6. 算 next_action → 可能生成直播推送文本
      7. 写回 DB
    """
    # 1. 客户建档 / 读取
    cust_row = await sales_db.upsert_customer(
        external_id=customer_external_id,
        channel=channel,
        nickname=nickname,
    )
    customer_id = str(cust_row["id"])
    raw_profile = cust_row.get("profile")
    if isinstance(raw_profile, str):
        import json as _json
        raw_profile = _json.loads(raw_profile)
    existing_profile = dict(raw_profile) if isinstance(raw_profile, dict) else {}
    profile = CustomerProfile.from_dict(existing_profile)

    first_seen_at = cust_row.get("first_seen_at")
    last_active_at = cust_row.get("last_active_at")

    # 2. 正则打标签
    new_signals, new_source = regex_tag(customer_msg)
    new_profile_data = regex_profile_extract(customer_msg)

    # 3. LLM 兜底（正则命中 < 2 个且没有标记任何关键信号 → 可能是模糊表述）
    need_llm = (not new_signals) or force_llm
    if need_llm and llm_client is not None:
        try:
            from .tagger import llm_tag
            llm_sig, llm_src, llm_prof = await llm_tag(customer_msg, llm_client)
            # LLM 和正则结果合并：正则命中的保留 regex 源，LLM 新增的标记为 llm
            for label, val in llm_sig.items():
                if val and not new_signals.get(label):
                    new_signals[label] = True
                    new_source[label] = llm_src.get(label, "llm")
            # B 组画像只在正则没抽到的时候用 LLM
            for k, v in llm_prof.items():
                if v and not new_profile_data.get(k):
                    new_profile_data[k] = v
        except Exception as exc:
            logger.warning("LLM tag fallback skipped: %s", exc)

    # 4. 合并 + 算 delivery_q_count + 对话轮次累加
    merge_signals(profile, new_signals, new_source)
    merge_profile(profile, new_profile_data)
    profile.delivery_q_count = compute_delivery_count(profile)
    profile.customer_msg_count += 1

    # 5. 温度 + 时间窗
    apply_temperature_to_profile(profile, first_seen_at, last_active_at)

    # 6. 动作决策
    action_text = build_action_builder(profile)

    # 7. 写回 DB
    try:
        await sales_db.update_customer_profile(
            customer_id=customer_id,
            profile_json=profile.to_dict(),
            intent_temperature=profile.intent_temperature,
            next_action=profile.next_action,
        )
    except Exception as exc:
        logger.warning("profile writeback failed: %s", exc)

    return profile, action_text


def run_without_db(
    customer_msg: str,
    existing_profile: dict | None = None,
    first_seen_at: datetime | None = None,
    last_active_at: datetime | None = None,
) -> tuple[CustomerProfile, str | None]:
    """纯内存跑一遍，不碰 DB。方便单元测试 / 离线回放."""
    profile = CustomerProfile.from_dict(existing_profile)

    new_signals, new_source = regex_tag(customer_msg)
    new_profile_data = regex_profile_extract(customer_msg)

    merge_signals(profile, new_signals, new_source)
    merge_profile(profile, new_profile_data)
    profile.delivery_q_count = compute_delivery_count(profile)
    profile.customer_msg_count += 1

    apply_temperature_to_profile(profile, first_seen_at, last_active_at)
    action_text = build_action_builder(profile)

    return profile, action_text


# ── 公共入口：任何 chat 入口都可以加这 3 行触发 ──

async def maybe_run_sales_intent(
    user_id: str,
    user_text: str,
    channel: str = "wecom",
) -> str | None:
    """运行销售意向度判定，返回可能的追加文本（如直播链接）。

    开关: ``SALES_INTENT_ENABLED=true`` 时启用。
    任何异常都被吞掉，保证不影响正常回复流程。
    """
    import os

    if os.getenv("SALES_INTENT_ENABLED", "").strip().lower() not in ("1", "true", "yes"):
        return None
    if not user_text or not user_text.strip():
        return None

    try:
        _, action_text = await process_customer_message(
            customer_msg=user_text.strip(),
            customer_external_id=user_id,
            channel=channel,
        )
        return action_text
    except ImportError:
        logger.info("sales intent skipped: dependency missing (asyncpg?)")
        return None
    except Exception:
        logger.warning("sales intent scoring failed (non-fatal)", exc_info=True)
        return None

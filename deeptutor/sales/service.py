"""
顶层编排：一条客户消息进来 → LLM打标 → 正则兜底 → 算 delivery_q_count → 算温度 → 算动作 → 写回 DB → 返回要追加的动作文本.

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
    llm_tag,
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
    force_regex: bool = False,
    corpid: str | None = None,
    qywx_userid: str | None = None,
    qywx_userid_fallback: str | None = None,
    third_sale_uuid_fallback: str | None = None,
    third_user_id_fallback: int | None = None,
    vid_fallback: int | None = None,
    _existing_cust_row: dict | None = None,
) -> tuple[CustomerProfile, str | None]:
    """处理一条客户消息。返回 (更新后的 profile, 要追加到 AI 回复的动作文本或 None).

    流程:
      1. upsert customer → 拿已有 profile (_existing_cust_row 传入时跳过, 由调用方已 upsert)
      2. LLM 打标签（优先，有 llm_client 就调）
      3. 正则兜底（LLM 失败/全空 或 force_regex=True 时触发，补 LLM 没覆盖到的标签）
      4. 合并 signals + profile + 算 delivery_q_count
      5. 算温度档 + 时间窗 + 止损
      6. 算 next_action → 可能生成直播推送文本
      7. 写回 DB
    """
    # 1. 客户建档 / 读取
    if _existing_cust_row is not None:
        cust_row = _existing_cust_row
    else:
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

    new_signals: dict[str, bool] = {}
    new_source: dict[str, str] = {}
    new_profile_data: dict[str, Any] = {}

    # 2. LLM 打标（优先）
    llm_ok = False
    if llm_client is not None and not force_regex:
        try:
            llm_sig, llm_src, llm_prof = await llm_tag(customer_msg, llm_client)
            # LLM 命中的直接装进去，source = "llm"
            for label, val in llm_sig.items():
                if val:
                    new_signals[label] = True
                    new_source[label] = llm_src.get(label, "llm")
            # B 组画像有值就填
            for k, v in llm_prof.items():
                if v is not None and v != "" and v != []:
                    new_profile_data[k] = v
            llm_ok = True
        except Exception as exc:
            logger.warning("LLM tag failed, falling back to regex: %s", exc)

    # 3. 正则补漏（无论 LLM 有没有命中都跑，只补 LLM 没覆盖到的标签）
    reg_sig, reg_src = regex_tag(customer_msg)
    reg_prof = regex_profile_extract(customer_msg)
    for label, val in reg_sig.items():
        if val and not new_signals.get(label):
            new_signals[label] = True
            new_source[label] = reg_src.get(label, "regex")
    for k, v in reg_prof.items():
        if v and not new_profile_data.get(k):
            new_profile_data[k] = v

    # 4. 合并 + 算 delivery_q_count + 对话轮次累加
    merge_signals(profile, new_signals, new_source)
    merge_profile(profile, new_profile_data)
    profile.delivery_q_count = compute_delivery_count(profile)
    profile.customer_msg_count += 1

    # 5. 温度 + 时间窗
    apply_temperature_to_profile(profile, first_seen_at, last_active_at)

    # 6. 动作决策
    action_text = await build_action_builder(
        profile, corpid=corpid, external_userid=customer_external_id,
        qywx_userid=qywx_userid,
        qywx_userid_fallback=qywx_userid_fallback,
        third_sale_uuid_fallback=third_sale_uuid_fallback,
        third_user_id_fallback=third_user_id_fallback,
        vid_fallback=vid_fallback,
    )

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
    action_text = None

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

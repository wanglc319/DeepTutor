"""
asyncpg 连接管理 + CRUD.

不追求 ORM，够用就行。连接池懒加载。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


def _dsn_from_env() -> str:
    host = os.getenv("SALES_PG_HOST", "127.0.0.1")
    port = int(os.getenv("SALES_PG_PORT", "5433"))
    user = os.getenv("SALES_PG_USER", "sales")
    pwd  = os.getenv("SALES_PG_PASSWORD", "sales_dev_2026")
    db   = os.getenv("SALES_PG_DATABASE", "sales_crm")
    return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None or _pool.is_closing():
        _pool = await asyncpg.create_pool(_dsn_from_env(), min_size=1, max_size=5)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool and not _pool.is_closing():
        await _pool.close()
    _pool = None


# ── customers ──────────────────────────────────────────────────────────

async def upsert_customer(
    external_id: str,
    channel: str = "wecom",
    nickname: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """按 external_id upsert customer，返回完整 row."""
    pool = await get_pool()
    now = datetime.now(timezone.utc)
    row = await pool.fetchrow(
        """
        INSERT INTO customers (external_id, channel, nickname, first_seen_at, last_active_at)
        VALUES ($1, $2, $3, $4, $4)
        ON CONFLICT (external_id) DO UPDATE
            SET last_active_at = $4,
                nickname      = COALESCE(EXCLUDED.nickname, customers.nickname)
        RETURNING id, external_id, channel, nickname, first_seen_at, last_active_at,
                  profile, intent_temperature, next_action, intent_level
        """,
        external_id, channel, nickname, now,
    )
    return dict(row)


async def get_customer_by_external_id(external_id: str) -> dict[str, Any] | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM customers WHERE external_id = $1", external_id
    )
    return dict(row) if row else None


async def get_customer_by_id(customer_id: str) -> dict[str, Any] | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM customers WHERE id = $1", customer_id
    )
    return dict(row) if row else None


async def update_customer_profile(
    customer_id: str,
    profile_json: dict[str, Any],
    intent_temperature: str,
    next_action: str,
) -> None:
    """更新 customers.profile + 冗余索引列."""
    import json as _json
    pool = await get_pool()
    now = datetime.now(timezone.utc)
    await pool.execute(
        """
        UPDATE customers
        SET profile            = $1::jsonb,
            intent_temperature  = $2::intent_temperature,
            next_action         = $3::next_action,
            profile_updated_at  = $4,
            last_active_at      = $4
        WHERE id = $5
        """,
        _json.dumps(profile_json, ensure_ascii=False),
        intent_temperature,
        next_action,
        now,
        customer_id,
    )


async def touch_customer_active(customer_id: str) -> None:
    """仅刷新 last_active_at."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE customers SET last_active_at = now() WHERE id = $1", customer_id
    )


# ── messages ────────────────────────────────────────────────────────────

async def append_message(
    conversation_id: str,
    sender: str,            # 'customer' | 'ai'
    content: str,
    llm_model: str | None = None,
    latency_ms: int | None = None,
    rag_hits: list[dict] | None = None,
) -> None:
    import json as _json
    pool = await get_pool()
    # 先拿到当前 conv 的最大 seq
    row = await pool.fetchrow(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM messages WHERE conversation_id = $1",
        conversation_id,
    )
    seq = row["next_seq"] if row else 1

    # asyncpg 对 jsonb 参数要求: 传 json.dumps 后的字符串, 不要直接 Python list/dict
    rag_json = _json.dumps(rag_hits or [], ensure_ascii=False)

    await pool.execute(
        """
        INSERT INTO messages (conversation_id, sender, seq, content, llm_model, latency_ms, rag_hits)
        VALUES ($1, $2::message_sender, $3, $4, $5, $6, $7::jsonb)
        """,
        conversation_id, sender, seq, content, llm_model, latency_ms, rag_json,
    )
    # 同步更新 conversation.last_message_at
    await pool.execute(
        "UPDATE conversations SET last_message_at = now() WHERE id = $1", conversation_id
    )


# ── conversations (memory 层) ──────────────────────────────────────────

async def upsert_conversation(
    customer_id: str,
    session_hint: str | None = None,
    partner_id: str = "lisa",
) -> str:
    """按 (customer_id, opened_at 当天) 找现有会话, 没有就开一个. 返回 conversation id.

    企微是"客户 → AI → 结束/挂起 → 回来继续"的模式, 同一天内同一个客户
    应该复用同一个 conversation, 而不是每次新开, 否则 messages 表就炸了.
    """
    from datetime import date
    pool = await get_pool()
    today = date.today()
    row = await pool.fetchrow(
        """
        SELECT id FROM conversations
        WHERE customer_id = $1
          AND partner_id   = $2
          AND opened_at::date = $3
        ORDER BY opened_at DESC LIMIT 1
        """,
        customer_id, partner_id, today,
    )
    if row:
        conv_id = str(row["id"])
        await pool.execute(
            "UPDATE conversations SET last_message_at = now() WHERE id = $1", conv_id
        )
        return conv_id
    row = await pool.fetchrow(
        """
        INSERT INTO conversations (customer_id, partner_id)
        VALUES ($1, $2)
        RETURNING id
        """,
        customer_id, partner_id,
    )
    return str(row["id"])


async def count_conv_messages(conversation_id: str) -> int:
    pool = await get_pool()
    n = await pool.fetchval(
        "SELECT count(*) FROM messages WHERE conversation_id = $1", conversation_id
    )
    return int(n) if n else 0


async def fetch_history_window(
    conversation_id: str,
    window_turns: int = 10,
) -> list[dict[str, Any]]:
    """取最近 window_turns 条消息, 转为 LLM 可用的 [{role, content}] 格式.

    注意: 返回的是 messages 表直接按 seq 排序的结果, 不管是否已被 summary 覆盖.
    如果会话很长, 调用方应该先拿 fetch_applicable_summary + 本函数做拼接.
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT sender, content, seq, created_at
        FROM messages
        WHERE conversation_id = $1
        ORDER BY seq DESC
        LIMIT $2
        """,
        conversation_id, window_turns,
    )
    out: list[dict[str, Any]] = []
    for r in reversed(rows):  # 再翻回来正序
        role = "user" if r["sender"] == "customer" else "assistant"
        out.append({"role": role, "content": r["content"]})
    return out


async def fetch_applicable_summary(
    conversation_id: str,
    recent_lo_seq: int,
) -> str | None:
    """取能覆盖 < recent_lo_seq (即滑窗之外更早那段) 的最新摘要.

    只有当存在 summary.lo_seq <= recent_lo_seq - 1 时才有意义;
    否则说明滑窗已经覆盖了全部历史, 不需要摘要.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT summary, key_points, lo_seq, hi_seq
        FROM conversation_summary
        WHERE conversation_id = $1
          AND lo_seq          <= $2
        ORDER BY lo_seq DESC
        LIMIT 1
        """,
        conversation_id, recent_lo_seq - 1,
    )
    if not row:
        return None
    kp = row["key_points"] or []
    kp_text = "\n".join(f"  - {k}" for k in kp) if kp else ""
    head = (
        f"[历史摘要] (覆盖 seq {row['lo_seq']}~{row['hi_seq']})\n"
        f"{row['summary']}"
    )
    return head


async def save_summary(
    conversation_id: str,
    lo_seq: int,
    hi_seq: int,
    summary: str,
    key_points: list[str] | None = None,
    llm_model: str | None = None,
    token_count: int | None = None,
    latency_ms: int | None = None,
) -> None:
    """把一段消息的 LLM 摘要写入 conversation_summary."""
    import json as _json
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO conversation_summary
            (conversation_id, lo_seq, hi_seq, turn_count, summary, key_points,
             llm_model, token_count, latency_ms)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
        """,
        conversation_id,
        lo_seq,
        hi_seq,
        hi_seq - lo_seq + 1,
        summary,
        _json.dumps(key_points or [], ensure_ascii=False),
        llm_model,
        token_count,
        latency_ms,
    )


async def fetch_all_messages_seq_range(
    conversation_id: str,
    lo_seq: int,
    hi_seq: int,
) -> list[dict[str, Any]]:
    """取指定 seq 区间的原始消息, 用于送 LLM 做总结."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT seq, sender, content
        FROM messages
        WHERE conversation_id = $1
          AND seq BETWEEN $2 AND $3
        ORDER BY seq
        """,
        conversation_id, lo_seq, hi_seq,
    )
    return [dict(r) for r in rows]

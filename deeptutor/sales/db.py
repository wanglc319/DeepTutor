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
    pool = await get_pool()
    # 先拿到当前 conv 的最大 seq
    row = await pool.fetchrow(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM messages WHERE conversation_id = $1",
        conversation_id,
    )
    seq = row["next_seq"] if row else 1

    await pool.execute(
        """
        INSERT INTO messages (conversation_id, sender, seq, content, llm_model, latency_ms, rag_hits)
        VALUES ($1, $2::message_sender, $3, $4, $5, $6, $7::jsonb)
        """,
        conversation_id, sender, seq, content, llm_model, latency_ms, rag_hits or [],
    )
    # 同步更新 conversation.last_message_at
    await pool.execute(
        "UPDATE conversations SET last_message_at = now() WHERE id = $1", conversation_id
    )

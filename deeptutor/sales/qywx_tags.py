"""
tag_qywx 标签查询 + 自动映射服务
================================

职责:
  1. 从 PG 的 tag_qywx 表查 tag_name → tag_id (支持精确匹配 + pg_trgm 模糊匹配)
  2. 把 sales_service 返回的 CustomerProfile 自动映射成一组 tag_name (方向 Z)
  3. 支持 LLM 输出的 tag_name 直接匹配 (方向 X)
  4. 输出 tag_id[] → 交给 qywx.mark_tags 调 Shirley MCP 2 接口

缓存:
  tag_qywx 全量只有 4398 条，进程内懒加载 + 定时刷新 (默认 5min)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

_CACHE_TTL = 300.0  # 5min
_cache_ts = 0.0
_cache_name_to_id: dict[str, str] = {}
_cache_lock = asyncio.Lock()

# sales_service intent_temperature → 建议 tag_name
# 这个是「软性映射」，tag_name 必须在 tag_qywx 表里存在才生效
# tag_qywx 里如果没有对应名字，silently skip — 不报错
_INTENT_TAG_SUGGESTIONS: dict[str, list[str]] = {
    "blazing":  ["高意向用户"],
    "hot":      ["高意向用户"],
    "warm":     [],
    "cool":     [],
    "cold":     [],
    "unknown":  [],
}


async def _get_pool() -> asyncpg.Pool:
    from deeptutor.sales.db import get_pool
    return await get_pool()


async def _ensure_cache() -> dict[str, str]:
    """懒加载 tag_qywx 表到内存 dict[tag_name → tag_id]"""
    global _cache_ts, _cache_name_to_id
    now = time.time()
    if _cache_name_to_id and (now - _cache_ts) < _CACHE_TTL:
        return _cache_name_to_id

    async with _cache_lock:
        if _cache_name_to_id and (time.time() - _cache_ts) < _CACHE_TTL:
            return _cache_name_to_id
        pool = await _get_pool()
        rows = await pool.fetch(
            "SELECT tag_id, tag_name FROM tag_qywx WHERE NOT is_deleted"
        )
        _cache_name_to_id = {r["tag_name"]: r["tag_id"] for r in rows}
        _cache_ts = time.time()
        logger.info("[tag_cache] loaded %d tags from tag_qywx", len(_cache_name_to_id))
        return _cache_name_to_id


async def invalidate_cache() -> None:
    """下次调用 _ensure_cache 时重新拉。"""
    global _cache_ts
    _cache_ts = 0.0


async def resolve_tag_names(tag_names: list[str]) -> list[str]:
    """把一组 tag_name 查表转成 tag_id。找不到的 tag_name 跳过并记 warning."""
    if not tag_names:
        return []
    cache = await _ensure_cache()
    found: list[str] = []
    missing: list[str] = []
    for name in tag_names:
        if not name:
            continue
        tid = cache.get(name)
        if tid:
            found.append(tid)
        else:
            missing.append(name)
    if missing:
        logger.warning("[tag.resolve] tag_name not found in tag_qywx: %s", missing)
    return found


async def fuzzy_resolve_tag_names(tag_names: list[str]) -> list[str]:
    """PG pg_trgm 模糊匹配 (LIKE %name%) — 当精确匹配找不到时用。"""
    if not tag_names:
        return []
    pool = await _get_pool()
    resolved: list[str] = []
    for name in tag_names:
        if not name:
            continue
        rows = await pool.fetch(
            """SELECT tag_id, tag_name, similarity(tag_name, $1) AS sim
               FROM tag_qywx
               WHERE NOT is_deleted AND tag_name ILIKE $2
               ORDER BY sim DESC LIMIT 1""",
            name, f"%{name}%",
        )
        if rows:
            # 结果里可能有多条候选，取第一条
            for r in rows:
                resolved.append(r["tag_id"])
        else:
            logger.warning("[tag.fuzzy_resolve] no fuzzy match for: %s", name)
    return resolved


async def tag_ids_for_profile(profile: Any, *, extra_names: list[str] | None = None) -> list[str]:
    """方向 Z: 把 CustomerProfile 自动映射成 tag_id 列表。

    规则:
      1. intent_temperature (blazing/hot/warm/cool/cold) → 预定义 tag_name 映射
      2. explicit_refusal=True → 建议打「拒绝」相关标签（表里有就打）
      3. extra_names: 调用方额外想打的 tag_name 列表 (方向 X)
    """
    names: list[str] = []
    seen = set()

    def _add(n: str) -> None:
        if n and n not in seen:
            seen.add(n)
            names.append(n)

    intent_temp = getattr(profile, "intent_temperature", "unknown")
    for suggested in _INTENT_TAG_SUGGESTIONS.get(intent_temp, []):
        _add(suggested)

    if getattr(profile, "explicit_refusal", False):
        # 拒绝场景: 找 tag_name 含「拒绝」或「流失」的
        pool = await _get_pool()
        rows = await pool.fetch(
            """SELECT tag_name FROM tag_qywx
               WHERE NOT is_deleted
                 AND (tag_name ILIKE '%拒绝%' OR tag_name ILIKE '%流失%')
               LIMIT 5"""
        )
        for r in rows:
            _add(r["tag_name"])

    for n in (extra_names or []):
        _add(n)

    # 先精确匹配
    exact_ids = await resolve_tag_names(names)
    missing = [n for n in names if _cache_name_to_id.get(n) not in exact_ids]
    if missing:
        fuzzy_ids = await fuzzy_resolve_tag_names(missing)
        exact_ids.extend(fuzzy_ids)

    logger.info("[tag.tag_ids_for_profile] intent=%s | names=%s | resolved_ids=%d",
                intent_temp, names, len(exact_ids))
    return exact_ids


async def apply_tags_to_customer(
    *,
    corpid: str,
    external_userid: str,
    tag_ids: list[str],
    follow_userid: str | None = None,
) -> None:
    """调 Shirley MCP 2 接口打标签。tag_ids 为空则跳过。"""
    if not tag_ids:
        logger.info("[tag.apply] tag_ids empty, skip")
        return
    from deeptutor.services.shirley import qywx
    t0 = time.perf_counter()
    try:
        res = await qywx.mark_tags(
            corpid, external_userid, tag_ids, follow_userid=follow_userid,
        )
        logger.info(
            "[tag.apply] elapsed_ms=%d | tag_ids=%s | follow_userid=%s | ok=%s",
            int((time.perf_counter() - t0) * 1000), tag_ids, follow_userid or "(auto)", res is not None,
        )
    except Exception as e:
        logger.warning("[tag.apply] FAILED | err=%s", e)

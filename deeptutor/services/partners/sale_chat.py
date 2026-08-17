"""
Sale Chat Service
=================

给 Lisa 销售侧用的专用聊天入口。与通用 partner/chat 不同:

  1. 每个 session_id 独立维护一个 10s 抖动窗口 (debounce)
  2. 窗口内聚合所有用户消息 → 触发业务逻辑
  3. 业务链路: fetch_profile(5.1) → fetch_history(5.2)
     → sales_service(打标签+温度档+拒绝判定+next_action)
     → 拒绝? push_reject(6 msgType=119) : llm_reply(画像注入) → push_sentences
     → analyze_and_save(5.3) 同步回写画像
  4. 正常回复: 分句 → 逐句调 Shirley MCP reply_lisa_message
  5. 拒绝回复: 直接调 reply_lisa_message(msgType=119, answer=拒绝原因)
  6. 全部推送完清空窗口

架构:
  进程内 dict[session_id, _DebounceSlot] 维护状态
  asyncio.Lock 保护 per-session 并发安全
  asyncio.create_task 启动抖动定时器 + 触发处理
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import os
import re
import time
from typing import Any

from deeptutor.observability.agent_monitor import (
    detect_bad_case,
    mark_mcp_failed,
    observation,
    redact_pii,
)
from deeptutor.services.partners.sentence_split import TypingDelay, split_sentences
from deeptutor.services.shirley import qywx

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 10.0
TYPING_BASE_DELAY = 0.50
TYPING_PER_CHAR = 0.30
TYPING_MAX_DELAY = 12.0

# ── PG memory 层: 长会话策略 ──────────────────────────────────────────
# MEMORY_WINDOW_HARD: 每次送给 LLM 推理的消息条数上限 (滑窗)
# MEMORY_SUMMARIZE_TRIGGER: conversation.messages 累计条数 ≥ 此值时
#   自动取前半段送 LLM 做总结 → 存入 conversation_summary
#   下次推理时 messages = 最新 window 条 (PG) + 历史摘要 (PG)
MEMORY_WINDOW_HARD = 10
MEMORY_SUMMARIZE_TRIGGER = 20
# 总结一次覆盖的条数 (从 lo_seq 开始往前推)
MEMORY_SUMMARIZE_CHUNK = 12

# 动态 soul 的 partner_id: 从 data/partners/<id>/workspace/user/workspace/SOUL.md 读取
# 后台改完 SOUL.md 下一轮对话立即生效, 无需重启
SOUL_PARTNER_ID = "lisa"


@dataclass
class _DebounceSlot:
    """一个 session 的抖动窗口状态。"""

    messages: list[dict[str, Any]] = field(default_factory=list)
    last_event_ts: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timer_task: asyncio.Task | None = None
    prime_info: dict[str, Any] = field(default_factory=dict)
    corpid: str = ""
    external_userid: str = ""


_slots: dict[str, _DebounceSlot] = {}
_slots_lock = asyncio.Lock()


async def enqueue(
    session_id: str,
    message: dict[str, Any],
    *,
    corpid: str,
    external_userid: str,
    prime_info: dict[str, Any],
) -> None:
    """把一条消息塞进 session 的 debounce 窗口。触发 / 重置定时器。"""
    if not session_id:
        logger.warning("[sale_chat.enqueue] session_id 为空，丢弃")
        return

    content = (message or {}).get("content", "")
    slot = await _get_or_create_slot(session_id)

    async with slot.lock:
        slot.messages.append(message)
        slot.last_event_ts = time.time()
        slot.corpid = corpid
        slot.external_userid = external_userid
        slot.prime_info = prime_info

        was_running = bool(slot.timer_task and not slot.timer_task.done())
        if was_running:
            slot.timer_task.cancel()

        slot.timer_task = asyncio.create_task(
            _debounce_timer(session_id, DEBOUNCE_SECONDS)
        )

    logger.info(
        "[sale_chat.enqueue] session=%s | msg_len=%d | aggregate_count=%d | timer=%s | corpid=%s | ext=%s",
        session_id, len(content), len(slot.messages),
        "reset" if was_running else "new", corpid, external_userid,
    )


async def _get_or_create_slot(session_id: str) -> _DebounceSlot:
    async with _slots_lock:
        slot = _slots.get(session_id)
        if slot is None:
            slot = _DebounceSlot()
            _slots[session_id] = slot
        return slot


async def _debounce_timer(session_id: str, delay: float) -> None:
    t0 = time.perf_counter()
    logger.debug("[sale_chat.debounce] session=%s timer_start delay=%.1fs", session_id, delay)
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        logger.debug("[sale_chat.debounce] session=%s timer_cancelled after %.1fs", session_id, time.perf_counter() - t0)
        return

    slot = _slots.get(session_id)
    if slot is None:
        return

    async with slot.lock:
        if not slot.messages:
            return
        messages_to_process = list(slot.messages)
        slot.messages.clear()
        slot.timer_task = None
        corpid = slot.corpid
        external_userid = slot.external_userid
        prime_info = dict(slot.prime_info)

    elapsed = time.perf_counter() - t0
    logger.info(
        "[sale_chat.debounce] session=%s TRIGGERED after %.1fs | aggregated_msgs=%d",
        session_id, elapsed, len(messages_to_process),
    )

    asyncio.create_task(
        _process_session(
            session_id=session_id,
            messages=messages_to_process,
            corpid=corpid,
            external_userid=external_userid,
            prime_info=prime_info,
        )
    )


# ── Shirley 画像拉取 / 写回 辅助 ──

async def _fetch_profile_summary(corpid: str, external_userid: str) -> str:
    """调 Shirley 5.1 get_user_profile → summarize_profile 压缩成摘要注入 system prompt."""
    try:
        from deeptutor.services.shirley import profile as shirley_profile
        t0 = time.perf_counter()
        raw = await shirley_profile.fetch_profile(corpid, external_userid)
        summary = shirley_profile.summarize_profile(raw)
        logger.info(
            "[sale_chat.fetch_profile] elapsed_ms=%d | has_profile=%s | summary_len=%d",
            int((time.perf_counter() - t0) * 1000), bool(raw), len(summary),
        )
        return summary
    except Exception as e:
        logger.warning("[sale_chat.fetch_profile] failed: %s", e)
        return ""


async def _analyze_and_save_profile(
    *,
    corpid: str,
    external_userid: str,
    history: list[dict[str, Any]],
    user_text: str,
    intent_level: str | None,
) -> None:
    """调 Shirley 5.3 把本轮 sales service 判定的画像结果同步回写."""
    try:
        from deeptutor.services.shirley import profile as shirley_profile
        t0 = time.perf_counter()
        res = await shirley_profile.analyze_and_save(
            corpid=corpid,
            external_userid=external_userid,
            history=history,
            user_text=user_text,
            intent_level=intent_level,
        )
        logger.info(
            "[sale_chat.analyze_and_save] elapsed_ms=%d | intent_level=%s | ok=%s",
            int((time.perf_counter() - t0) * 1000), intent_level, res is not None,
        )
    except Exception as e:
        logger.warning("[sale_chat.analyze_and_save] failed: %s", e)


# ── PG memory 辅助函数 ─────────────────────────────────────────────────


async def _pg_ensure_customer_conv(
    external_userid: str,
    nickname: str | None = None,
    partner_id: str = "lisa",
) -> tuple[dict[str, Any], str] | None:
    """upsert customer + upsert conversation (当天复用), 返回 (cust_row, conversation_id).

    cust_row 包含完整 customers 表数据, 可直接传给 process_customer_message
    的 _existing_cust_row 参数, 避免重复 upsert.
    PG 挂了不阻断对话, 返回 None 让调用方跳过 PG 记忆层, 继续走 Shirley 就行.
    """
    try:
        from deeptutor.sales import db as sales_db
        cust = await sales_db.upsert_customer(
            external_id=external_userid,
            channel="wecom",
            nickname=nickname,
        )
        customer_id = str(cust["id"])
        conv_id = await sales_db.upsert_conversation(customer_id, partner_id=partner_id)
        return dict(cust), conv_id
    except Exception as e:
        logger.warning("[sale_chat.pg.memory] ensure customer/conv FAILED=%s", e)
        return None


async def _pg_append_message(
    conversation_id: str,
    sender: str,
    content: str,
    llm_model: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """往 PG messages 表写一条. 失败只 log, 不抛."""
    try:
        from deeptutor.sales import db as sales_db
        await sales_db.append_message(
            conversation_id=conversation_id,
            sender=sender,
            content=content,
            llm_model=llm_model,
            latency_ms=latency_ms,
        )
    except Exception as e:
        logger.warning("[sale_chat.pg.memory] append_message(%s) FAILED=%s", sender, e)


async def _pg_build_history_for_llm(
    conversation_id: str,
) -> tuple[list[dict[str, str]], str | None]:
    """从 PG 组装 LLM 可用的 messages: 滑窗 + 历史摘要.

    返回 (history_list, summary_inject_text):
      - history_list: [{role:user/assistant, content}] 正序
      - summary_inject_text: 若存在更早的摘要, 返回一段要注入 system prompt 的文本; None 表示没有
    """
    try:
        from deeptutor.sales import db as sales_db

        # 1) 先算滑窗的起点 seq
        total = await sales_db.count_conv_messages(conversation_id)
        lo_seq = max(1, total - MEMORY_WINDOW_HARD + 1) if total > 0 else 1

        # 2) 取滑窗 (不含当前这轮 user 消息, 主流程会 append 当前消息)
        window = await sales_db.fetch_history_window(
            conversation_id, MEMORY_WINDOW_HARD
        )

        # 3) 取覆盖滑窗之前那段的摘要
        summary_text = await sales_db.fetch_applicable_summary(
            conversation_id, recent_lo_seq=lo_seq
        )

        logger.info(
            "[sale_chat.pg.memory] conv=%s | total_msgs=%d | window_lo=%d | summary=%s",
            conversation_id, total, lo_seq, bool(summary_text),
        )
        return window, summary_text
    except Exception as e:
        logger.warning("[sale_chat.pg.memory] build_history FAILED=%s", e)
        return [], None


async def _pg_maybe_trigger_summary(
    conversation_id: str,
    llm_model: str | None = None,
) -> None:
    """count_conv_messages ≥ MEMORY_SUMMARIZE_TRIGGER 时, 异步 fire-and-forget
    一段 LLM 总结. 不阻塞主流程 —— 就算总结失败, 下次还会再触发.
    """
    try:
        from deeptutor.sales import db as sales_db

        total = await sales_db.count_conv_messages(conversation_id)
        if total < MEMORY_SUMMARIZE_TRIGGER:
            return

        # 找到当前最新 summary 覆盖到哪 (没有就从 seq 1 开始)
        pool = await sales_db.get_pool()
        last = await pool.fetchval(
            "SELECT COALESCE(MAX(hi_seq), 0) FROM conversation_summary "
            "WHERE conversation_id = $1",
            conversation_id,
        )
        lo_seq = int(last) + 1
        hi_seq = min(total, lo_seq + MEMORY_SUMMARIZE_CHUNK - 1)
        if lo_seq > hi_seq:
            return  # 已经总结到顶了

        # 拉那段原始消息
        chunk = await sales_db.fetch_all_messages_seq_range(
            conversation_id, lo_seq, hi_seq
        )
        if not chunk:
            return

        # 送 LLM 总结 (用 DeepTutor 内置 LLMClient)
        summary, key_points = await _llm_summarize_chunk(chunk, llm_model=llm_model)
        if not summary:
            logger.warning("[sale_chat.pg.memory] summarize LLM returned empty")
            return

        await sales_db.save_summary(
            conversation_id=conversation_id,
            lo_seq=lo_seq,
            hi_seq=hi_seq,
            summary=summary,
            key_points=key_points,
            llm_model=llm_model,
        )
        logger.info(
            "[sale_chat.pg.memory] summary saved | conv=%s | seq %d~%d | kp=%d",
            conversation_id, lo_seq, hi_seq, len(key_points),
        )
    except Exception as e:
        logger.warning("[sale_chat.pg.memory] trigger_summary FAILED=%s", e)


async def _llm_summarize_chunk(
    chunk: list[dict[str, Any]],
    llm_model: str | None = None,
) -> tuple[str, list[str]]:
    """把一段 messages (dict with seq/sender/content) 送 LLM 做总结.

    返回 (summary_text, key_points_list).
    """
    from deeptutor.services.llm import get_llm_client

    # 组装对话文本
    turns: list[str] = []
    for m in chunk:
        role = "客户" if m["sender"] == "customer" else "AI"
        turns.append(f"[{role}] {m['content']}")
    text_block = "\n".join(turns)

    system_prompt = (
        "你是 AI 销售记忆压缩工具。请把下面一段 AI 销售 (Lisa) 和客户的历史对话"
        "压缩成 300-400 字的摘要段落 (第三人称, 保留关键事实)，"
        "并额外列出 5 条以内的 key_points (客户明确说过的事实/偏好/顾虑)。\n"
        "严格按以下格式输出 (不要加前后缀解释):\n"
        "SUMMARY: ...\nKEY_POINTS:\n- ...\n- ...\n- ..."
    )
    prompt = f"=== 对话开始 ===\n{text_block}\n=== 对话结束 ==="

    llm = get_llm_client()
    try:
        if hasattr(llm, "complete") and callable(getattr(llm, "complete")):
            raw = await llm.complete(prompt=prompt, system_prompt=system_prompt)
        else:
            raw = ""
    except Exception as e:
        logger.warning("[sale_chat.pg.memory] llm summarize FAILED=%s", e)
        return "", []

    if not raw:
        return "", []

    # 解析 SUMMARY / KEY_POINTS
    summary = ""
    key_points: list[str] = []
    lines = raw.strip().splitlines()
    section = None
    for ln in lines:
        s = ln.strip()
        low = s.lower()
        if low.startswith("summary"):
            section = "summary"
            summary = s.split(":", 1)[1].strip() if ":" in s else ""
            continue
        if low.startswith("key_points") or low.startswith("key points"):
            section = "kp"
            continue
        if section == "summary" and s:
            summary += ("\n" if summary else "") + s
        elif section == "kp":
            if s.startswith("-") or s.startswith("*") or s.startswith("•"):
                kp = s.lstrip("-*•").strip()
                if kp:
                    key_points.append(kp)
    return summary.strip(), key_points


# 知识库中历史过期的直播回放链接（不可靠，不返回给用户）
_LIVE_LINK_RE = re.compile(
    r'https?://[^\s<>"\')\]]*(?:shirleyclass\.com|live\.|h5\.|watch\.)[^\s<>"\')\]]*',
    re.IGNORECASE,
)


def _strip_live_links(text: str) -> str:
    """清除知识库召回内容中的直播回放链接。

    知识库里的直播链接是历史录入的，大部分已过期失效。
    直播链接应从 MCP 接口实时获取，不使用知识库中的历史链接。
    """
    if not text:
        return text
    cleaned = _LIVE_LINK_RE.sub("[直播链接已移除，请从系统获取最新链接]", text)
    # 清理可能残留的空 markdown 链接 [文字]()
    cleaned = _re_mod.sub(r'\[([^\]]*)\]\(\s*\)', r'\1', cleaned)
    return cleaned


# 用户主动要直播链接时 LLM 输出的标记，后端检测到就调 MCP 拉取
_LIVE_LINK_MARKER = "[LIVE_LINK]"


async def _fetch_live_link_direct(prime_info: dict[str, Any], external_userid: str) -> str | None:
    """用户主动要直播链接时，直接从 MCP 拉取（绕过温度档逻辑）。

    复用 sales.actions._get_live_url 获取链接，用简化文案返回。
    """
    try:
        from deeptutor.sales.actions import _get_live_url
        corpid = str(prime_info.get("corpid") or "") or None
        qywx_uid = str(prime_info.get("qywxUserid") or "") or None
        url = await _get_live_url(
            corpid=corpid,
            external_userid=external_userid,
            qywx_userid=qywx_uid,
            qywx_userid_fallback=qywx_uid,
            third_sale_uuid_fallback=str(prime_info.get("thirdSaleUuid") or "") or None,
            third_user_id_fallback=int(prime_info.get("thirdUserId")) if prime_info.get("thirdUserId") is not None else None,
            vid_fallback=int(prime_info.get("vid")) if prime_info.get("vid") is not None else None,
        )
        if not url:
            logger.warning("[sale_chat.live_request] MCP returned no live url")
            return None
        return f"\n\n您要的直播链接来啦：\n{url}\n开播前 15 分钟进群还能拿专属预习资料~"
    except Exception as e:
        logger.warning("[sale_chat.live_request] failed: %s", e)
        return None


async def _fetch_kb_context(query: str, partner_id: str = SOUL_PARTNER_ID) -> tuple[str, float]:
    """走 qdrant 知识库检索, 把相关话术/知识片段压缩后注入 system prompt.

    返回 (kb_context, max_score)。max_score 是所有 KB 召回结果中最高的
    qdrant 相似度分数, 用于判断置信度是否足够。

    与 runtime 一致: 列出 partner 绑定的 KB (kb_strategy 过滤), 逐个
    RAGService.search, 取 content 前若干字。任何一步失败都降级为空串,
    不阻断主链路。

    注意: 知识库中可能包含历史过期的直播回放链接, 会在此处被清除。
    直播链接应从 MCP 接口实时获取, 不使用知识库中的历史链接。
    """
    if not (query or "").strip():
        return "", 0.0
    try:
        from deeptutor.knowledge.manager import KnowledgeBaseManager
        from deeptutor.services.partners.workspace import (
            apply_kb_strategy,
            ensure_partner_workspace,
            read_partner_config,
        )
        from deeptutor.services.rag.service import RAGService

        kb_root = ensure_partner_workspace(partner_id) / "knowledge_bases"
        if not kb_root.is_dir():
            logger.info("[sale_chat.kb] no kb_root dir at %s, skip retrieval", kb_root)
            return "", 0.0
        kb_names = apply_kb_strategy(
            KnowledgeBaseManager(base_dir=str(kb_root)).list_knowledge_bases(),
            read_partner_config(partner_id).get("kb_strategy", "equal_weight"),
        )
        if not kb_names:
            logger.info("[sale_chat.kb] no KBs bound, skip retrieval")
            return "", 0.0

        svc = RAGService(kb_base_dir=str(kb_root))
        snippets: list[str] = []
        max_score: float = 0.0
        for kb in kb_names[:3]:
            try:
                t0 = time.perf_counter()
                res = await svc.search(query=query[:200], kb_name=kb)
                content = str(res.get("content") or res.get("answer") or "").strip()
                # 提取 sources 里的 score
                sources = res.get("sources") or []
                for src in sources:
                    if isinstance(src, dict):
                        sc = src.get("score")
                        if isinstance(sc, (int, float)) and sc > max_score:
                            max_score = float(sc)
                logger.info(
                    "[sale_chat.kb ←✓] kb=%s | elapsed_ms=%d | content_chars=%d | max_score=%.4f",
                    kb, int((time.perf_counter() - t0) * 1000), len(content), max_score,
                )
                if content:
                    # 清除知识库中的历史过期直播链接
                    content = _strip_live_links(content)
                    snippets.append(f"【{kb}】{content[:1200]}")
            except Exception as e:
                logger.warning("[sale_chat.kb ←✗] kb=%s | FAILED=%s", kb, e)
        if not snippets:
            return "", max_score
        return "\n\n".join(snippets)[:3000], max_score
    except Exception as e:
        logger.warning("[sale_chat.kb] retrieval failed, degrade to empty: %s", e)
        return "", 0.0


async def _fetch_history(
    corpid: str, external_userid: str, session_id: str, limit: int = 6
) -> list[dict[str, str]]:
    try:
        from deeptutor.services.shirley import profile as shirley_profile
        history = await shirley_profile.query_chat_history(
            corpid, external_userid, limit=limit * 2, offset=0
        )
        if isinstance(history, dict):
            messages = [m for m in (history.get("messages") or []) if isinstance(m, dict)]

            # Shirley 5.2 返回的是倒序（最新在前）, 必须转成正序时间线,
            # 否则 LLM 会看到倒放的对话, 答非所问
            def _sort_key(m: dict[str, Any]) -> str:
                return str(m.get("createdAt") or m.get("sendTime") or "")

            if messages and any(_sort_key(m) for m in messages):
                messages.sort(key=_sort_key)

            result: list[dict[str, str]] = []
            for m in messages[-limit * 2:]:
                content = str(m.get("content") or m.get("text") or "")
                if not content:
                    continue
                # 历史里可能残留之前误推的工具调用碎片, 清洗掉再喂给 LLM
                content = _strip_tool_calls(content)
                if not content:
                    continue
                role = str(m.get("role") or m.get("type") or "")
                if role not in ("user", "assistant"):
                    # senderType: 0=销售/Lisa(assistant), 1=客户(user)
                    st = m.get("senderType")
                    role = "assistant" if st == 0 else "user" if st == 1 else ""
                if role and content:
                    result.append({"role": role, "content": content})
            if result:
                logger.info(
                    "[sale_chat._fetch_history] turns=%d | first=%s… | last=%s…",
                    len(result), result[0]["content"][:20], result[-1]["content"][:20],
                )
                return result
    except Exception as e:
        logger.warning("[sale_chat._fetch_history] Shirley 5.2 failed, degrade to empty: %s", e)

    try:
        from deeptutor.services.partners import get_partner_manager  # noqa: F401
    except Exception:
        pass

    return []


# ── 主处理链路 ──

_MEDIA_URL_RE = re.compile(r"^https?://\S+$")


def _humanize_aggregated(text: str) -> str:
    """把纯媒体消息（空 content / 裸图片链接）转成 LLM 能理解的描述。

    真实图片消息 content 往往是 "" 或一个 mmecoa 链接, 模型对着裸链接
    会返回空。转成 "[客户发了一张图片]" 后模型才知道该怎么接话。
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "[客户发了一张图片或语音]"
    out: list[str] = []
    for ln in lines:
        if _MEDIA_URL_RE.match(ln):
            out.append("[客户发了一张图片]")
        else:
            out.append(ln)
    return "\n".join(out)


def _collect_image_urls(messages: list[dict[str, Any]]) -> list[str]:
    """从聚合消息里挑出 msgType=101 的图片 URL。"""
    urls: list[str] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if m.get("msgType") != _MSG_TYPE_IMAGE:
            continue
        content = str(m.get("content") or "").strip()
        if content and _MEDIA_URL_RE.match(content):
            urls.append(content)
    return urls


async def _describe_images(image_urls: list[str]) -> str:
    """用视觉模型读取图片内容, 返回一段文字描述（供 KB 检索 + LLM 回答用）。

    主模型不支持 vision, 这里单独用 _VISION_MODEL 走同一网关。
    LLM 网关无法直接下载外网图片(企微 mmecoa 链接等), 所以先本地下载
    转 base64 data URL 再传给视觉模型。
    任何失败都降级为空串, 不阻断主链路。
    """
    if not image_urls or not _VISION_MODEL:
        return ""
    try:
        import base64 as _b64

        import httpx

        from deeptutor.services.llm import factory as llm_factory
        from deeptutor.services.llm import get_llm_client

        cfg = get_llm_client().config

        # 本地下载图片 → base64 data URL (网关拉不到外网图)
        content_parts: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "请描述这张图片的内容，重点说明：这是什么学科/科目的资料、"
                    "是试卷/练习册/课本/手写笔记/截图中的哪一类、"
                    "涉及哪些知识点或题目。用中文简洁描述，2-4 句。"
                ),
            }
        ]
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
            for u in image_urls[:3]:
                try:
                    resp = await http.get(u)
                    resp.raise_for_status()
                    mime = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                    if not mime.startswith("image/"):
                        mime = "image/jpeg"
                    data_url = f"data:{mime};base64,{_b64.b64encode(resp.content).decode('ascii')}"
                    content_parts.append({"type": "image_url", "image_url": {"url": data_url}})
                except Exception as dl_err:
                    logger.warning("[sale_chat.vision] download image FAILED | url=%s | err=%s", u[:80], dl_err)

        if len(content_parts) <= 1:
            logger.warning("[sale_chat.vision] no image downloaded, skip vision")
            return ""

        messages = [{"role": "user", "content": content_parts}]
        t0 = time.perf_counter()
        desc = await llm_factory.complete(
            prompt="",
            system_prompt="你是一个图片内容识别助手，负责把图片里的学习内容转成文字描述。",
            model=_VISION_MODEL,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            binding=getattr(cfg, "binding", "openai"),
            messages=messages,
        )
        desc = (desc or "").strip()
        logger.info(
            "[sale_chat.vision ←✓] images=%d | elapsed_ms=%d | desc_chars=%d",
            len(image_urls), int((time.perf_counter() - t0) * 1000), len(desc),
        )
        return desc
    except Exception as e:
        logger.warning("[sale_chat.vision ←✗] images=%d | FAILED=%s", len(image_urls), e)
        return ""


async def _kb_can_answer(question: str, kb_context: str) -> bool:
    """用 LLM 判断知识库召回内容能否回答用户问题（降级转人工的闸门）。

    纯向量 score 阈值区分度不够（域内 0.65-0.81 vs 无关 0.61-0.65 间隙太窄），
    所以用 LLM 做最终相关性判定。LLM 失败时放行（不误伤正常对话）。
    """
    if not (question or "").strip() or not (kb_context or "").strip():
        return False
    try:
        from deeptutor.services.llm import get_llm_client
        llm = get_llm_client()
        prompt = (
            "判断下面的【参考资料】能否回答【用户问题】。\n"
            "只回答一个字：能 或 不能。\n\n"
            f"【用户问题】{question[:200]}\n\n"
            f"【参考资料】{kb_context[:1500]}"
        )
        reply = await llm.complete(prompt)
        text = (reply or "").strip()
        can = "能" in text and "不能" not in text
        logger.info(
            "[sale_chat.kb_judge ←✓] question=%s | can_answer=%s | reply=%s",
            question[:40], can, text[:20],
        )
        return can
    except Exception as e:
        logger.warning("[sale_chat.kb_judge ←✗] FAILED=%s, 放行不误伤", e)
        return True


@dataclass
class _TraceOutcome:
    reply: str = ""
    transferred: bool = False
    explicit_refusal: bool = False
    llm_empty: bool = False


async def _process_session(
    *,
    session_id: str,
    messages: list[dict[str, Any]],
    corpid: str,
    external_userid: str,
    prime_info: dict[str, Any],
) -> None:
    from deeptutor.observability.agent_monitor import has_mcp_failed, reset_trace_state

    reset_trace_state()
    outcome = _TraceOutcome()
    trace_input = {
        "messages": messages,
        "corpid": corpid,
        "externalUserid": external_userid,
        "primeInfo": prime_info,
    }
    with observation(
        "saleChat.turn",
        input=trace_input,
        metadata={"session_id": session_id, "message_count": len(messages)},
        as_type="chain",
    ) as root_span:
        if root_span is not None:
            root_span.update_trace(
                name="saleChat.turn",
                session_id=session_id,
                user_id=redact_pii(external_userid, key="externalUserid"),
                input=redact_pii(trace_input),
                tags=["saleChat", os.getenv("LANGFUSE_ENVIRONMENT", "development")],
            )
        await _process_session_core(
            session_id=session_id,
            messages=messages,
            corpid=corpid,
            external_userid=external_userid,
            prime_info=prime_info,
            trace_outcome=outcome,
        )
        bad_case = detect_bad_case(
            reply=outcome.reply,
            mcp_failed=has_mcp_failed(),
            llm_empty=outcome.llm_empty,
            transferred=outcome.transferred,
            explicit_refusal=outcome.explicit_refusal,
        )
        if root_span is not None:
            root_span.update(
                output=redact_pii({"reply": outcome.reply, "bad_case_reasons": bad_case.reasons}),
                metadata={"bad_case": bad_case.is_bad, "bad_case_reasons": list(bad_case.reasons)},
            )
            root_span.update_trace(
                output=redact_pii(outcome.reply),
                tags=["saleChat", "bad-case"] if bad_case.is_bad else ["saleChat", "normal"],
            )
            root_span.score_trace(
                name="bad_case",
                value=bad_case.is_bad,
                data_type="BOOLEAN",
                comment=",".join(bad_case.reasons) if bad_case.reasons else "normal",
            )


async def _process_session_core(
    *,
    session_id: str,
    messages: list[dict[str, Any]],
    corpid: str,
    external_userid: str,
    prime_info: dict[str, Any],
    trace_outcome: _TraceOutcome,
) -> None:
    raw_text = "\n".join(m.get("content", "") for m in messages if m.get("content")).strip()
    aggregated_text = _humanize_aggregated(raw_text)

    logger.info(
        "[sale_chat.process] session=%s | msgs=%d | chars=%d | corpid=%s | ext=%s",
        session_id, len(messages), len(aggregated_text), corpid, external_userid,
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("[sale_chat.process] session=%s | aggregated_text=%s", session_id, aggregated_text[:300])

    try:
        # ⓪ 图片识别 + PG ensure 互不依赖, 提前并行启动 (省 ~30-80ms)
        image_urls = _collect_image_urls(messages)
        _pg_task = asyncio.create_task(_pg_ensure_customer_conv(external_userid))
        image_desc = ""
        if image_urls:
            image_desc = await _describe_images(image_urls)
            if image_desc:
                aggregated_text = f"{aggregated_text}\n[客户图片内容识别] {image_desc}".strip()
                logger.info(
                    "[sale_chat.image] session=%s | images=%d | desc_chars=%d",
                    session_id, len(image_urls), len(image_desc),
                )

        # PG ensure 结果 (提前启动, 此时大概率已完成)
        pg_cust_row: dict[str, Any] | None = None
        pg_conv_id: str | None = None
        pg_pair = await _pg_task
        if pg_pair:
            pg_cust_row, pg_conv_id = pg_pair

        # ①②②.5 Shirley 画像+历史+QDrant + PG history 合成四者互不依赖, gather 并行
        kb_query = image_desc if image_desc else aggregated_text
        t_fetch = time.perf_counter()
        (
            profile_summary,
            history,
            (kb_context, kb_score),
        ) = await asyncio.gather(
            _fetch_profile_summary(corpid, external_userid),
            _fetch_history(corpid, external_userid, session_id),
            _fetch_kb_context(kb_query),
        )
        logger.info(
            "[sale_chat.parallel_fetch] session=%s | total_ms=%d | history_turns=%d | kb_chars=%d | max_score=%.4f",
            session_id, int((time.perf_counter() - t_fetch) * 1000),
            len(history), len(kb_context), kb_score,
        )

        # ── PG memory 层: user 消息进 PG + 合成 history ────────────
        history_summary_inject: str | None = None
        if pg_conv_id:
            await _pg_append_message(pg_conv_id, "customer", aggregated_text)
            pg_window, history_summary_inject = await _pg_build_history_for_llm(
                pg_conv_id
            )
            # PG 有数据 → 用 PG 滑窗替换 Shirley history (PG 按 seq 更稳定);
            # PG 还没数据 (第一次对话) → 保留 Shirley history 作为初始上下文
            if pg_window:
                history = pg_window
                logger.info(
                    "[sale_chat.pg.memory] using PG window turns=%d | summary=%s",
                    len(pg_window), bool(history_summary_inject),
                )

        # 是否需要知识库支撑: 问题命中产品/课程类关键词才算"需要 KB 回答"。
        # 图片消息用 KB 做增强(匹配上就用), 但不作为降级闸门 ——
        # 家长发作业/资料图不是"需要知识库回答的问题", LLM 看图自然接话即可。
        kb_required = any(kw in raw_text for kw in _KB_REQUIRED_KEYWORDS)

        # ③ sales.service: 打标签 + 算温度档 + 判定 explicit_refusal + next_action
        #    这步内部会跑 LLM tagger + regex 补漏，比单独 LLM 拒绝判断更精确
        from deeptutor.sales import service as sales_service
        from deeptutor.services.llm import get_llm_client
        t0 = time.perf_counter()
        try:
            cust_profile, action_text = await sales_service.process_customer_message(
                customer_msg=aggregated_text,
                customer_external_id=external_userid,
                corpid=corpid,
                llm_client=get_llm_client(),
                qywx_userid=str(prime_info.get("qywxUserid") or "") or None,
                qywx_userid_fallback=str(prime_info.get("qywxUserid") or "") or None,
                third_sale_uuid_fallback=str(prime_info.get("thirdSaleUuid") or "") or None,
                third_user_id_fallback=int(prime_info.get("thirdUserId")) if prime_info.get("thirdUserId") is not None else None,
                vid_fallback=int(prime_info.get("vid")) if prime_info.get("vid") is not None else None,
                _existing_cust_row=pg_cust_row,
            )
            elapsed_sales = int((time.perf_counter() - t0) * 1000)
            intent_temperature = getattr(cust_profile, "intent_temperature", "unknown")
            explicit_refusal = bool(getattr(cust_profile, "explicit_refusal", False))
            next_action = getattr(cust_profile, "next_action", "none")
            refusal_reason = ""
            if explicit_refusal:
                sigs = getattr(cust_profile, "intent_signals", {}) or {}
                ts = getattr(cust_profile, "tag_source", {}) or {}
                raw_text = str(sigs.get("refusal_text") or "") if isinstance(sigs, dict) else ""
                refusal_reason = _build_refusal_reason(raw_text, ts)
            logger.info(
                "[sale_chat.sales] session=%s | elapsed_ms=%d | temp=%s | refusal=%s | next_action=%s | action_len=%d",
                session_id, elapsed_sales, intent_temperature, explicit_refusal,
                next_action, len(action_text or ""),
            )

            # ③.5 自动打企微标签: cust_profile → tag_qywx 查表 → Shirley 接口 2
            if cust_profile is not None:
                try:
                    from deeptutor.sales import qywx_tags
                    tag_ids = await qywx_tags.tag_ids_for_profile(cust_profile)
                    _qywx_uid = str(prime_info.get("qywxUserid") or "") or None
                    await qywx_tags.apply_tags_to_customer(
                        corpid=corpid,
                        external_userid=external_userid,
                        tag_ids=tag_ids,
                        follow_userid=_qywx_uid,
                    )
                except Exception as tag_err:
                    logger.warning("[sale_chat.tag] session=%s | FAILED=%s", session_id, tag_err)
        except Exception as sales_err:
            elapsed_sales = int((time.perf_counter() - t0) * 1000)
            logger.warning(
                "[sale_chat.sales] session=%s | elapsed_ms=%d | FAILED=%s",
                session_id, elapsed_sales, sales_err,
            )
            cust_profile = None
            action_text = ""
            intent_temperature = None
            explicit_refusal = False
            refusal_reason = ""

        # ④ 拒绝分支: profile.explicit_refusal → 打勿扰标签 + 推 msgType=119 + 写画像 → return
        if explicit_refusal:
            trace_outcome.transferred = True
            trace_outcome.explicit_refusal = True
            trace_outcome.reply = _format_reject_answer(
                refusal_reason or "用户明确拒绝", aggregated_text[:200]
            )
            # ── PG memory: 拒绝回复也进 PG ──────────────────────────────
            if pg_conv_id:
                await _pg_append_message(pg_conv_id, "ai", trace_outcome.reply)
            t0 = time.perf_counter()

            # 4.1 打「勿扰」企微标签
            try:
                from deeptutor.sales import qywx_tags
                _qywx_uid = str(prime_info.get("qywxUserid") or "") or None
                await qywx_tags.apply_do_not_disturb_tag(
                    corpid=corpid,
                    external_userid=external_userid,
                    follow_userid=_qywx_uid,
                )
            except Exception as dnd_err:
                logger.warning("[sale_chat.reject_dnd] session=%s | FAILED=%s", session_id, dnd_err)

            # 4.2 推 msgType=119 转人工，answer 包含 AI 分析原因 + 用户原话
            try:
                await _push_reject(
                    corpid=corpid,
                    external_userid=external_userid,
                    prime_info=prime_info,
                    reason=refusal_reason or "用户明确拒绝",
                    user_original=aggregated_text[:200],
                )
                logger.info(
                    "[sale_chat.reject_push] session=%s | elapsed_ms=%d | DONE",
                    session_id, int((time.perf_counter() - t0) * 1000),
                )
            except Exception as push_err:
                logger.warning("[sale_chat.reject_push] session=%s | FAILED=%s", session_id, push_err)

            # 转人工后清除 refusal 标记, 避免后续每条消息都重复转人工
            if cust_profile is not None:
                cust_profile.explicit_refusal = False
                cust_profile.intent_signals.pop("explicit_refusal", None)
                cust_profile.tag_source.pop("explicit_refusal", None)
                try:
                    from deeptutor.sales import db as sales_db
                    row = await sales_db.get_customer_by_external_id(external_userid)
                    if row:
                        await sales_db.update_customer_profile(
                            customer_id=row["id"],
                            profile_json=cust_profile.to_dict(),
                            intent_temperature=cust_profile.intent_temperature,
                            next_action=cust_profile.next_action,
                        )
                    logger.info("[sale_chat.reject_clear] session=%s | refusal flag cleared", session_id)
                except Exception as clear_err:
                    logger.warning("[sale_chat.reject_clear] session=%s | FAILED=%s", session_id, clear_err)

            await _analyze_and_save_profile(
                corpid=corpid,
                external_userid=external_userid,
                history=history,
                user_text=aggregated_text,
                intent_level=intent_temperature,
            )
            return

        # ④.5 KB 置信度降级: 需要知识库支撑但答不上/置信度低 → 转人工 (119)
        #     图片消息识别后 KB 匹配不上, 或产品/课程类问题 KB 召回弱, 都走人工
        #     三段判定: score < 阈值 → 直接转人工; score >= 阈值+0.10 → 放行;
        #     中间模糊区 → LLM 判定召回内容能否回答问题
        kb_degraded = False
        degrade_reason = ""
        if kb_required:
            if not kb_context or kb_score < _KB_CONFIDENCE_THRESHOLD:
                kb_degraded = True
                degrade_reason = (
                    f"知识库无法有效回答该问题（置信度 {kb_score:.2f} < 阈值 {_KB_CONFIDENCE_THRESHOLD}），"
                    f"已降级转人工处理。用户问题摘要：{raw_text[:80]}"
                )
            elif kb_score < _KB_CONFIDENCE_THRESHOLD + 0.10:
                # 模糊区: LLM 判定召回内容能否回答
                can_answer = await _kb_can_answer(kb_query, kb_context)
                if not can_answer:
                    kb_degraded = True
                    degrade_reason = (
                        f"知识库召回内容与用户问题不匹配（置信度 {kb_score:.2f}），"
                        f"AI 判定无法回答，已降级转人工处理。用户问题摘要：{raw_text[:80]}"
                    )
        if kb_degraded:
            trace_outcome.transferred = True
            trace_outcome.reply = _format_reject_answer(degrade_reason, raw_text[:200])
            # ── PG memory: KB 降级拒绝回复也进 PG ────────────────────────
            if pg_conv_id:
                await _pg_append_message(pg_conv_id, "ai", trace_outcome.reply)
            logger.warning(
                "[sale_chat.kb_degrade] session=%s | kb_required=%s | kb_score=%.4f | threshold=%.2f | → 转人工",
                session_id, kb_required, kb_score, _KB_CONFIDENCE_THRESHOLD,
            )
            try:
                await _push_reject(
                    corpid=corpid,
                    external_userid=external_userid,
                    prime_info=prime_info,
                    reason=degrade_reason,
                    user_original=raw_text[:200],
                )
            except Exception as push_err:
                logger.warning("[sale_chat.kb_degrade] push FAILED=%s", push_err)

            await _analyze_and_save_profile(
                corpid=corpid,
                external_userid=external_userid,
                history=history,
                user_text=aggregated_text,
                intent_level=intent_temperature,
            )
            return

        # ⑤ 正常分支: LLM 流式回复 + 边收边推 (首句立即出, 不再等完整生成)
        full_reply = await _stream_llm_and_push(
            history=history,
            aggregated=aggregated_text,
            corpid=corpid,
            external_userid=external_userid,
            prime_info=prime_info,
            profile_summary=profile_summary,
            kb_context=kb_context,
            history_summary_inject=history_summary_inject,
        )

        # ⑤.5 兜底: LLM 流式完全没出字 → 推兜底话术
        final_reply = (full_reply or "").strip()
        trace_outcome.llm_empty = not final_reply
        if not final_reply:
            try:
                await _push_sentences(
                    corpid=corpid,
                    external_userid=external_userid,
                    prime_info=prime_info,
                    text=_FALLBACK_REPLY,
                )
                final_reply = _FALLBACK_REPLY
                logger.warning(
                    "[sale_chat.fallback] session=%s | LLM returned empty, using fallback reply",
                    session_id,
                )
            except Exception as fb_err:
                logger.warning("[sale_chat.fallback_push] session=%s | FAILED=%s", session_id, fb_err)

        # ⑥ 追加 sales service 的 action_text (直播链接等), 流式结束后再追加推
        if action_text:
            final_reply += action_text
            try:
                await _push_sentences(
                    corpid=corpid,
                    external_userid=external_userid,
                    prime_info=prime_info,
                    text=action_text,
                )
                logger.info(
                    "[sale_chat.sales_action_push] session=%s | pushed %d chars",
                    session_id, len(action_text),
                )
            except Exception as act_err:
                logger.warning("[sale_chat.sales_action_push] session=%s | FAILED=%s", session_id, act_err)

        # ⑥.1 LLM 意图识别: 流式回复里含 [LIVE_LINK] 标记 → 调 MCP 拉取真实链接追加推
        if _LIVE_LINK_MARKER in full_reply:
            logger.info("[sale_chat.live_request] session=%s | LLM emitted [LIVE_LINK] marker", session_id)
            live_text = await _fetch_live_link_direct(prime_info, external_userid)
            if live_text:
                final_reply = final_reply.replace(_LIVE_LINK_MARKER, live_text.strip())
                try:
                    await _push_sentences(
                        corpid=corpid,
                        external_userid=external_userid,
                        prime_info=prime_info,
                        text=live_text.strip(),
                    )
                    logger.info(
                        "[sale_chat.live_request_push] session=%s | pushed %d chars",
                        session_id, len(live_text),
                    )
                except Exception as live_err:
                    logger.warning("[sale_chat.live_request_push] session=%s | FAILED=%s", session_id, live_err)
            else:
                final_reply = final_reply.replace(_LIVE_LINK_MARKER, "")
                logger.warning("[sale_chat.live_request] session=%s | MCP returned no live url, marker removed", session_id)

        trace_outcome.reply = final_reply.strip()

        # ── PG memory 层: AI 回复进 PG + 触发自动总结 ───────────────
        if pg_conv_id and final_reply.strip():
            await _pg_append_message(
                pg_conv_id, "ai", final_reply.strip(),
                llm_model=getattr(get_llm_client(), "model", None),
            )
            # fire-and-forget: 不阻塞主流程
            asyncio.create_task(_pg_maybe_trigger_summary(pg_conv_id))

        # ⑧ Shirley 5.3: 把本轮 sales 判定的 intent_level + 画像分析写回
        await _analyze_and_save_profile(
            corpid=corpid,
            external_userid=external_userid,
            history=history,
            user_text=aggregated_text,
            intent_level=intent_temperature,
        )

    except Exception as e:
        if not trace_outcome.reply:
            trace_outcome.reply = f"处理异常：{type(e).__name__}"
        logger.error(
            "[sale_chat.process] session=%s FAILED | err=%s",
            session_id, e, exc_info=True,
        )


# ── LLM 调用层 ──

# LLM 异常时的兜底话术，保证 reply_lisa_message 一定被调用（核心指标）
_FALLBACK_REPLY = "稍等一下哦，我这边看看～"

# msgType=101 表示客户发的是图片消息（content 为图片 URL）
_MSG_TYPE_IMAGE = 101

# 图片多模态识别用的视觉模型（主模型 qwen3.7-flash 不支持 vision）。
# 走同一个网关 base_url + api_key, 只换 model。留空则跳过图片识别。
_VISION_MODEL = os.getenv("SALE_CHAT_VISION_MODEL", "qwen3.8-max")

# KB 召回置信度阈值: max_score 低于此值视为"知识库答不上来", 触发降级转人工。
# 实测 bge-m3 分布: 域内产品问题 0.65-0.81, 无关问题 ~0.47, 取 0.55 做分界。
_KB_CONFIDENCE_THRESHOLD = float(os.getenv("SALE_CHAT_KB_MIN_SCORE", "0.55"))

# 需要知识库事实支撑才算"答得上"的问题类型关键词。
# 只保留价格/退款/有效期这类必须查知识库硬事实的; 竞品对比、课程咨询、
# 适合几岁等 Lisa 用 soul 人格就能自然回答, 不走 KB 闸门(避免误伤正常销售对话)。
_KB_REQUIRED_KEYWORDS = (
    "多少钱", "价格", "费用", "学费", "优惠", "便宜",
    "退款", "退费", "有效期", "多久",
)

# SOUL.md 缺失/读取失败时的最小兜底人格
_SOUL_FALLBACK = """你是 Lisa —— 雪梨英语的班主任，在微信上跟家长一对一沟通。
- 说话像真人，语气亲切，不用书面语
- 回答简洁不啰嗦，一段 2-3 句
- 先听懂孩子的情况，引导家长说出痛点，不要一开始就推销
- 灵活灵活再灵活，别像机器人"""


# saleChat 链路没有工具执行环节: SOUL.md 里描述的 partner_memorize 等工具
# 在这里调不了, LLM 一旦吐出调用格式就会原文推给真实客户。必须显式禁止。
_NO_TOOL_RULE = """

## 本次对话的硬性输出约束（最高优先级，覆盖以上人格描述里的任何工具说明）
你现在是在微信里直接跟家长发消息，**没有任何工具可以调用**。
- 禁止输出 partner_memorize、tool_call、function_call 等任何工具调用
- 禁止输出 JSON、大括号 {}、``` 代码块
- 禁止输出 key="value" 这种参数写法
- 只输出你要发给家长的纯中文口语句子，别的一个字都不要加

**唯一例外：直播链接 [LIVE_LINK]**
只有当家长**明确表达了想看直播/试听课/回放的意愿**时，你才在回复末尾加上 [LIVE_LINK] 标记（独占一行）。系统会自动替换成最新的有效直播链接。
严格判定标准——必须同时满足：
1. 家长的话里有"直播""试听课""公开课""回放"等**课程观看类**词汇
2. 且家长是在**要求获取链接/参与/观看**，而不是在拒绝、抱怨或陈述其他事情
正面例子（应加 [LIVE_LINK]）：
- "给我发个直播链接" / "怎么看电视直播" / "直播在哪看" / "我想试听一下" / "有回放链接吗"
反面例子（绝对不加 [LIVE_LINK]）：
- "你们课程有链接吗"（问的是课程介绍，不是直播）
- "发个购买链接"（要的是下单链接，不是直播）
- "我在别的链接买过了"（陈述事实）
- "直播太贵了"（抱怨价格，不是要链接）
- "课程链接发我看看"（要看课程资料，不是直播）
如果不确定家长是否要直播链接，**不要加** [LIVE_LINK]，先口头引导确认。
你不需要、也绝对不能自己编链接。
你输出的每一个字（除了 [LIVE_LINK] 标记）都会被原样发到家长微信里，所以只能是人话。"""


def _build_system_prompt(
    profile_summary: str = "",
    kb_context: str = "",
) -> str:
    """动态 Lisa soul = SOUL.md 基座 + qdrant 知识 + 画像摘要。

    每轮对话重新读盘，所以在 Soul 管理后台改完人格下一轮立即生效。
    读不到就回落到 _SOUL_FALLBACK，不阻断对话。
    """
    soul = ""
    try:
        from deeptutor.services.partners.workspace import read_soul
        soul = (read_soul(SOUL_PARTNER_ID) or "").strip()
    except Exception as e:
        logger.warning("[sale_chat.soul] read_soul(%s) failed: %s", SOUL_PARTNER_ID, e)

    if soul:
        logger.info("[sale_chat.soul] loaded SOUL.md | partner=%s | chars=%d", SOUL_PARTNER_ID, len(soul))
    else:
        soul = _SOUL_FALLBACK
        logger.warning("[sale_chat.soul] SOUL.md empty/missing, using fallback persona")

    parts = [soul]
    if kb_context:
        parts.append(
            "\n\n## 知识库检索结果（只作参考弹药, 用自己的话说, 竞品内容只做对比）\n"
            "注意：检索结果中可能残留的历史直播链接已过期失效，**绝对不要**在回复中包含任何链接。"
            "如果家长要直播链接，系统会自动从 MCP 接口获取最新的有效链接推送。\n\n"
            + kb_context
        )
    if profile_summary:
        parts.append(f"\n\n{profile_summary}")
    parts.append(_NO_TOOL_RULE)
    return "".join(parts)


# 流式切句的硬终止符 (与 sentence_split._HARD_TERMINATORS 保持一致)
_STREAM_SENTENCE_END_RE = re.compile(r'[。！？.!?\n]')


def _find_stream_sentence_end(text: str) -> int | None:
    """在 LLM 流式累积文本中找到第一个完整句子的结束位置。

    返回终止符字符本身的 index, 没找到返回 None。
    """
    m = _STREAM_SENTENCE_END_RE.search(text)
    if m:
        return m.end() - 1
    return None


async def _push_one_sentence(
    sentence: str,
    *,
    corpid: str,
    external_userid: str,
    prime_info: dict[str, Any],
    third_uuid: str,
    third_uid: int | None,
    label: str,
    idx: int | None = None,
) -> bool:
    """推一条清洗后的句子到 Shirley, 带 typing 延迟 + 统一的 log/error 处理.

    返回 True=推送成功, False=推送失败.
    """
    from deeptutor.services.partners.sentence_split import TypingDelay
    from deeptutor.observability.agent_monitor import mark_mcp_failed
    from deeptutor.services.shirley import qywx

    safe = _strip_tool_calls(sentence.strip())
    if not safe or _LIVE_LINK_MARKER in safe:
        return True

    typing_delay = TypingDelay(
        base=TYPING_BASE_DELAY, per_char=TYPING_PER_CHAR, max_delay=TYPING_MAX_DELAY,
    )
    sleep_for = typing_delay.for_sentence(safe)
    tag = f"[{label}] idx={idx} | " if idx is not None else f"[{label}] "
    logger.info(
        f"[sale_chat.{label}_typing] chars=%d | sleep=%.2fs",
        len(safe), sleep_for,
    )
    await asyncio.sleep(sleep_for)
    try:
        await qywx.send_lisa_message(
            corpid=corpid,
            external_userid=external_userid,
            msg_text=safe,
            third_sale_uuid=third_uuid or None,
            third_user_id=third_uid,
            msg_type=qywx._MSG_TYPE_TEXT,
            disable_dedup=True,
            original_user_id=str(prime_info.get("originalUserId") or "") or None,
            customer_name=str(prime_info.get("customerName") or "") or None,
            qywx_userid=str(prime_info.get("qywxUserid") or "") or None,
            is_prod=prime_info.get("isProd"),
        )
        logger.info(f"[sale_chat.{label}_push] OK | {tag}chars=%d", len(safe))
        return True
    except Exception as push_err:
        mark_mcp_failed()
        logger.warning(
            f"[sale_chat.{label}_push] FAIL | {tag}err=%s | text=%s",
            push_err, safe[:80],
        )
        return False


async def _stream_llm_and_push(
    *,
    history: list[dict[str, str]],
    aggregated: str,
    corpid: str,
    external_userid: str,
    prime_info: dict[str, Any],
    profile_summary: str = "",
    kb_context: str = "",
    history_summary_inject: str | None = None,
) -> str:
    """流式调 LLM, 边收 chunk 边检测完整句子, 立即推送.

    返回累积的完整文本 (用于后续 action_text 追加和 [LIVE_LINK] 标记处理).
    首句在流式过程中就已推到家长微信, 不再等完整生成.

    history_summary_inject: 可选的长会话摘要, 有值时拼进 system prompt,
        作为滑窗之外更久远历史的压缩记忆.
    """
    t0 = time.perf_counter()
    from deeptutor.observability.agent_monitor import mark_mcp_failed
    from deeptutor.services.llm import get_llm_client
    from deeptutor.services.llm import factory as llm_factory

    llm = get_llm_client()
    cfg = llm.config

    system_prompt = _build_system_prompt(profile_summary, kb_context=kb_context)
    # 长会话摘要注入: 追加到 system prompt 最前面 (让 LLM 先看到宏观背景)
    if history_summary_inject:
        system_prompt = f"{history_summary_inject}\n\n---\n\n{system_prompt}"

    clean_history: list[dict[str, str]] = []
    for h in history:
        role = h.get("role", "user")
        if role not in ("user", "assistant", "system"):
            role = "user"
        content = h.get("content", "")
        if content:
            clean_history.append({"role": role, "content": content})

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(clean_history)
    messages.append({"role": "user", "content": aggregated})

    third_uuid = str(prime_info.get("thirdSaleUuid") or "")
    third_uid = prime_info.get("thirdUserId")
    if third_uid is not None:
        try:
            third_uid = int(third_uid)
        except (TypeError, ValueError):
            third_uid = None

    logger.info(
        "[sale_chat._stream_llm_and_push →] soul_chars=%d | history_turns=%d "
        "| prompt_chars=%d | msgs=%d",
        len(system_prompt), len(clean_history), len(aggregated), len(messages),
    )

    full_parts: list[str] = []
    sentence_buffer: list[str] = []
    sentence_count = 0
    in_think_block = False

    try:
        async for chunk in llm_factory.stream(
            aggregated,
            system_prompt=system_prompt,
            model=cfg.model,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            api_version=getattr(cfg, "api_version", None),
            binding=getattr(cfg, "binding", "openai"),
            reasoning_effort=getattr(cfg, "reasoning_effort", None),
            extra_headers=getattr(cfg, "extra_headers", None),
            messages=messages,
        ):
            if chunk == "<think>":
                in_think_block = True
                continue
            if chunk == "</think>":
                in_think_block = False
                continue
            if in_think_block:
                continue
            if not chunk:
                continue

            full_parts.append(chunk)
            sentence_buffer.append(chunk)

            buffer_text = "".join(sentence_buffer)
            while True:
                end_idx = _find_stream_sentence_end(buffer_text)
                if end_idx is None:
                    break

                sentence = buffer_text[: end_idx + 1].strip()
                buffer_text = buffer_text[end_idx + 1:]
                sentence_buffer = [buffer_text] if buffer_text else []

                if sentence:
                    if _LIVE_LINK_MARKER in sentence:
                        logger.debug("[sale_chat.stream_push] skip LIVE_LINK marker | raw=%r", sentence)
                        continue
                    if _strip_tool_calls(sentence):
                        sentence_count += 1
                        await _push_one_sentence(
                            sentence,
                            corpid=corpid, external_userid=external_userid,
                            prime_info=prime_info, third_uuid=third_uuid,
                            third_uid=third_uid, label="stream", idx=sentence_count,
                        )
    except Exception as stream_err:
        logger.error(
            "[sale_chat._stream_llm_and_push] FAILED | elapsed_ms=%d | err=%s",
            int((time.perf_counter() - t0) * 1000), stream_err, exc_info=True,
        )

    tail = "".join(sentence_buffer).strip()
    if tail and _LIVE_LINK_MARKER not in tail and _strip_tool_calls(tail):
        sentence_count += 1
        await _push_one_sentence(
            tail,
            corpid=corpid, external_userid=external_userid,
            prime_info=prime_info, third_uuid=third_uuid,
            third_uid=third_uid, label="stream_tail", idx=sentence_count,
        )

    full_reply = "".join(full_parts).strip()
    logger.info(
        "[sale_chat._stream_llm_and_push] elapsed_ms=%d | full_chars=%d | sentences=%d",
        int((time.perf_counter() - t0) * 1000), len(full_reply), sentence_count,
    )
    return full_reply


async def _llm_reply(
    history: list[dict[str, str]],
    aggregated: str,
    *,
    profile_summary: str = "",
    kb_context: str = "",
) -> str:
    t0 = time.perf_counter()
    try:
        from deeptutor.services.llm import get_llm_client
        llm = get_llm_client()

        # 动态 Lisa soul（SOUL.md 实时读盘）+ 产品铁律 + qdrant 知识 + 画像摘要
        system_prompt = _build_system_prompt(profile_summary, kb_context=kb_context)

        clean_history: list[dict[str, str]] = []
        for h in history:
            role = h.get("role", "user")
            if role not in ("user", "assistant", "system"):
                role = "user"
            content = h.get("content", "")
            if content:
                clean_history.append({"role": role, "content": content})

        # 注意: factory._build_messages 一旦收到 messages/history 就会整包直接用,
        # 丢掉 system_prompt 和 prompt。所以这里必须自己拼全量 messages,
        # 否则 soul 和用户当前这句都进不了模型。
        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        messages.extend(clean_history)
        messages.append({"role": "user", "content": aggregated})

        logger.info(
            "[sale_chat._llm_reply →] soul_chars=%d | history_turns=%d | profile_len=%d "
            "| kb_len=%d | prompt_chars=%d | msgs=%d",
            len(system_prompt), len(clean_history), len(profile_summary or ""),
            len(kb_context or ""), len(aggregated), len(messages),
        )

        reply = await llm.complete(
            aggregated,
            system_prompt=system_prompt,
            history=messages,
        )
        text = (reply or "").strip() if isinstance(reply, str) else str(reply or "").strip()
        logger.info(
            "[sale_chat._llm_reply ←✓] elapsed_ms=%d | reply_chars=%d",
            int((time.perf_counter() - t0) * 1000), len(text),
        )
        return text
    except Exception as e:
        logger.error(
            "[sale_chat._llm_reply ←✗] elapsed_ms=%d | FAILED=%s",
            int((time.perf_counter() - t0) * 1000), e, exc_info=True,
        )
        return ""


# ── MCP 推送层 ──

# SOUL.md 里描述了 partner_memorize 等工具，但 saleChat 没有工具执行环节，
# LLM 吐出的工具调用 JSON 会原文漏给真实客户。推送前必须剥掉。
_FENCE_RE = re.compile(r"```(?:json|tool_code|python)?\s*.*?(?:```|\Z)", re.S)

# ① JSON 风格: {"tool_name": "partner_memorize", "arguments": {...}}
_TOOL_JSON_RE = re.compile(
    r"\{[^{}]*[\"'](?:tool_name|tool|name|arguments|parameters|data)[\"']\s*:.*?(?:\}\s*\}|\}|\Z)",
    re.S,
)

# ② 函数调用风格: tool_call: partner_memorize(name=\"豆包\", grade=\"三年级\")
#    LLM 实测会吐这种, 且 partner / _memorize 可能被换行拆开
_TOOL_CALL_RE = re.compile(
    r"(?:tool_call|tool|function_call|调用工具)\s*[:：]?\s*"
    r"[A-Za-z_][\w\s]*?_?[\w]*\s*\(.*?(?:\)|\Z)",
    re.S,
)

# ③ 裸参数残片: name=\"x\"  op=\"add\"  score=\"62\")
_KV_ARG_RE = re.compile(r"[A-Za-z_]\w*\s*=\s*\\?[\"'][^\"']*\\?[\"']\s*[,)]?")

_TOOL_LINE_RE = re.compile(
    r"^\s*(?:[{}\[\]()]+|[\"']?(?:tool_name|tool|tool_call|arguments|parameters|data|preference"
    r"|grade|pain_points|owned_products|child_name|op|score|name)[\"']?\s*[:=].*"
    r"|\\?[\"'].*\\?[\"']\s*,?)\s*$"
)

# 出现这些词就说明该行是工具调用残片, 整行丢掉
_TOOL_MARKERS = ("tool_call", "partner_memorize", "_memorize", "function_call",
                 "tool_name", "arguments")


def _strip_tool_calls(text: str) -> str:
    """剥掉 LLM 回复里的工具调用（JSON / 代码块 / 函数调用风格），只留自然语言。

    必须在分句之前调用: 否则 split_sentences 会把
    `partner_memorize(name="豆包", grade="三年级")` 按逗号切成好几句,
    每句都单独推给客户。
    """
    if not text:
        return ""

    cleaned = _FENCE_RE.sub(" ", text)
    cleaned = _TOOL_JSON_RE.sub(" ", cleaned)
    cleaned = _TOOL_CALL_RE.sub(" ", cleaned)

    # 逐行兜底
    kept: list[str] = []
    for line in cleaned.splitlines():
        low = line.lower()
        if any(mk in low for mk in _TOOL_MARKERS):
            continue
        if _TOOL_LINE_RE.match(line):
            continue
        kept.append(line)
    cleaned = "\n".join(kept)

    # 漏网的 key="value" 残片
    cleaned = _KV_ARG_RE.sub(" ", cleaned)

    # 残留的转义引号 / 孤立括号
    cleaned = re.sub(r'\\+"', '"', cleaned)
    cleaned = re.sub(r"^[\s{}\[\](),]+|[\s{}\[\](),]+$", "", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


async def _push_sentences(
    *,
    corpid: str,
    external_userid: str,
    prime_info: dict[str, Any],
    text: str,
) -> None:
    if not text:
        return

    safe_text = _strip_tool_calls(text)
    if safe_text != text:
        logger.warning(
            "[sale_chat.sanitize] stripped tool-call JSON | before=%d chars | after=%d chars",
            len(text), len(safe_text),
        )
    if not safe_text:
        logger.warning("[sale_chat.sanitize] nothing left after strip, using fallback")
        safe_text = _FALLBACK_REPLY
    text = safe_text

    sentences = split_sentences(text) or [text]
    third_uuid = str(prime_info.get("thirdSaleUuid") or "")
    third_uid = prime_info.get("thirdUserId")
    if third_uid is not None:
        try:
            third_uid = int(third_uid)
        except (TypeError, ValueError):
            third_uid = None

    delay = TypingDelay(base=TYPING_BASE_DELAY, per_char=TYPING_PER_CHAR, max_delay=TYPING_MAX_DELAY)
    first = True
    idx = 0
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        idx += 1

        # 第一句直接发，后续按实际句长延迟（模拟打字，比真人略快）
        if not first:
            sleep_for = delay.for_sentence(s)
            logger.debug(
                "[sale_chat.push_sentences] typing_wait | next_idx=%d | chars=%d | sleep=%.2fs",
                idx, len(s), sleep_for,
            )
            await asyncio.sleep(sleep_for)
        first = False

        t0 = time.perf_counter()
        try:
            await qywx.send_lisa_message(
                corpid=corpid,
                external_userid=external_userid,
                msg_text=s,
                third_sale_uuid=third_uuid or None,
                third_user_id=third_uid,
                msg_type=qywx._MSG_TYPE_TEXT,
                disable_dedup=True,
                original_user_id=str(prime_info.get("originalUserId") or "") or None,
                customer_name=str(prime_info.get("customerName") or "") or None,
                qywx_userid=str(prime_info.get("qywxUserid") or "") or None,
                is_prod=prime_info.get("isProd"),
            )
            logger.info(
                "[sale_chat.push_sentences] OK | idx=%d/%d | elapsed_ms=%d | text=%s",
                idx, len(sentences), int((time.perf_counter() - t0) * 1000), s[:80],
            )
        except Exception as e:
            mark_mcp_failed()
            logger.warning(
                "[sale_chat.push_sentences] FAIL | idx=%d/%d | err=%s | text=%s",
                idx, len(sentences), e, s[:80],
            )


def _translate_refusal_source(source: str | None) -> str:
    """把 tag_source['explicit_refusal'] 的英文枚举翻译成销售能懂的中文。"""
    _MAP = {
        "regex": "关键词命中",
        "llm": "AI判断",
        "unknown": "自动判定",
        "rule": "规则匹配",
    }
    if not source:
        return "自动判定"
    return _MAP.get(source, source)


def _build_refusal_reason(
    refusal_text: str,
    tag_source: dict[str, Any] | None,
) -> str:
    """把 tagger 吐出的 refusal_text + tag_source 组合成中文拒绝原因（仅分析原因，不含原话）。

    原话统一由 _format_reject_answer 的 "用户原话：" 行承载，避免重复。
    """
    if refusal_text:
        return refusal_text[:200]
    src = ""
    if isinstance(tag_source, dict):
        src = str(tag_source.get("explicit_refusal") or "")
    return f"用户明确拒绝（触发来源：{_translate_refusal_source(src)}）"


def _format_reject_answer(analysis_reason: str, user_original: str) -> str:
    """构造转人工 answer：AI 分析原因 + 用户原话。"""
    return f"转人工原因：{analysis_reason}\n用户原话：{user_original}"


async def _push_reject(
    *,
    corpid: str,
    external_userid: str,
    prime_info: dict[str, Any],
    reason: str,
    user_original: str = "",
) -> None:
    third_uuid = str(prime_info.get("thirdSaleUuid") or "")
    third_uid = prime_info.get("thirdUserId")
    if third_uid is not None:
        try:
            third_uid = int(third_uid)
        except (TypeError, ValueError):
            third_uid = None
    # 119 转人工 Shirley 网关要求 vid 或 thirdUserId, 从 primeInfo 透传 vid
    vid = prime_info.get("vid")
    if vid is not None:
        try:
            vid = int(vid)
        except (TypeError, ValueError):
            vid = None

    # answer 必须同时包含 AI 分析原因和用户原话
    answer = _format_reject_answer(reason or "用户明确拒绝", user_original or reason or "")

    t0 = time.perf_counter()
    try:
        await qywx.send_lisa_message(
            corpid=corpid,
            external_userid=external_userid,
            msg_text=answer,
            third_sale_uuid=third_uuid or None,
            third_user_id=third_uid,
            vid=vid,
            msg_type=qywx._MSG_TYPE_REJECT,
            answer=answer,
            disable_dedup=True,
            original_user_id=str(prime_info.get("originalUserId") or "") or None,
            customer_name=str(prime_info.get("customerName") or "") or None,
            qywx_userid=str(prime_info.get("qywxUserid") or "") or None,
            is_prod=prime_info.get("isProd"),
        )
        logger.info(
            "[sale_chat.push_reject] OK | elapsed_ms=%d | reason=%s",
            int((time.perf_counter() - t0) * 1000), (reason or "")[:200],
        )
    except Exception as e:
        mark_mcp_failed()
        logger.warning("[sale_chat.push_reject] FAIL | elapsed_ms=%d | err=%s", int((time.perf_counter() - t0) * 1000), e)
"""
用户画像域 —— Shirley MCP 5.x

  - fetch_profile       → 5.1 get_user_profile
  - save_analysis       → 5.3 save_user_profile_analysis（修复后的正确 attributes 格式）
  - query_profile_chat_history → 5.2 分页拉聊天记录供分析

保留旧 shirley_profile.py 的兼容 API:
  - summarize_profile
  - should_trigger_analysis
  - analyze_from_dialogue
  - ALL_PROFILE_KEYS

关键变更: 5.3 的 attributes 必须是完整的 **13 项**（新增 intent_level），
每项含 key / values / summary / confidence / evidence 五字段。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ._client import _call_tool

logger = logging.getLogger(__name__)

SHIRLEY_SOURCE = "deeptutor-lisa"

# 文档约定 13 项 key（含 intent_level）
THIRTEEN_ATTRIBUTE_KEYS: tuple[str, ...] = (
    "grade",
    "owned_products",
    "pain_points",
    "level_self_report",
    "school_english_start",
    "available_time",
    "external_classes",
    "price_sensitivity",
    "coaching_ability",
    "multi_child",
    "region_textbook",
    "decision_makers",
    "intent_level",
)

# 旧 shirley_profile.py 的 12 项（不含 intent_level）—— 保持向后兼容
ALL_PROFILE_KEYS: list[str] = list(THIRTEEN_ATTRIBUTE_KEYS)  # 现在是 13 项
ALL_PROFILE_KEYS_NO_INTENT: list[str] = [k for k in THIRTEEN_ATTRIBUTE_KEYS if k != "intent_level"]

# ---- 5.1 查询用户画像 ----

async def fetch_profile(corpid: str, external_userid: str) -> dict[str, Any] | None:
    """调用 Shirley MCP 5.1 get_user_profile 拉取用户画像。

    返回完整 profile dict（含 identity / basic / tags / orders / aiAnalysis 等），
    或 None（工具不可用 / 用户不存在 / 网络错误）。
    """
    if not corpid or not external_userid:
        return None
    try:
        return await _call_tool("get_user_profile", {
            "corpid": corpid,
            "externalUserid": external_userid,
        })
    except Exception as e:
        logger.warning("Shirley fetch_profile failed: %s", e)
        return None


# ---- 5.2 查询画像聊天记录（分页） ----

async def query_profile_chat_history(
    corpid: str,
    external_userid: str,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any] | None:
    """调用 Shirley MCP 5.2 query_user_profile_chat_history 分页拉聊天记录。

    返回 { messages, total, offset, limit, hasMore }，或 None。
    """
    if not corpid or not external_userid:
        return None
    try:
        return await _call_tool("query_user_profile_chat_history", {
            "corpid": corpid,
            "externalUserid": external_userid,
            "limit": max(1, min(500, limit)),
            "offset": offset,
        })
    except Exception as e:
        logger.warning("Shirley query_profile_chat_history failed: %s", e)
        return None


# ---- 5.3 保存画像分析（修复后的正确格式） ----

def build_attributes_payload(
    analysis: dict[str, Any],
    intent_level: str | None = None,
    intent_summary: str = "",
) -> list[dict[str, Any]]:
    """把业务侧的扁平画像 dict 转成 Shirley 5.3 要求的完整 13 项 attributes 数组。

    analysis 可以是部分 key；缺失的字段会以 confidence=0 空值占位。
    """
    attrs: list[dict[str, Any]] = []
    for key in THIRTEEN_ATTRIBUTE_KEYS:
        if key == "intent_level":
            values = [intent_level] if intent_level else []
            attrs.append({
                "key": key,
                "values": values,
                "summary": intent_summary or ("未提取到" if not intent_level else intent_level),
                "confidence": 0.8 if intent_level else 0.0,
                "evidence": [],
            })
            continue
        raw = analysis.get(key)
        if not raw:
            attrs.append({
                "key": key,
                "values": [],
                "summary": "未提取到",
                "confidence": 0.0,
                "evidence": [],
            })
            continue
        if isinstance(raw, list):
            values = [str(v) for v in raw if v]
            summary = "、".join(values)[:100]
        else:
            values = [str(raw)]
            summary = str(raw)[:100]
        attrs.append({
            "key": key,
            "values": values,
            "summary": summary,
            "confidence": 0.7,
            "evidence": [],
        })
    return attrs


async def save_analysis(
    corpid: str,
    external_userid: str,
    analysis: dict[str, Any],
    source: str = SHIRLEY_SOURCE,
    intent_level: str | None = None,
    intent_summary: str = "",
) -> dict[str, Any] | None:
    """调用 Shirley MCP 5.3 save_user_profile_analysis 保存画像分析。

    analysis 是扁平 dict（key → value），函数自动补全缺失字段 + 生成 attributes 数组。
    intent_level 建议值: "high" / "medium" / "low" / "unknown"（对应 cool/warm/hot/blazing）。
    """
    if not corpid or not external_userid or not analysis:
        return None
    idempotency_key = f"profile-{external_userid}-{int(time.time())}"
    attributes = build_attributes_payload(analysis, intent_level, intent_summary)
    try:
        return await _call_tool("save_user_profile_analysis", {
            "corpid": corpid,
            "externalUserid": external_userid,
            "source": source,
            "idempotencyKey": idempotency_key,
            "attributes": attributes,
        })
    except Exception as e:
        logger.warning("Shirley save_analysis failed: %s", e)
        return None


# ---- 画像摘要（注入 system prompt） ----

def summarize_profile(profile: dict[str, Any] | None) -> str:
    """把完整 profile 压缩成 system prompt 能装下的摘要（< 400 字符）"""
    if not profile:
        return ""
    try:
        identity = profile.get("identity") or {}
        basic = profile.get("basic") or {}
        tags = profile.get("tags") or []
        courses = profile.get("purchasedCourses") or []
        ai = profile.get("aiAnalysis") or {}

        parts: list[str] = []
        if identity.get("name"):
            line = f"- 姓名: {identity['name']}"
            if basic.get("phone"):
                line += f" | 手机: {basic['phone']}"
            parts.append(line)
        bits = []
        if basic.get("region"):
            bits.append(basic["region"])
        if basic.get("grade"):
            bits.append(basic["grade"])
        if bits:
            parts.append(f"- 地域年级: {' | '.join(bits)}")
        if courses:
            names = [c.get("name", str(c)) if isinstance(c, dict) else str(c) for c in courses[:5]]
            parts.append(f"- 已购课程: {', '.join(names)}")
        if tags:
            tag_names = [t.get("name", str(t)) if isinstance(t, dict) else str(t) for t in tags[:8]]
            parts.append(f"- 企微标签: {', '.join(tag_names)}")
        if ai:
            ai_brief = []
            for k, v in ai.items():
                if v and k not in ("updatedAt", "timestamp"):
                    ai_brief.append(f"{k}={v}")
            if ai_brief:
                parts.append(f"- 上次AI分析: {'; '.join(ai_brief[:6])}")
        if not parts:
            return ""
        return "【用户画像】\n" + "\n".join(parts)
    except Exception:
        return ""


# ---- 关键词触发 + LLM 画像抽取 ----

_PROFILE_KEYWORDS: dict[str, list[str]] = {
    "grade": ["年级", "几岁", "多大", "九月升", "几年级", "小升初", "中考", "高考", "初一", "初二", "初三", "高一", "高二", "高三", "一年级", "二年级", "三年级", "四年级", "五年级", "六年级"],
    "owned_products": ["挂图", "1500词", "音标", "自然拼读", "牛津树", "RAZ", "海尼曼", "点读笔", "单词卡", "背单词", "闪卡", "点读机"],
    "pain_points": ["不敢开口", "背了忘", "跟不上", "发音不准", "死记硬背", "记不住", "学不进去", "没兴趣", "讨厌英语", "听不懂", "不会读", "容易忘", "不爱开口", "口语差"],
    "level_self_report": ["成绩", "词汇量", "分数", "零基础", "刚启蒙", "入门", "初级", "中级", "高级", "水平", "基础", "期末", "考试", "测试"],
    "school_english_start": ["学校几年级开始", "教材版本", "人教版", "外研版", "译林版", "鲁教版", "湘教版", "北师大版", "学校英语", "课本", "课堂英语"],
    "available_time": ["每天学多久", "晚自习", "作业量", "兴趣班", "时间够", "时间紧", "没时间学", "周末", "有空吗", "能安排"],
    "external_classes": ["学而思", "新东方", "斑马", "线下班", "课外班", "辅导班", "网课", "机构", "VIPKID", "51Talk", "哒哒英语", "魔力耳朵", "伴鱼"],
    "price_sensitivity": ["多少钱", "价格", "贵", "优惠", "便宜", "折扣", "预算", "分期", "划算", "值不值", "太贵了"],
    "coaching_ability": ["我英语不行", "不会教", "发音不准", "没时间管", "辅导不了", "教不好", "没精力", "不会读", "不敢教"],
    "multi_child": ["两个孩子", "两个娃", "老大老二", "大宝二宝", "姐姐弟弟", "哥哥妹妹", "二胎", "三胎", "两个宝宝"],
    "region_textbook": ["北京", "上海", "广东", "江苏", "浙江", "山东", "河南", "四川", "湖南", "湖北", "福建", "安徽", "河北", "陕西", "辽宁", "重庆", "天津", "人教版", "外研版", "译林版"],
    "decision_makers": ["和配偶商量", "跟爸爸商量", "跟妈妈商量", "孩子自己决定", "我说了算", "他爸说了算", "他妈说了算", "一起商量"],
}

def should_trigger_analysis(user_text: str) -> list[str]:
    """返回本轮用户消息命中的画像 key 列表（空 = 不触发分析）。"""
    if not user_text:
        return []
    hits: list[str] = []
    for key, kws in _PROFILE_KEYWORDS.items():
        for kw in kws:
            if kw in user_text:
                hits.append(key)
                break
    return hits


_EXTRACT_PROMPT = """你是一个专业的客户画像分析师。根据下面的对话历史，提取客户画像信息。

只提取你**确信**从对话中能看出的属性；不确定的字段留空字符串。不要编造。

## 对话历史
{history}

## 需要提取的 12 项属性（严格按此 key 名）
1. grade — 年级/年龄（如 "三年级"、"8岁"、"初三"）
2. owned_products — 已购买或提到的产品（如 "牛津树"、"RAZ", 多个用顿号分隔）
3. pain_points — 客户提到的痛点（如 "不敢开口"、"背了忘"）
4. level_self_report — 客户自述英语水平（如 "零基础"、"词汇量500"）
5. school_english_start — 学校英语从几年级开始/教材版本（如 "人教版 三年级起点"）
6. available_time — 每天/每周可用于学习的时间（如 "每天30分钟"、"作业多时间少"）
7. external_classes — 报过的课外班（如 "学而思"、"斑马"）
8. price_sensitivity — 价格敏感度（如 "问价"、"嫌贵"、"要优惠"）
9. coaching_ability — 家长辅导能力（如 "家长英语不好"、"没时间管"）
10. multi_child — 多孩情况（如 "两个孩子"、"老大老二"）
11. region_textbook — 地区/教材（如 "上海"、"外研版"）
12. decision_makers — 决策人（如 "妈妈决定"、"跟爸爸商量"）

## 已有画像（如有）
{prev_analysis}

## 输出格式
只输出严格的 JSON（不要 Markdown，不要解释），格式：
{{
  "grade": "",
  "owned_products": "",
  "pain_points": "",
  ...（其余同格式，共 12 个 key）
}}
"""


async def analyze_from_dialogue(
    history: list[dict[str, Any]],
    user_text: str,
    prev_ai_analysis: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """用 LLM 从对话历史中抽取画像字段。

    仅在 should_trigger_analysis(user_text) 命中后调用。返回 dict 或 None。
    """
    try:
        from deeptutor.runtime.llm_client import get_llm_client
        client = get_llm_client()
    except Exception as e:
        logger.warning("analyze_from_dialogue: LLM client unavailable: %s", e)
        return None

    history_text = ""
    for h in history[-20:]:
        role = h.get("role", "?")
        content = h.get("content", "")
        if content:
            history_text += f"{role}: {content}\n"
    if not history_text.strip():
        history_text = f"user: {user_text}\n"

    prev_str = json.dumps(prev_ai_analysis or {}, ensure_ascii=False)
    prompt = _EXTRACT_PROMPT.format(history=history_text, prev_analysis=prev_str)

    try:
        reply = await client.complete(prompt)
        text = (reply or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
    except Exception as e:
        logger.warning("analyze_from_dialogue: failed to parse LLM reply: %s", e)
        return None

    result: dict[str, Any] = {}
    for key in ALL_PROFILE_KEYS_NO_INTENT:
        v = data.get(key, "")
        if isinstance(v, str):
            v = v.strip()
        if v:
            result[key] = v

    if not result:
        return None

    if prev_ai_analysis:
        merged = dict(prev_ai_analysis)
        for k, v in result.items():
            if v:
                merged[k] = v
        return merged
    return result

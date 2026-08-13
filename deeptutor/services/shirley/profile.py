"""
Shirley 用户画像 Skill
======================

封装 Shirley MCP 5.1 / 5.2 / 5.3 三个画像接口，供 runtime / partner 内部调用。
不注册为 DeepTutor Tool —— 画像预拉（turn 开始）和写回（turn 结束）是 runtime
自动触发的，LLM 不需要直接调用。

对外 4 个函数:
  - fetch_profile(corpid, external_userid) → dict | None      # 5.1
  - query_chat_history(corpid, external_userid, limit, offset) → dict | None  # 5.2
  - save_analysis(corpid, external_userid, attrs, source) → dict | None        # 5.3
  - analyze_and_save(history, user_text, prev_ai, intent_level) → dict | None  # LLM 抽 + 5.3
  - should_trigger_analysis(user_text) → list[str]                            # 关键词命中
  - summarize_profile(profile) → str                                          # system prompt 摘要
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import client

logger = logging.getLogger(__name__)

SHIRLEY_SOURCE = "deeptutor-lisa"

# Shirley 5.3 文档规定的 14 项固定 key（含 intent_level、child_name）
ALL_PROFILE_KEYS: list[str] = [
    "child_name",
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
]

# ── Shirley 5.3 字段长度限制（按文档） ──
# 超过自动截断，summary 额外加"…"，values 数组逐项独立截断
_ATTR_LENGTH_LIMITS: dict[str, int] = {
    "child_name":          10,
    "grade":               15,
    "pain_points":         50,
    "level_self_report":   25,
    "region_textbook":     15,
    "owned_products":      50,
    "school_english_start":25,
    "external_classes":    25,
    "available_time":      25,
    "coaching_ability":    25,
    "multi_child":         20,
    "price_sensitivity":   25,
    "decision_makers":     20,
    # intent_level 是 high/medium/low 短值，不限
}


def _truncate_str(s: Any, limit: int) -> str:
    """截断字符串到 limit 字以内；超了末尾加 "…"。非 str 转 str 再截。"""
    if s is None:
        return ""
    text = str(s)
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)] + "…"


def _truncate_attr(attr: dict[str, Any]) -> dict[str, Any]:
    """对单个 attr dict 做字段级截断（values 数组每项 + summary）。"""
    key = attr.get("key", "")
    limit = _ATTR_LENGTH_LIMITS.get(key)
    if limit is None:
        return attr

    values = attr.get("values") or []
    truncated_vals = [_truncate_str(v, limit) for v in values]

    summary = attr.get("summary") or ""
    truncated_summary = _truncate_str(summary, limit)

    return {**attr, "values": truncated_vals, "summary": truncated_summary}


# 前 13 项（不含 intent_level）
_PROFILE_KEYWORDS: dict[str, list[str]] = {
    "child_name": ["叫什么", "小名叫", "宝贝叫", "孩子叫", "娃叫", "大名", "昵称",
                   "宝宝叫", "闺女叫", "儿子叫", "女儿叫", "我家娃叫"],
    "grade": ["年级", "几岁", "多大", "九月升", "几年级", "小升初", "中考", "高考",
              "初一", "初二", "初三", "高一", "高二", "高三", "一年级", "二年级",
              "三年级", "四年级", "五年级", "六年级"],
    "owned_products": ["挂图", "1500词", "音标", "自然拼读", "牛津树", "RAZ", "海尼曼",
                       "点读笔", "单词卡", "背单词", "闪卡", "点读机"],
    "pain_points": ["不敢开口", "背了忘", "跟不上", "发音不准", "死记硬背", "记不住",
                    "学不进去", "没兴趣", "讨厌英语", "听不懂", "不会读", "容易忘",
                    "不爱开口", "口语差"],
    "level_self_report": ["成绩", "词汇量", "分数", "零基础", "刚启蒙", "入门", "初级",
                          "中级", "高级", "水平", "基础", "期末", "考试", "测试"],
    "school_english_start": ["学校几年级开始", "教材版本", "人教版", "外研版", "译林版",
                              "鲁教版", "湘教版", "北师大版", "学校英语", "课本", "课堂英语"],
    "available_time": ["每天学多久", "晚自习", "作业量", "兴趣班", "时间够", "时间紧",
                       "没时间学", "周末", "有空吗", "能安排"],
    "external_classes": ["学而思", "新东方", "斑马", "线下班", "课外班", "辅导班", "网课",
                         "机构", "VIPKID", "51Talk", "哒哒英语", "魔力耳朵", "伴鱼"],
    "price_sensitivity": ["多少钱", "价格", "贵", "优惠", "便宜", "折扣", "预算",
                          "分期", "划算", "值不值", "太贵了"],
    "coaching_ability": ["我英语不行", "不会教", "发音不准", "没时间管", "辅导不了",
                         "教不好", "没精力", "不会读", "不敢教"],
    "multi_child": ["两个孩子", "两个娃", "老大老二", "大宝二宝", "姐姐弟弟",
                    "哥哥妹妹", "二胎", "三胎", "两个宝宝"],
    "region_textbook": ["北京", "上海", "广东", "江苏", "浙江", "山东", "河南", "四川",
                        "湖南", "湖北", "福建", "安徽", "河北", "陕西", "辽宁", "重庆",
                        "天津", "人教版", "外研版", "译林版"],
    "decision_makers": ["和配偶商量", "跟爸爸商量", "跟妈妈商量", "孩子自己决定",
                        "我说了算", "他爸说了算", "他妈说了算", "一起商量"],
}

_EXTRACT_PROMPT = """你是一个专业的客户画像分析师。根据下面的对话历史，提取客户画像信息。

只提取你**确信**从对话中能看出的属性；不确定的字段留空。不要编造。

## 对话历史
{history}

## 需要提取的 13 项属性（严格按此 key 名，intent_level 由调用方单独判定）
1. child_name — 学员昵称/小名（如 "小明"、"豆豆"）
2. grade — 年级/年龄（如 "三年级"、"8岁"、"初三"）
3. owned_products — 已购买或提到的产品（多个用顿号分隔）
4. pain_points — 客户提到的痛点
5. level_self_report — 客户自述英语水平
6. school_english_start — 学校英语从几年级开始/教材版本
7. available_time — 每天/每周可用于学习的时间
8. external_classes — 报过的课外班
9. price_sensitivity — 价格敏感度
10. coaching_ability — 家长辅导能力
11. multi_child — 多孩情况
12. region_textbook — 地区/教材
13. decision_makers — 决策人

## 输出格式
只输出严格的 JSON（不要 Markdown，不要解释），格式：
{{
  "child_name": "",
  "grade": "",
  "owned_products": "",
  ...（其余同格式，共 13 个 key）
}}
"""


# ── 5.1 查询完整画像 ──

async def fetch_profile(corpid: str, external_userid: str) -> dict[str, Any] | None:
    """调用 Shirley MCP get_user_profile (5.1)。返回完整画像或 None。"""
    if not corpid or not external_userid:
        return None
    return await client.safe_call_tool("get_user_profile", {
        "corpid": corpid,
        "externalUserid": external_userid,
    })


# ── 5.2 分页查聊天记录 ──

async def query_chat_history(
    corpid: str,
    external_userid: str,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any] | None:
    """调用 Shirley MCP query_user_profile_chat_history (5.2)。

    返回 dict 含 messages/total/offset/limit/hasMore，或 None。
    limit 范围 1~500，默认 100。
    """
    if not corpid or not external_userid:
        return None
    limit = max(1, min(500, int(limit)))
    return await client.safe_call_tool("query_user_profile_chat_history", {
        "corpid": corpid,
        "externalUserid": external_userid,
        "limit": limit,
        "offset": int(offset),
    })


# ── 5.3 保存画像分析 ──

async def save_analysis(
    corpid: str,
    external_userid: str,
    attributes: Any,
    source: str = SHIRLEY_SOURCE,
    idempotency_key: str | None = None,
    intent_level: str | None = None,
    throw_on_error: bool = False,
) -> dict[str, Any] | None:
    """调用 Shirley MCP save_user_profile_analysis (5.3)。

    attributes 支持两种输入（自动检测）:
      A) 扁平 dict {"grade": "三年级", "pain_points": "...", ...} — 旧版 runtime 用
      B) attributes 列表 [{"key","values","summary","confidence","evidence"}, ...] — 新格式

    intent_level 单独传（13 项里的最后一项），如果 attributes 是 dict 且包含
    intent_level 也能自动识别。

    throw_on_error=True 时用 client.call_tool 抛 ShirleyMCPToolError；
    默认 False 用 safe_call_tool 吞掉错误返回 None（向后兼容）。
    """
    if not corpid or not external_userid or not attributes:
        return None

    # ── 自动检测输入格式 ──
    if isinstance(attributes, dict):
        flat = attributes
        # 从 dict 里单独拿 intent_level，避免重复
        if intent_level is None and "intent_level" in flat:
            intent_level = flat.pop("intent_level") or None
        attrs = merge_into_attributes(build_empty_attributes(), flat, intent_level)
    elif isinstance(attributes, list):
        # runtime 旧路径直接传 list — 再走一遍字段级截断（双保险）
        attrs = [_truncate_attr(a) if isinstance(a, dict) else a for a in attributes]
    else:
        return None

    if idempotency_key is None:
        idempotency_key = f"profile-{external_userid}-{int(time.time())}"

    # ── 双保险: 无论哪条路径进来, 发 Shirley 前再过一遍截断 ──
    attrs = [_truncate_attr(a) if isinstance(a, dict) else a for a in attrs]

    payload = {
        "corpid": corpid,
        "externalUserid": external_userid,
        "source": source,
        "idempotencyKey": idempotency_key,
        "attributes": attrs,
    }

    if throw_on_error:
        return await client.call_tool("save_user_profile_analysis", payload)
    return await client.safe_call_tool("save_user_profile_analysis", payload)


def build_empty_attributes(intent_level_summary: str = "") -> list[dict[str, Any]]:
    """构造一个全空的 13 项 attributes 列表（未提取到时用）。"""
    attrs: list[dict[str, Any]] = []
    for key in ALL_PROFILE_KEYS:
        attr: dict[str, Any] = {
            "key": key,
            "values": [],
            "summary": "未提取到",
            "confidence": 0,
            "evidence": [],
        }
        if key == "intent_level" and intent_level_summary:
            attr["summary"] = intent_level_summary
        attrs.append(attr)
    return attrs


def merge_into_attributes(
    base_attrs: list[dict[str, Any]],
    extracted: dict[str, str],
    intent_level: str | None = None,
) -> list[dict[str, Any]]:
    """把 LLM 抽取结果 + intent_level 合并进空模板。

    对每个 key 的 values 和 summary 做字段级截断（按 _ATTR_LENGTH_LIMITS）。
    """
    result: list[dict[str, Any]] = []
    intent_summary = intent_level or ""
    for attr in base_attrs:
        key = attr["key"]
        extracted_val = extracted.get(key, "")
        if extracted_val:
            raw_values = [extracted_val] if isinstance(extracted_val, str) else list(extracted_val)
            raw_summary = extracted_val if isinstance(extracted_val, str) else ", ".join(extracted_val)
            attr = {
                **attr,
                "values": raw_values,
                "summary": raw_summary,
                "confidence": 0.7,
            }
        elif key == "intent_level" and intent_level:
            attr = {
                **attr,
                "values": [intent_level],
                "summary": intent_summary or f"intent={intent_level}",
                "confidence": 0.6,
            }
        result.append(_truncate_attr(attr))
    return result


# ── 关键词命中 + LLM 抽取 ──

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


async def analyze_from_dialogue(
    history: list[dict[str, Any]],
    user_text: str,
    prev_ai_analysis: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    """用 LLM 从对话历史中抽取 12 项画像字段（不含 intent_level）。

    返回 dict[key → value_string] 或 None。
    """
    try:
        from deeptutor.services.llm import get_llm_client
        client_obj = get_llm_client()
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
        reply = await client_obj.complete(prompt)
        text = (reply or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
    except Exception as e:
        logger.warning("analyze_from_dialogue: failed to parse LLM reply: %s", e)
        return None

    result: dict[str, str] = {}
    for key in list(_PROFILE_KEYWORDS.keys()):
        v = data.get(key, "")
        if isinstance(v, str):
            v = v.strip()
        if v:
            result[key] = v

    # 和 prev_ai_analysis 做 diff：新值覆盖旧值
    if prev_ai_analysis:
        for k, v in prev_ai_analysis.items():
            if k not in result and v:
                result[k] = str(v)

    return result or None


async def analyze_and_save(
    corpid: str,
    external_userid: str,
    history: list[dict[str, Any]],
    user_text: str,
    prev_ai_analysis: dict[str, Any] | None = None,
    intent_level: str | None = None,
) -> dict[str, Any] | None:
    """一键: LLM 抽字段 → 合并 intent_level → 调 5.3 保存。

    intent_level 通常来自 sales tagger 的判定结果（high/medium/low）。

    Shirley schema 兼容 (网关灰度中，13/14 项不稳定):
      - 先发 14 项 (child_name + 13)
      - 若被拒自动降级发 13 项 (去掉 child_name)
      - 双向重试直到成功；最坏情况返回 None（画像非关键路径）
    """
    from deeptutor.services.shirley.client import ShirleyMCPToolError

    extracted = await analyze_from_dialogue(history, user_text, prev_ai_analysis)
    base = build_empty_attributes()
    attrs_14 = merge_into_attributes(base, extracted or {}, intent_level)
    attrs_13 = [a for a in attrs_14 if a.get("key") != "child_name"]

    # 候选顺序: 先 14 再 13 —— 理论上 14 是最新 schema
    candidates = [
        ("14 attrs (with child_name)", attrs_14),
        ("13 attrs (legacy)", attrs_13),
    ]

    last_err: ShirleyMCPToolError | None = None
    for label, candidate in candidates:
        try:
            result = await save_analysis(
                corpid, external_userid, candidate, throw_on_error=True,
            )
            if last_err is not None:
                logger.warning(
                    "Shirley 5.3 schema resolved after fallback: used %s (tried %d candidates)",
                    label, candidates.index((label, candidate)) + 1,
                )
            return result
        except ShirleyMCPToolError as e:
            last_err = e
            logger.info("Shirley 5.3 attempt [%s] failed: %s", label, str(e)[:80])

    # 画像写回属非关键路径: 重试全失败也只记日志跳过, 不影响 reply_lisa_message
    logger.warning(
        "Shirley 5.3 SKIPPED after retries (tried 14 attrs + 13 attrs, gateway schema unstable): %s",
        last_err,
    )
    return None


def _error_hints_13(err_msg: str) -> bool:
    """判断 Shirley 返回的 error 是否在提示 schema 数量不匹配 (要 13 项不是 14 项)。"""
    return any(k in err_msg for k in ("13项", "必须提交13", "13 项"))


# ── 画像摘要（注入 system prompt） ──

def summarize_profile(profile: dict[str, Any] | None) -> str:
    """把完整 profile 压缩成 system prompt 能装下的摘要。"""
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
            phone_val = basic.get("phone") or basic.get("mobile")
            if phone_val:
                line += f" | 手机: {phone_val}"
            parts.append(line)
        region_bits: list[str] = []
        for key in ("region", "grade"):
            if basic.get(key):
                region_bits.append(str(basic[key]))
        # Shirley 有 province + city 分开，拼一下
        if basic.get("province") and basic.get("city"):
            region_bits.insert(0, f"{basic['province']}{basic['city']}")
        if region_bits:
            parts.append(f"- 地域年级: {' | '.join(region_bits)}")
        if courses:
            def _course_name(c: Any) -> str:
                if isinstance(c, dict):
                    for k in ("name", "sourceThirdName", "targetSourceThirdName"):
                        v = c.get(k)
                        if isinstance(v, str) and v:
                            return v
                return str(c)
            names = [_course_name(c) for c in courses[:5]]
            parts.append(f"- 已购课程: {', '.join(names)}")
        if tags:
            def _tag_name(t: Any) -> str:
                if isinstance(t, dict):
                    for k in ("tagName", "name"):
                        v = t.get(k)
                        if isinstance(v, str) and v:
                            return v
                return str(t)
            tag_names = [_tag_name(t) for t in tags[:8]]
            parts.append(f"- 企微标签: {', '.join(tag_names)}")
        if ai and isinstance(ai, dict):
            attrs_list = ai.get("attributes") or []
            # attributes 是 [{"key","values","summary","confidence","evidence"}, ...]
            attr_by_key: dict[str, dict[str, Any]] = {}
            if isinstance(attrs_list, list):
                for a in attrs_list:
                    if isinstance(a, dict) and a.get("key"):
                        attr_by_key[a["key"]] = a
            elif isinstance(attrs_list, dict):
                attr_by_key = attrs_list

            # 核心 4 项单独展示
            _core_keys = ("child_name", "grade", "intent_level", "pain_points")
            core_bits: list[str] = []
            for ck in _core_keys:
                a = attr_by_key.get(ck)
                if not a:
                    continue
                vals = a.get("values") or []
                if vals:
                    joined = "/".join(str(v) for v in vals if v)
                    core_bits.append(f"{ck}={joined}")
                elif a.get("summary") and str(a.get("summary")) != "未提取到":
                    core_bits.append(f"{ck}={a['summary']}")
            if core_bits:
                parts.append(f"- 画像核心: {'; '.join(core_bits)}")

            # 剩余 attr 的 summary 凑一行
            rest_brief: list[str] = []
            for k, v in attr_by_key.items():
                if k in _core_keys:
                    continue
                s = str(v.get("summary") or "").strip() if isinstance(v, dict) else ""
                if s and s != "未提取到":
                    rest_brief.append(f"{k}={s}")
            if rest_brief:
                parts.append(f"- 画像补充: {'; '.join(rest_brief[:5])}")
        if not parts:
            return ""
        return "【用户画像】\n" + "\n".join(parts)
    except Exception:
        return ""

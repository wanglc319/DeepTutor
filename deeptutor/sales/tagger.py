"""
意向度打标签器
===========

两层判定（LLM 优先 + 正则兜底）:
  1. LLM 语义判定 —— 先调用 LLM 做语义级别的意图打分
  2. 正则兜底      —— LLM 调用失败或返回全空时，退回正则关键词匹配

策略: 每轮客户消息到达后增量判定，只对新增消息打标签，合并进已有 signals。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .schemas import A_GROUP_SIGNALS, A_GROUP_LABEL_CN, B_GROUP_PROFILE, CustomerProfile

logger = logging.getLogger(__name__)


# ──────────────────── 正则模板 ────────────────────────────────────────────
# 每个标签的 regex hint：匹配就认为该信号为 True
# 部分标签有多组正则（| 分隔），全部用 re.IGNORECASE
_REGEX_HINTS: dict[str, str] = {
    # ── A 组 ──
    "asked_material_fee": r"教材|材料费|课本费|书本费|教材费|material",
    "asked_start_date":   r"开课|什么时候.*(开始|开|启动)|哪天.*开课|这期|这一期|开班|啥时候.*(开始|开课)|start.*(date|time)",
    "asked_placement":     r"测评|分班|摸底|水平测试|测一下|placement",
    "asked_schedule":     r"(每天|一周|每周|星期|几节课|多少节课|时间安排|课程安排|上课时间|课表|怎么安排|schedule|frequency|how many.*class)",
    "asked_refund":       r"退款|退费|不想学.*(退|换|放弃)|不合适.*退|全额.*退|7天|七天.*退|refund",
    "asked_discount":     r"优惠|便宜|打折|折扣|能不能.*(便宜|优惠|打)|再.*(便宜|优惠)|一起报.*优惠|discount|promotion",
    "asked_payment":      r"(怎么|如何|用什么|能.*方式).*(付款|支付)|微信.*(付|支)|支付宝|分期|payment",
    "asked_teacher":      r"(老师|讲师|授课|任教).*(谁|什么|资质|学历|水平|背景)|teacher|instructor",
    "asked_trial":        r"试课|试听|体验|demo|trial|free.*lesson",
    "asked_price":        r"(多少钱|价格|费用|报价|价位|预算|price|cost|fee)",   # 弱信号，低权重
    "asked_level":        r"(什么级别|程度|水平|难易|难度|入门|进阶|高级|beginner|intermediate|advanced|level)",

    # 明确拒绝（唯一负面标签）
    "__refusal__": r"(不用了|算了|再想想|不急|暂时.*(不|没)|以后|先.*(不|没)|不需要|不考虑|不感兴趣|不用谢谢|拒绝|我不需要)",

    # ── B 组（画像） ──
    "grade":              r"(几年级|年级|year\s*\d|primary|junior|senior|grade\s*\d)",
    "pain_points":        r"(不爱学|没兴趣|抵触|拖拉|走神|忘词|不会说|听力差|不敢开口|讨厌)",
    "price_sensitivity":  r"(太贵|预算有限|性价比|划算|值不值|能不能.*便宜|有点贵)",
    "available_time":     r"(每天.*时间|每天.*(几点|什么时候)|(周末|平时|晚上|早上).*(有空|时间)|available.*time)",
    "coaching_ability":   r"(我能.*(教|辅导)|我自己.*(教|辅导)|我不会英语|我英语不好|我没时间|我不会教)",
    "multi_child":        r"(还有.*(弟弟|妹妹|哥哥|姐姐)|两个孩子|二胎|三胎|multi.*child)",
    "external_classes":  r"(线下班|一对一|外教|网课|直播课|录播课|机构|辅导班|补习班|school.*class|external)",
}


# ──────────────────── 编译 ──────────────────────────────────────────────
_COMPILED: dict[str, re.Pattern] = {k: re.compile(v, re.IGNORECASE) for k, v in _REGEX_HINTS.items()}


# ──────────────────── 核心 API ──────────────────────────────────────────

def regex_tag(text: str) -> tuple[dict[str, bool], dict[str, str]]:
    """仅用正则判定单条消息的标签。返回 (signals_dict, source_dict)."""
    signals: dict[str, bool] = {}
    source: dict[str, str] = {}
    if not text:
        return signals, source

    for label in A_GROUP_SIGNALS:
        rx = _COMPILED.get(label)
        if rx and rx.search(text):
            signals[label] = True
            source[label] = "regex"

    # 特殊：明确拒绝
    ref_rx = _COMPILED.get("__refusal__")
    if ref_rx and ref_rx.search(text):
        signals["explicit_refusal"] = True
        source["explicit_refusal"] = "regex"

    return signals, source


def regex_profile_extract(text: str) -> dict[str, Any]:
    """正则从单条消息里抽 B 组画像字段（粗粒度，能抽几个算几个）."""
    out: dict[str, Any] = {}
    if not text:
        return out

    # grade: 匹配 "X年级" / "X岁" / 英文 grade X
    m = re.search(r"([一二三四五六七八九十]+|[1-9])\s*(年级|岁)", text)
    if m:
        out["grade"] = f"{m.group(1)}{m.group(2)}"
    else:
        m2 = re.search(r"grade\s*(\d)", text, re.IGNORECASE)
        if m2:
            out["grade"] = f"grade_{m2.group(1)}"

    # price_sensitivity
    ps_rx = _COMPILED.get("price_sensitivity")
    if ps_rx and ps_rx.search(text):
        out["price_sensitivity"] = "price_sensitive"

    # coaching_ability
    ca_rx = _COMPILED.get("coaching_ability")
    if ca_rx and ca_rx.search(text):
        out["coaching_ability"] = "low" if ("不会" in text or "没时间" in text) else "high"

    # available_time
    at_rx = _COMPILED.get("available_time")
    if at_rx and at_rx.search(text):
        out["available_time"] = "mentioned"

    # external_classes
    ec_rx = _COMPILED.get("external_classes")
    if ec_rx and ec_rx.search(text):
        out["external_classes"] = ["mentioned"]

    # pain_points
    pp_rx = _COMPILED.get("pain_points")
    if pp_rx and pp_rx.search(text):
        out["pain_points"] = ["mentioned"]

    # multi_child
    mc_rx = _COMPILED.get("multi_child")
    if mc_rx and mc_rx.search(text):
        out["multi_child"] = True

    return out


# ──────────────────── LLM 兜底 ──────────────────────────────────────────

LLM_TAG_SYSTEM_PROMPT = f"""你是一个专业的销售意向分析助手。根据客户发来的消息，判断客户在哪些交付维度上表现出购买意向。

## 输出要求
严格输出一个 JSON 对象（不要加解释、不要加 markdown 代码块），包含以下字段：

```json
{{
  "signals": {{
    {json.dumps({k: False for k in A_GROUP_SIGNALS}, ensure_ascii=False, indent=4)[2:-2]}
  }},
  "explicit_refusal": false,
  "profile": {{
    {json.dumps({k: None for k in B_GROUP_PROFILE}, ensure_ascii=False, indent=4)[2:-2]}
  }}
}}
```

## 判定规则
- **signals**: 对每个交付维度，如果客户消息里表现出追问/关注（哪怕很委婉），置为 true，否则 false
  - 标准：客户主动问了「XX 是多少/怎么样/怎么安排」就触发
  - 不要因为 AI 提到某个维度就算作客户问了
- **explicit_refusal**: 客户明确说"不用/算了/不考虑"等就置 true，保留性表述（"我再想想""不急"）不算
- **profile**: 能从消息里推断的画像字段填值，不能推断的保留 null

## 注意
- 只看客户消息，不要脑补
- 客户说"多少钱"→ 同时触发 asked_price 和可能的 asked_discount（取决于上下文）
- 客户说"什么时候开课 + 教材费多少"→ asked_start_date + asked_material_fee 都为 true
- 客户只说"你好"→ 全部 false
"""


async def llm_tag(text: str, llm_client: Any) -> tuple[dict[str, bool], dict[str, str], dict[str, Any]]:
    """调用 LLM 做语义打标签。返回 (signals, source, profile)."""
    import asyncio

    payload = {
        "model": getattr(llm_client, "model", getattr(llm_client, "MODEL", "qwen3.7-flash")),
        "messages": [
            {"role": "system", "content": LLM_TAG_SYSTEM_PROMPT},
            {"role": "user",   "content": f"客户消息：{text}"},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    # 兼容 DeepTutor 的 llm_client（可能有 .chat.completions.create，也可能是 aiohttp session）
    try:
        resp = await llm_client.chat.completions.create(**payload)
        content = resp.choices[0].message.content
    except Exception:
        # 兜底：简单同步（如果在非 async 上下文）
        try:
            resp = llm_client.chat.completions.create(**payload)
            content = resp.choices[0].message.content
        except Exception as exc:
            logger.warning("llm_tag fallback failed: %s", exc)
            return {}, {}, {}

    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("llm_tag returned non-json: %s", content[:200])
        return {}, {}, {}

    signals: dict[str, bool] = {}
    source: dict[str, str] = {}
    raw_sig = obj.get("signals", {}) or {}
    for label in A_GROUP_SIGNALS:
        if bool(raw_sig.get(label)):
            signals[label] = True
            source[label] = "llm"

    if bool(obj.get("explicit_refusal")):
        signals["explicit_refusal"] = True
        source["explicit_refusal"] = "llm"

    raw_prof = obj.get("profile", {}) or {}
    profile_out: dict[str, Any] = {}
    for k in B_GROUP_PROFILE:
        v = raw_prof.get(k)
        if v is not None and v != "":
            profile_out[k] = v

    return signals, source, profile_out


# ──────────────────── 合并入口 ──────────────────────────────────────────

def merge_signals(prev: CustomerProfile, new_signals: dict[str, bool], new_source: dict[str, str]) -> None:
    """增量合并：已有 True 不被覆盖为 False，新增 True 按来源记录。"""
    for label, val in new_signals.items():
        if val and not prev.intent_signals.get(label, False):
            prev.intent_signals[label] = True
            prev.tag_source[label] = new_source.get(label, "unknown")
        # 已有 True 保持 True（标签一旦打上去就不撤回）

    # explicit_refusal 特殊处理：refusal > 已有拒绝不丢，但极热档会覆盖它的效果
    if new_signals.get("explicit_refusal"):
        prev.explicit_refusal = True
        prev.tag_source["explicit_refusal"] = new_source.get("explicit_refusal", "unknown")


def merge_profile(prev: CustomerProfile, new_profile: dict[str, Any]) -> None:
    """画像字段合并：有就填，没有就保留旧值（旧值比新消息更全面）."""
    for k, v in new_profile.items():
        if v is not None and v != "" and v != []:
            prev.profile[k] = v


def compute_delivery_count(profile: CustomerProfile) -> int:
    """统计 11 个 A 组标签里 True 的数量 → delivery_q_count."""
    return sum(1 for label in A_GROUP_SIGNALS if profile.intent_signals.get(label))

"""意向度打分相关的数据结构定义."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


# ── A 组：11 个交付维度意向标签（唯一进 delivery_q_count） ──
A_GROUP_SIGNALS: tuple[str, ...] = (
    "asked_material_fee",   # 教材费
    "asked_start_date",     # 开课时间
    "asked_placement",      # 测评分班
    "asked_schedule",       # 课程安排
    "asked_refund",         # 退款政策
    "asked_discount",       # 优惠
    "asked_payment",        # 付款方式
    "asked_teacher",        # 老师资质
    "asked_trial",          # 试课
    "asked_price",          # 价格（弱信号，lift 1.61）
    "asked_level",          # 分级程度
)
A_GROUP_LABEL_CN: dict[str, str] = {
    "asked_material_fee": "问教材费",
    "asked_start_date":   "问开课时间",
    "asked_placement":     "问测评分班",
    "asked_schedule":     "问课程安排",
    "asked_refund":       "问退款政策",
    "asked_discount":     "问优惠",
    "asked_payment":      "问付款方式",
    "asked_teacher":      "问老师资质",
    "asked_trial":        "问试课",
    "asked_price":        "问价格",
    "asked_level":        "问分级程度",
}


# ── B 组：画像字段（仅话术个性化，不进打分） ──
B_GROUP_PROFILE: tuple[str, ...] = (
    "grade",                 # 年级
    "owned_products",        # 已购产品
    "pain_points",           # 痛点
    "level_self_report",     # 自评水平
    "school_english_start",  # 学校英语起始
    "available_time",        # 可学习时间
    "external_classes",     # 外部课程
    "price_sensitivity",    # 价格敏感度
    "coaching_ability",      # 陪学能力
    "multi_child",           # 多子女
    "region_textbook",       # 地区教材
    "decision_makers",       # 决策人
)


# ── 温度分档 ──
TEMP_BLAZING = "blazing"   # 极热: delivery_q_count >= 4
TEMP_HOT     = "hot"       # 热:   2-3
TEMP_WARM    = "warm"      # 温:   1
TEMP_COOL    = "cool"      # 凉:   0 + 无拒绝 → 标准培育（直播推送）
TEMP_COLD    = "cold"      # 冷:   0 + 有拒绝 → 低频维护
TEMP_UNKNOWN = "unknown"

TEMP_TO_DB_ENUM = {
    TEMP_BLAZING: "blazing",
    TEMP_HOT:     "hot",
    TEMP_WARM:    "warm",
    TEMP_COOL:    "cool",
    TEMP_COLD:    "cold",
    TEMP_UNKNOWN: "unknown",
}


# ── 时间窗关键天数 ──
DAY_GATE_3  = 3    # 正常跟进
DAY_GATE_5  = 5    # 加大推进
DAY_GATE_7  = 7    # 断崖，降级
DAY_GATE_10 = 10   # 转低频
DAY_GATE_14 = 14   # 止损
DAY_GATE_28 = 28   # 停止主动


# ── 封装 ──
@dataclass
class CustomerProfile:
    """完整的客户意向档案（文档 5.4 节 schema 的 Python 表示）."""

    # A 组
    intent_signals: dict[str, bool] = field(default_factory=lambda: {k: False for k in A_GROUP_SIGNALS})
    delivery_q_count: int = 0
    explicit_refusal: bool = False

    # 时间轴（由外部注入，这里只存计算结果）
    days_since_add: int = 0
    customer_msg_count: int = 0      # 客户累计发消息条数（对话轮次）
    first_delivery_q_day: int | None = None
    days_since_closing: int | None = None
    silent_days: int = 0          # 读时算，这里缓存

    # B 组
    profile: dict[str, Any] = field(default_factory=lambda: {k: None for k in B_GROUP_PROFILE})

    # 输出
    intent_temperature: str = TEMP_UNKNOWN
    next_action: str = "none"
    live_pushed: bool = False  # 直播链接是否已推送过（只发一次）

    # 打标签来源
    tag_source: dict[str, str] = field(default_factory=dict)  # label -> "regex" | "llm" | "inherited"

    # ── dict / JSON 互转 ──
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "CustomerProfile":
        if not d:
            return cls()
        # 老 profile 可能缺字段，补齐默认值
        sig = {k: bool((d.get("intent_signals") or {}).get(k, False)) for k in A_GROUP_SIGNALS}
        prof = dict((d.get("profile") or {}))
        for k in B_GROUP_PROFILE:
            prof.setdefault(k, None)
        return cls(
            intent_signals=sig,
            delivery_q_count=int(d.get("delivery_q_count", 0)),
            explicit_refusal=bool(d.get("explicit_refusal", False)),
            days_since_add=int(d.get("days_since_add", 0)),
            customer_msg_count=int(d.get("customer_msg_count", 0)),
            first_delivery_q_day=d.get("first_delivery_q_day"),
            days_since_closing=d.get("days_since_closing"),
            silent_days=int(d.get("silent_days", 0)),
            profile=prof,
            intent_temperature=d.get("intent_temperature", TEMP_UNKNOWN),
            next_action=d.get("next_action", "none"),
            live_pushed=bool(d.get("live_pushed", False)),
            tag_source=dict(d.get("tag_source") or {}),
        )

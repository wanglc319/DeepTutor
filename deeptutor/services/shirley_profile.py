"""
兼容层 —— 重导出 deeptutor.services.shirley 的公共 API.

保留旧 import 路径 ``from deeptutor.services import shirley_profile`` 不变，
内部实现已迁移到 ``deeptutor.services.shirley`` 包。
"""

from deeptutor.services.shirley import (
    fetch_profile,
    save_analysis,
    summarize_profile,
    should_trigger_analysis,
    analyze_from_dialogue,
    ALL_PROFILE_KEYS,
    ALL_PROFILE_KEYS_NO_INTENT,
    THIRTEEN_ATTRIBUTE_KEYS,
    build_attributes_payload,
    query_profile_chat_history,
    query_sales_chat_history,
    mark_qywx_tags,
    get_qywx_detail,
    get_mantis_live_link,
    notify_lisa_reply,
)

__all__ = [
    "fetch_profile",
    "save_analysis",
    "summarize_profile",
    "should_trigger_analysis",
    "analyze_from_dialogue",
    "ALL_PROFILE_KEYS",
    "ALL_PROFILE_KEYS_NO_INTENT",
    "THIRTEEN_ATTRIBUTE_KEYS",
    "build_attributes_payload",
    "query_profile_chat_history",
    "query_sales_chat_history",
    "mark_qywx_tags",
    "get_qywx_detail",
    "get_mantis_live_link",
    "notify_lisa_reply",
]

"""
Shirley AI MCP Skill 包
========================

封装 Shirley AI MCP 网关的全部 Tool，按业务域分 6 个子模块：

  - profile   用户画像（get_user_profile / save_user_profile_analysis / query_user_profile_chat_history）
  - chat      聊天历史（query_user_sales_chat_history）
  - customer  客户标签（mark_qywx_customer_tags）
  - detail    客户详情（get_qywx_external_detail_v2）
  - live      直播链接（get_mantis_live_link）
  - reply     Lisa 回复通知（reply_lisa_message）

底层通过 _client.ShirleyClient 统一走 Streamable HTTP 协议。

参考: https://jcnmgzcga30e.feishu.cn/wiki/PHFSwSwK3iwKs3koRtMcxHp1nId
"""

from ._client import ShirleyClient, get_client
from .profile import (
    fetch_profile,
    save_analysis,
    query_profile_chat_history,
    summarize_profile,
    should_trigger_analysis,
    analyze_from_dialogue,
    ALL_PROFILE_KEYS,
    ALL_PROFILE_KEYS_NO_INTENT,
    THIRTEEN_ATTRIBUTE_KEYS,
    build_attributes_payload,
)
from .chat import query_sales_chat_history
from .customer import mark_qywx_tags
from .detail import get_qywx_detail
from .live import get_mantis_live_link
from .reply import notify_lisa_reply

__all__ = [
    "ShirleyClient",
    "get_client",
    "fetch_profile",
    "save_analysis",
    "query_profile_chat_history",
    "summarize_profile",
    "should_trigger_analysis",
    "analyze_from_dialogue",
    "ALL_PROFILE_KEYS",
    "THIRTEEN_ATTRIBUTE_KEYS",
    "build_attributes_payload",
    "query_sales_chat_history",
    "mark_qywx_tags",
    "get_qywx_detail",
    "get_mantis_live_link",
    "notify_lisa_reply",
]

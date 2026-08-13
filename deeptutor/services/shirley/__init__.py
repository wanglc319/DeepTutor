"""
Shirley AI MCP Skill 包 —— 按业务域分为 3 个 Skill
==================================================

Skill 1: 用户画像 (profile)  — Shirley MCP 5.x
Skill 2: 直播 (live)         — Shirley MCP 4
Skill 3: 企微调用 (wecom)    — Shirley MCP 2 / 3 / 6 / 7

底层 _client.py 统一走 Streamable HTTP 协议。
"""

from ._client import ShirleyClient, get_client

# ──────────────────────────────────────────────────────
# Skill 1: 用户画像 —— Shirley MCP 5.x
#   5.1 get_user_profile
#   5.2 query_user_profile_chat_history
#   5.3 save_user_profile_analysis
# ──────────────────────────────────────────────────────
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

# ──────────────────────────────────────────────────────
# Skill 2: 直播 —— Shirley MCP 4
#   4. get_mantis_live_link
# ──────────────────────────────────────────────────────
from .live import get_mantis_live_link, get_live_url_from_env

# ──────────────────────────────────────────────────────
# Skill 3: 企微调用 —— Shirley MCP 2 / 3 / 6 / 7
#   2. query_user_sales_chat_history  (chat.py)
#   3. mark_qywx_customer_tags        (customer.py)
#   6. reply_lisa_message             (reply.py)
#   7. get_qywx_external_detail_v2    (detail.py)
# ──────────────────────────────────────────────────────
from .chat import query_sales_chat_history
from .customer import mark_qywx_tags
from .reply import notify_lisa_reply
from .detail import get_qywx_detail

__all__ = [
    # 底层
    "ShirleyClient",
    "get_client",
    # Skill 1: 用户画像
    "fetch_profile",
    "save_analysis",
    "query_profile_chat_history",
    "summarize_profile",
    "should_trigger_analysis",
    "analyze_from_dialogue",
    "ALL_PROFILE_KEYS",
    "ALL_PROFILE_KEYS_NO_INTENT",
    "THIRTEEN_ATTRIBUTE_KEYS",
    "build_attributes_payload",
    # Skill 2: 直播
    "get_mantis_live_link",
    "get_live_url_from_env",
    # Skill 3: 企微调用
    "query_sales_chat_history",
    "mark_qywx_tags",
    "get_qywx_detail",
    "notify_lisa_reply",
]

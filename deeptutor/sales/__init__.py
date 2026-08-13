"""deeptutor.sales — AI 销售意向度打分体系.

快速上手:

```python
# 纯内存跑一遍（不碰 DB，方便测试）
from deeptutor.sales.service import run_without_db
profile, action = run_without_db(
    customer_msg="教材费多少？什么时候开课？",
)
print(profile.intent_temperature)   # 'hot'
print(action)                       # None (热档不推直播)

# 接 DB + WS 消息流集成
from deeptutor.sales.service import process_customer_message
profile, action_text = await process_customer_message(
    customer_msg, customer_external_id="wx_001",
)
# action_text 可能是直播链接追加文本
```
"""
from .schemas import (
    CustomerProfile,
    A_GROUP_SIGNALS, A_GROUP_LABEL_CN, B_GROUP_PROFILE,
    TEMP_BLAZING, TEMP_HOT, TEMP_WARM, TEMP_COOL, TEMP_COLD, TEMP_UNKNOWN,
)
from .tagger import regex_tag, regex_profile_extract, merge_signals, merge_profile
from .temperature import (
    compute_temperature, compute_days_since_add, compute_silent_days,
    compute_time_factor, compute_stop_loss, apply_temperature_to_profile,
)
from .actions import decide_next_action, build_action_builder
from .service import process_customer_message, run_without_db
from . import db as sales_db

__all__ = [
    # schemas
    "CustomerProfile",
    "A_GROUP_SIGNALS", "A_GROUP_LABEL_CN", "B_GROUP_PROFILE",
    "TEMP_BLAZING", "TEMP_HOT", "TEMP_WARM", "TEMP_COOL", "TEMP_COLD", "TEMP_UNKNOWN",
    # tagger
    "regex_tag", "regex_profile_extract", "merge_signals", "merge_profile",
    # temperature
    "compute_temperature", "compute_days_since_add", "compute_silent_days",
    "compute_time_factor", "compute_stop_loss", "apply_temperature_to_profile",
    # actions
    "decide_next_action", "build_action_builder",
    # service
    "process_customer_message", "run_without_db",
    # db
    "sales_db",
]

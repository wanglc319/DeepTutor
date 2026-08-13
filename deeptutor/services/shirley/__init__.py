"""
deeptutor.services.shirley
==========================

Shirley MCP 统一服务层。内部聚合 client / resolver / profile / live / qywx，
对外暴露 Skill 入口函数和 DeepTutor Tool 类。

架构:
  client    底层 Streamable HTTP，所有 MCP 调用走它
  resolver  ID 映射层: corpid+externalUserid → 完整参数集
  profile   画像 Skill (5.1/5.2/5.3) — 内部函数，不注册 Tool
  live      直播 Skill (4/4.1)     — 注册 Tool: shirley_get_live_schedule
  qywx      企微 Skill (2/3/6/7)    — 注册 Tool: shirley_qywx_* (4 个)

上层调用示例:
  # 拉画像
  profile = await shirley.profile.fetch_profile(corpid, external_userid)
  summary = shirley.profile.summarize_profile(profile)

  # 查直播（runtime 内部调用，不走 Tool）
  sessions = await shirley.live.list_weekly_lives(corpid, external_userid)

  # 企微写操作（同样有内部函数）
  await shirley.qywx.mark_tags(corpid, external_userid, tag_ids=["t1","t2"])

  # 或者让 LLM 通过 Tool 调（Tool 类见 live.ShirleyGetLiveScheduleTool 等）
"""
from . import client, live, profile, qywx, resolver

__all__ = [
    "client",
    "live",
    "profile",
    "qywx",
    "resolver",
]

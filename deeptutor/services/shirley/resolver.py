"""
IDResolver
==========

把业务语义参数 (corpid, external_userid) 解出 Shirley MCP 各工具
所需的完整参数集合。

策略:
  - 能从 5.1 get_user_profile 解的 → 自动解 (addedAt, followUserid, tagIds[])
  - 能从 7 get_qywx_external_detail_v2 解的 → 自动解 (thirdUserId, userId)
  - 全局/静态配置 → 环境变量或 partner config 读取 (campId, qywxUserid)
  - 解不出来的 → 留空，上层可以显式传入覆盖
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from . import client

logger = logging.getLogger(__name__)


@dataclass
class ResolvedIDs:
    """一次 resolve 调用能解出的全部 ID / 参数。

    所有字段都可能是 None —— 上层要做空值检查。
    """

    corpid: str = ""
    external_userid: str = ""

    # ── 5.1 能解出的 ──
    added_at: str | None = None  # 客户加微时间 yyyy-MM-dd HH:mm:ss
    follow_userids: list[str] = field(default_factory=list)  # 所有跟进成员 userid
    primary_follow_userid: str | None = None  # 主推成员
    tag_ids: list[str] = field(default_factory=list)  # 客户现有企微标签 ID

    # ── 7 能解出的 ──
    third_user_id: int | None = None  # Sales 系统客户 ID
    third_sale_uuid: str | None = None  # Sales 账号 uuid (可能需要 followUser → uuid 映射)
    user_id: int | None = None  # 另一个 userId 体系 (3 号工具要)

    # ── 全局配置 ──
    camp_id: int | None = None  # 螳螂训练营 ID
    qywx_userid: str | None = None  # 默认销售企微 userid
    domain: str | None = None  # 直播域名

    # ── 原始响应 (供上层深挖) ──
    raw_profile: dict[str, Any] | None = None
    raw_detail: dict[str, Any] | None = None


async def resolve_all(
    corpid: str,
    external_userid: str,
    *,
    camp_id: int | None = None,
    qywx_userid: str | None = None,
    domain: str | None = None,
) -> ResolvedIDs:
    """一站式 resolve。并行调 5.1 和 7，拼出完整映射。"""
    resolved = ResolvedIDs(corpid=corpid, external_userid=external_userid)

    if camp_id is None:
        try:
            camp_id = int(os.getenv("SHIRLEY_CAMP_ID", "19913")) or 19913
        except (ValueError, TypeError):
            camp_id = 19913
    resolved.camp_id = camp_id

    if qywx_userid is None:
        qywx_userid = os.getenv("SHIRLEY_QYWX_USERID") or None
    resolved.qywx_userid = qywx_userid

    if domain is None:
        domain = os.getenv("SHIRLEY_LIVE_DOMAIN") or "qn715.hvawb.citv.cn"
    resolved.domain = domain

    # ── 5.1 get_user_profile ──
    profile = await client.safe_call_tool("get_user_profile", {
        "corpid": corpid,
        "externalUserid": external_userid,
    })
    if isinstance(profile, dict):
        resolved.raw_profile = profile
        _extract_from_profile(resolved, profile)

    # ── 7 get_qywx_external_detail_v2 ──
    # 需要 thirdUserId，但可能从 profile 的 orders 或其他字段里能找到。
    # 如果找不到就跳过这一步，不阻塞其他 resolve。
    if resolved.third_user_id:
        detail = await client.safe_call_tool("get_qywx_external_detail_v2", {
            "thirdUserId": resolved.third_user_id,
        })
        if isinstance(detail, dict):
            resolved.raw_detail = detail
            _extract_from_detail(resolved, detail)

    return resolved


def _fmt_added(added: Any) -> str | None:
    """把加微时间统一成 4.1 要的 'yyyy-MM-dd HH:mm:ss'。"""
    if isinstance(added, str) and added.strip():
        return added.strip().replace("T", " ").split(".")[0].split("+")[0]
    if isinstance(added, (int, float)):
        # 可能是毫秒时间戳
        from datetime import datetime
        ts = int(added / 1000) if added > 1e12 else int(added)
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    return None


def _extract_from_profile(resolved: ResolvedIDs, profile: dict[str, Any]) -> None:
    """从 5.1 返回里抽字段填 ResolvedIDs。

    addedAt 取值优先级:
      1. serviceEmployees 里 followUserid == qywxUserid 那条的 addedAt（该销售的加微时间）
      2. basic.firstAddedAt（最早加微时间）兜底
    """
    # serviceEmployees → followUserid 列表 + 按销售匹配 addedAt
    service_emps = profile.get("serviceEmployees") or []
    ids: list[str] = []
    matched_added: str | None = None
    if isinstance(service_emps, list):
        for emp in service_emps:
            if not isinstance(emp, dict):
                continue
            emp_userid = ""
            for key in ("followUserid", "userid", "qywxUserid", "userId"):
                val = emp.get(key)
                if val and isinstance(val, str):
                    emp_userid = val
                    break
            if emp_userid:
                ids.append(emp_userid)
                # followUserid 与当前销售 qywxUserid 匹配 → 取这条的 addedAt
                if resolved.qywx_userid and emp_userid == resolved.qywx_userid and not matched_added:
                    matched_added = _fmt_added(emp.get("addedAt"))
        resolved.follow_userids = ids
        if ids and not resolved.primary_follow_userid:
            resolved.primary_follow_userid = ids[0]
            # 如果没全局 qywxUserid 配置，用第一个跟进成员
            if not resolved.qywx_userid:
                resolved.qywx_userid = ids[0]

    if matched_added:
        resolved.added_at = matched_added
        logger.info(
            "[resolver] addedAt matched by serviceEmployees | qywxUserid=%s | addedAt=%s",
            resolved.qywx_userid, matched_added,
        )
    else:
        # 兜底: basic.firstAddedAt
        basic = profile.get("basic") or {}
        if isinstance(basic, dict):
            added = (
                basic.get("firstAddedAt")
                or basic.get("addedAt")
                or basic.get("addTime")
                or basic.get("firstAddTime")
            )
            resolved.added_at = _fmt_added(added)

    # tags → tagId 列表
    tags = profile.get("tags") or []
    if isinstance(tags, list):
        tag_ids: list[str] = []
        for t in tags:
            if isinstance(t, dict):
                tid = t.get("id") or t.get("tagId") or t.get("tag_id")
                if tid and isinstance(tid, str):
                    tag_ids.append(tid)
        resolved.tag_ids = tag_ids

    # orders → 可能有 thirdUserId
    orders = profile.get("orders") or []
    if isinstance(orders, list) and orders:
        for o in orders:
            if isinstance(o, dict):
                for key in ("thirdUserId", "userId", "id"):
                    val = o.get(key)
                    if val:
                        try:
                            resolved.third_user_id = int(val)
                        except (TypeError, ValueError):
                            pass
                        break
                if resolved.third_user_id:
                    break


def _extract_from_detail(resolved: ResolvedIDs, detail: dict[str, Any]) -> None:
    """从 7 返回里补充字段。"""
    for key in ("thirdSaleUuid", "saleUuid", "uuid"):
        val = detail.get(key)
        if val and isinstance(val, str):
            resolved.third_sale_uuid = val
            break
    for key in ("thirdUserId", "userId", "id"):
        val = detail.get(key)
        if val:
            try:
                resolved.third_user_id = int(val)
            except (TypeError, ValueError):
                pass
            break

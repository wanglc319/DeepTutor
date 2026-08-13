"""
Shirley 直播 Skill
==================

封装 Shirley MCP 4 + 4.1 两个直播接口，对外一个 API:
  - list_weekly_lives(corpid, external_userid) → list[LiveSession]
    内部流程: 4.1 get_mantis_promoter_weekly_live_links 拿到所有场次的 liveId
            → 4 get_mantis_live_link(liveId) 逐场补全域名/时间/标题/播放链接

同时注册为 DeepTutor Tool `shirley_get_live_schedule`，LLM 可以在 runtime 场景
（比如"给我安排下周直播提醒"）主动调用。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from deeptutor.core.tool_protocol import BaseTool, ToolDefinition, ToolParameter

from . import client
from .resolver import ResolvedIDs, resolve_all

logger = logging.getLogger(__name__)


@dataclass
class LiveSession:
    """一场直播的完整信息（已经补全域名+时间+标题）。"""

    live_id: int | None = None
    title: str = ""
    start_time: str = ""  # ISO / "yyyy-MM-dd HH:mm:ss"
    end_time: str = ""
    host: str = ""
    status: str = ""  # upcoming / live / ended
    play_url: str = ""
    start_timestamp: int | None = None
    camp_period_name: str = ""  # 4.1 返回的 campPeriodName, 如 "第1期"
    name: str = ""  # 4.1 返回的 name, 如 "训练营直播课"


async def list_weekly_lives(
    corpid: str,
    external_userid: str,
    *,
    domain: str | None = None,
    resolved: ResolvedIDs | None = None,
    qywx_userid: str | None = None,
) -> list[LiveSession]:
    """拉取下一场/本周可预约的直播场次，每场均补全 play_url。

    优先用 resolved 里的 qywxUserid / campId / addedAt，解不出来留空。
    """
    if not corpid or not external_userid:
        return []

    if resolved is None:
        resolved = await resolve_all(corpid, external_userid, domain=domain, qywx_userid=qywx_userid)

    added_at = resolved.added_at or ""
    qywx_userid = resolved.qywx_userid or ""
    camp_id = resolved.camp_id or 19913

    # ── 4.1: 拿 liveId 列表（失败重试, 耗尽记日志返回 None → 上层跳过）──
    weekly = await client.call_tool_with_retry("get_mantis_promoter_weekly_live_links", {
        "addedAt": added_at,
        "qywxUserid": qywx_userid,
        "campId": camp_id,
    })
    if not isinstance(weekly, dict):
        return []

    # 4.1 实际返回: {campId, mantisAccount, targetWeekStart, targetWeekEnd, liveCount, lives: [...]}
    # 兼容其他形状: data / result / response
    mantis_account = str(weekly.get("mantisAccount") or "")
    items: list[Any] = []
    if isinstance(weekly.get("lives"), list):
        items = weekly["lives"]
    else:
        for key in ("data", "result", "response"):
            if isinstance(weekly.get(key), list):
                items = weekly[key]
                break
            if isinstance(weekly.get(key), dict) and isinstance(weekly[key].get("list"), list):
                items = weekly[key]["list"]
                break
        if not items and isinstance(weekly, list):
            items = weekly

    # Sales 固定配置: domain=qn715.hvawb.citv.cn, scene=CAMP_COURSE, linkType=LINK
    default_domain = resolved.domain or "qn715.hvawb.citv.cn"

    # ── 4: 逐场补全播放链接 ──
    sessions: list[LiveSession] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        live_id = item.get("liveId") or item.get("id")
        try:
            live_id = int(live_id) if live_id is not None else None
        except (TypeError, ValueError):
            live_id = None

        camp_period_name = str(item.get("campPeriodName") or "")
        name = str(item.get("name") or "")
        title = (item.get("title") or item.get("liveTitle") or name)
        start_raw = item.get("beginTime") or item.get("startTime") or item.get("start_time") or ""
        start = _fmt_time(start_raw)
        end = _fmt_time(item.get("endTime") or item.get("end_time") or "")
        host = (item.get("host") or item.get("anchor") or "")
        status = (item.get("status") or item.get("liveStatus") or "")
        start_ts = item.get("startTimestamp") or _time_to_ts(start_raw)

        # 先看看 4.1 本身有没有给链接
        play_url = (item.get("pageUrl") or item.get("playUrl") or item.get("play_url") or item.get("url") or "")

        # 没有就调 4 补（失败重试）— 必填 liveId + scene + domain
        if live_id and not play_url:
            args4: dict[str, Any] = {
                "liveId": live_id,
                "scene": "CAMP_COURSE",
                "domain": default_domain,
                "linkType": "LINK",
            }
            if mantis_account:
                args4["shareUserId"] = mantis_account
            detail = await client.call_tool_with_retry("get_mantis_live_link", args4)
            if isinstance(detail, dict):
                play_url = (
                    detail.get("pageUrl")
                    or detail.get("url")
                    or detail.get("playUrl")
                    or detail.get("play_url")
                    or detail.get("liveUrl")
                    or ""
                )
                if not title:
                    title = detail.get("title", "")
                if not start:
                    start = (
                        detail.get("startTime")
                        or detail.get("start_time")
                        or ""
                    )
                if not end:
                    end = (
                        detail.get("endTime")
                        or detail.get("end_time")
                        or ""
                    )

        sessions.append(LiveSession(
            live_id=live_id,
            title=str(title or ""),
            start_time=str(start or ""),
            end_time=str(end or ""),
            host=str(host or ""),
            status=str(status or ""),
            play_url=str(play_url or ""),
            start_timestamp=(int(start_ts) if start_ts else None),
            camp_period_name=camp_period_name,
            name=name,
        ))

    # 按开始时间排，即将开始的在前
    sessions.sort(key=lambda s: s.start_timestamp or 0)
    return sessions


def _fmt_time(val: Any) -> str:
    """把时间字段统一成字符串。4.1 的 beginTime 可能是数组 [2026, 8, 18, 19, 0]。"""
    if isinstance(val, list) and len(val) >= 5:
        try:
            y, mo, d, h, mi = (int(x) for x in val[:5])
            return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:00"
        except (TypeError, ValueError):
            return ""
    return str(val or "")


def _time_to_ts(val: Any) -> int | None:
    """时间字段 → 时间戳（用于排序）。"""
    if isinstance(val, list) and len(val) >= 5:
        try:
            from datetime import datetime
            y, mo, d, h, mi = (int(x) for x in val[:5])
            return int(datetime(y, mo, d, h, mi).timestamp())
        except (TypeError, ValueError):
            return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str) and val:
        from datetime import datetime
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return int(datetime.strptime(val.split(".")[0].split("+")[0], fmt).timestamp())
            except ValueError:
                continue
    return None


def format_live_lines(sessions: list[LiveSession]) -> str:
    """把多场直播拼成推送文案行: 每场一行 "name：链接"。"""
    lines: list[str] = []
    for s in sessions:
        if not s.play_url:
            continue
        label = s.name.strip()
        if label:
            lines.append(f"{label}：{s.play_url}")
        else:
            lines.append(s.play_url)
    return "\n".join(lines)


async def pick_best_live(
    corpid: str,
    external_userid: str,
    *,
    prefer_live: bool = False,
    qywx_userid: str | None = None,
) -> LiveSession | None:
    """list_weekly_lives 里挑一场最适合推送的:

    prefer_live=True  → 正在直播的优先
    prefer_live=False → 最近的即将开始场次
    """
    sessions = await list_weekly_lives(corpid, external_userid, qywx_userid=qywx_userid)
    if not sessions:
        return None
    if prefer_live:
        for s in sessions:
            if s.status in ("live", "1", 1, "onAir", "on_air"):
                return s
    return sessions[0]


# ── DeepTutor Tool 注册 ──

class ShirleyGetLiveScheduleTool(BaseTool):
    """Tool 给 LLM 查 Shirley 直播排班 + 播放链接。"""

    name = "shirley_get_live_schedule"
    description = (
        "查询客户可预约/可回放的 Shirley 直播场次，返回每场的标题、时间、播放链接。"
        " 需传入 corpid + external_userid（企业微信客户标识）。"
    )

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=[
                ToolParameter(
                    name="corpid",
                    type="string",
                    description="企业微信 corpid",
                    required=True,
                ),
                ToolParameter(
                    name="external_userid",
                    type="string",
                    description="企微 external_userid（客户的企微 userid）",
                    required=True,
                ),
                ToolParameter(
                    name="prefer_live",
                    type="boolean",
                    description="true=优先返回正在直播的场次；false=返回最近的即将开始场次。默认 false。",
                    required=False,
                    default=False,
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        corpid = str(kwargs.get("corpid") or "")
        external_userid = str(kwargs.get("external_userid") or "")
        prefer_live = bool(kwargs.get("prefer_live", False))

        if not corpid or not external_userid:
            return {"ok": False, "error": "缺少 corpid 或 external_userid"}

        sessions = await list_weekly_lives(corpid, external_userid)
        if not sessions:
            return {"ok": True, "sessions": [], "summary": "本周暂无直播场次"}

        best = await pick_best_live(
            corpid, external_userid, prefer_live=prefer_live
        )

        return {
            "ok": True,
            "best": (
                {
                    "live_id": best.live_id,
                    "title": best.title,
                    "start_time": best.start_time,
                    "play_url": best.play_url,
                }
                if best
                else None
            ),
            "sessions": [
                {
                    "live_id": s.live_id,
                    "title": s.title,
                    "start_time": s.start_time,
                    "play_url": s.play_url,
                    "status": s.status,
                }
                for s in sessions
            ],
        }

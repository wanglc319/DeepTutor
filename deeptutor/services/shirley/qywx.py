"""
Shirley 企微 Skill
==================

封装 Shirley MCP 2 / 3 / 6 / 7 四个企微接口。读写分开:
  - read  层: get_customer_detail  (7)
              query_chat_history   (3)     ← 注意: 5.2 也能查聊天，用 3 是因为
                                                它直接走企微 API，返回原始消息对象
  - write 层: mark_tags            (2)     ← 打标签（幂等: 前置 tagIds[] 去重）
              send_text_message    (6)     ← 发 Lisa 自动回复

所有 Tool 的参数都先走 resolver 做 ID 映射，LLM 只给 corpid + external_userid
就能跑通。解不出来的（如 tagIds、followUserid、thirdUserId）允许显式传参覆盖。

注册 4 个 DeepTutor Tool:
  shirley_qywx_customer_detail  (读客户详情)
  shirley_qywx_chat_history     (读聊天)
  shirley_qywx_mark_tags        (写打标签)
  shirley_qywx_send_message     (写发消息)
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from deeptutor.core.tool_protocol import BaseTool, ToolDefinition, ToolParameter

from . import client
from .resolver import ResolvedIDs, resolve_all

logger = logging.getLogger(__name__)


# ── 幂等保护（进程内） ──

# mark_tags: (corpid, external_userid, frozenset(tag_ids)) → 上次写入时间
_TAG_WRITE_CACHE: dict[tuple[str, str, frozenset[str]], float] = {}
_TAG_WRITE_TTL = 60.0  # 60 秒内同组合直接跳过


# send_message: (corpid, external_userid, text_hash) → 上次发送时间
_MSG_SEND_CACHE: dict[tuple[str, str, str], float] = {}
_MSG_SEND_TTL = 30.0


# ── Read 层: 7 查客户详情 ──

async def get_customer_detail(
    corpid: str,
    external_userid: str,
    *,
    third_user_id: int | None = None,
    resolved: ResolvedIDs | None = None,
) -> dict[str, Any] | None:
    """调 Shirley MCP get_qywx_external_detail_v2 (7)。

    third_user_id 可选，不传就先用 5.1 解一遍。
    """
    if not corpid or not external_userid:
        return None
    if resolved is None:
        resolved = await resolve_all(corpid, external_userid)

    tuid = third_user_id or resolved.third_user_id
    if tuid is None:
        logger.warning(
            "get_customer_detail: 没有 thirdUserId，跳过 7 调用 "
            "(corpid=%s, external_userid=%s)",
            corpid, external_userid,
        )
        return None

    return await client.safe_call_tool("get_qywx_external_detail_v2", {
        "thirdUserId": int(tuid),
    })


# ── Read 层: 3 查聊天历史 ──

async def query_chat_history_via_3(
    corpid: str,
    external_userid: str,
    *,
    vid: str | None = None,
    uuid: str | None = None,
    qywx_userid: str | None = None,
    user_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
    resolved: ResolvedIDs | None = None,
) -> dict[str, Any] | None:
    """调 Shirley MCP query_user_sales_chat_history (3)。

    注意: 3 要求 vid + uuid + qywxUserid + userId，这些字段 Shirley 文档
    里没给 corpid/externalUserid 到它们的直接映射。
    所以这里只做参数透传 —— 调用方必须自己知道这些 ID 或通过其它工具解。

    如果只知道 corpid + external_userid，用 profile.query_chat_history (5.2)
    更合适，它不要求 vid/uuid。
    """
    if not corpid or not external_userid:
        return None
    if resolved is None:
        resolved = await resolve_all(corpid, external_userid)

    payload: dict[str, Any] = {
        "vid": vid or "",
        "uuid": uuid or resolved.third_sale_uuid or "",
        "qywxUserid": qywx_userid or resolved.qywx_userid or "",
        "userId": int(user_id or resolved.user_id or 0) or 0,
        "limit": max(1, min(500, int(limit))),
        "offset": int(offset),
    }
    # 4 个核心 ID 全空 → 没必要发
    if not payload["vid"] and not payload["uuid"] and not payload["qywxUserid"] and not payload["userId"]:
        logger.warning("query_chat_history_via_3: 所有核心 ID 为空，跳过")
        return None

    return await client.safe_call_tool("query_user_sales_chat_history", payload)


# ── Write 层: 2 打标签 ──

async def mark_tags(
    corpid: str,
    external_userid: str,
    tag_ids: list[str],
    *,
    follow_userid: str | None = None,
    resolved: ResolvedIDs | None = None,
    skip_dedup: bool = False,
) -> dict[str, Any] | None:
    """调 Shirley MCP mark_qywx_customer_tags (2)。

    幂等保护:
      - 进程内 60s TTL: 同 (corpid, external_userid, tag_ids 集合) 不重复写
      - skip_dedup=True 可以强制重写（调试用）

    如果调用者已经有客户的现有 tagIds[]（比如从 5.1 拉过），也可以
    提前传入 resolved.tag_ids 做差集。但真正写入前 Shirley 本身会做
    upsert，重复打同一个标签不会报错。
    """
    if not corpid or not external_userid or not tag_ids:
        return None

    tag_ids = [str(t) for t in tag_ids if t]
    if not tag_ids:
        return None

    # 幂等: 最近 60s 写过同组合直接跳过
    cache_key = (corpid, external_userid, frozenset(tag_ids))
    if not skip_dedup and cache_key in _TAG_WRITE_CACHE:
        if time.time() - _TAG_WRITE_CACHE[cache_key] < _TAG_WRITE_TTL:
            logger.info("mark_tags: 幂等命中，跳过 tag_ids=%s", tag_ids)
            return {"ok": True, "skipped_dedup": True, "tagIds": tag_ids}

    if resolved is None:
        resolved = await resolve_all(corpid, external_userid)

    # 如果没显式传 follow_userid，用 resolved.primary_follow_userid 或第一个
    fu = follow_userid or resolved.primary_follow_userid
    if not fu and resolved.follow_userids:
        fu = resolved.follow_userids[0]
    if not fu:
        # 最后回退用 qywx_userid
        fu = resolved.qywx_userid or ""

    result = await client.safe_call_tool("mark_qywx_customer_tags", {
        "externalUserid": external_userid,
        "corpid": corpid,
        "followUserid": fu,
        "tagIds": tag_ids,
    })

    # 写缓存
    if result is not None:
        _TAG_WRITE_CACHE[cache_key] = time.time()

    return result or {"ok": False, "error": "mark_qywx_customer_tags returned None"}


# ── Write 层: 6 发 Lisa 自动回复 ──

_MSG_TYPE_TEXT = 1
_MSG_TYPE_REJECT = 119

_MSG_TYPE_NAMES: dict[str | int, int] = {
    "text": _MSG_TYPE_TEXT,
    "1": _MSG_TYPE_TEXT,
    1: _MSG_TYPE_TEXT,
    "reject": _MSG_TYPE_REJECT,
    "119": _MSG_TYPE_REJECT,
    119: _MSG_TYPE_REJECT,
}


def _normalize_msg_type(msg_type: str | int) -> int:
    if isinstance(msg_type, int):
        return msg_type
    return _MSG_TYPE_NAMES.get(msg_type, _MSG_TYPE_TEXT)


async def send_lisa_message(
    corpid: str,
    external_userid: str,
    msg_text: str,
    *,
    third_sale_uuid: str | None = None,
    third_user_id: int | None = None,
    vid: int | None = None,
    msg_type: str | int = _MSG_TYPE_TEXT,
    answer: str | None = None,
    resolved: ResolvedIDs | None = None,
    skip_dedup: bool = False,
    disable_dedup: bool = False,
) -> dict[str, Any] | None:
    """调 Shirley MCP reply_lisa_message (6) —— 通过 Lisa 账号给客户发消息。

    参数:
      msg_type: "text"/1 = 普通文本推送；"reject"/119 = 拒绝/转人工通知
      answer:  仅 msg_type=119 时传，LLM 生成的拒绝/转人工原因纯文本
      vid:     仅 msg_type=119 时需要，Shirley 网关要求 vid 或 thirdUserId
      disable_dedup: 彻底跳过幂等检查（批量逐句推送时传 True，
                    避免同轮多次被 30s TTL 吞掉）

    幂等保护:
      - 默认 30s TTL: 同 (corpid, external_userid, text_hash) 不重复发
      - skip_dedup=True 强制绕过；disable_dedup=True 彻底禁用
    """
    if not corpid or not external_userid or not msg_text:
        return None

    normalized_type = _normalize_msg_type(msg_type)

    # 幂等: 30s 内同文本不重复发
    text_hash = str(hash(msg_text))
    cache_key = (corpid, external_userid, text_hash)
    if not disable_dedup and not skip_dedup and cache_key in _MSG_SEND_CACHE:
        if time.time() - _MSG_SEND_CACHE[cache_key] < _MSG_SEND_TTL:
            logger.info("send_lisa_message: 幂等命中，跳过")
            return {"ok": True, "skipped_dedup": True}

    if resolved is None:
        resolved = await resolve_all(corpid, external_userid)

    third_uuid = third_sale_uuid or resolved.third_sale_uuid or ""
    third_uid = int(third_user_id or resolved.third_user_id or 0) or 0

    payload: dict[str, Any] = {
        "thirdSaleUuid": third_uuid,
        "thirdUserId": third_uid,
        "msgType": normalized_type,
        "content": msg_text,
        "answer": answer or msg_text,
    }
    # 119 转人工: Shirley 网关要求 vid 或 thirdUserId, 有 vid 就带上
    if normalized_type == _MSG_TYPE_REJECT and vid:
        try:
            payload["vid"] = int(vid)
        except (TypeError, ValueError):
            pass

    # 两个核心 ID 都没 → 跳过
    if not payload["thirdSaleUuid"] and not payload["thirdUserId"]:
        logger.warning("send_lisa_message: thirdSaleUuid 和 thirdUserId 都为空，跳过")
        return None

    result = await client.safe_call_tool("reply_lisa_message", payload)

    if result is not None and not disable_dedup:
        _MSG_SEND_CACHE[cache_key] = time.time()

    return result


# ── DeepTutor Tool 注册 ──

class ShirleyQywxCustomerDetailTool(BaseTool):
    name = "shirley_qywx_customer_detail"
    description = (
        "查询 Shirley 企微客户详情（基础信息、跟进员工、订单等）。"
        " 需 corpid + external_userid。"
    )

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=[
                ToolParameter(name="corpid", type="string", required=True),
                ToolParameter(name="external_userid", type="string", required=True),
                ToolParameter(
                    name="third_user_id", type="integer", required=False,
                    description="Sales 系统 thirdUserId，可选（不传则自动从 5.1 解）",
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        result = await get_customer_detail(
            corpid=str(kwargs.get("corpid") or ""),
            external_userid=str(kwargs.get("external_userid") or ""),
            third_user_id=kwargs.get("third_user_id"),
        )
        if result is None:
            return {"ok": False, "error": "查询失败或缺少 thirdUserId"}
        return {"ok": True, "detail": result}


class ShirleyQywxChatHistoryTool(BaseTool):
    name = "shirley_qywx_chat_history"
    description = (
        "查询 Shirley 企微销售与客户的聊天历史（走 sales_chat_history 接口，"
        "比 profile_chat_history 更原始，返回完整消息对象）。"
        "需要 corpid + external_userid + 可选的 vid/uuid/qywxUserid/userId。"
    )

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=[
                ToolParameter(name="corpid", type="string", required=True),
                ToolParameter(name="external_userid", type="string", required=True),
                ToolParameter(name="vid", type="string", required=False, default=""),
                ToolParameter(name="uuid", type="string", required=False, default=""),
                ToolParameter(name="qywx_userid", type="string", required=False, default=""),
                ToolParameter(name="user_id", type="integer", required=False, default=0),
                ToolParameter(name="limit", type="integer", required=False, default=100),
                ToolParameter(name="offset", type="integer", required=False, default=0),
            ],
        )

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        result = await query_chat_history_via_3(
            corpid=str(kwargs.get("corpid") or ""),
            external_userid=str(kwargs.get("external_userid") or ""),
            vid=kwargs.get("vid") or None,
            uuid=kwargs.get("uuid") or None,
            qywx_userid=kwargs.get("qywx_userid") or None,
            user_id=kwargs.get("user_id"),
            limit=int(kwargs.get("limit", 100)),
            offset=int(kwargs.get("offset", 0)),
        )
        if result is None:
            return {"ok": False, "error": "查询失败或缺少必要 ID（vid/uuid/qywxUserid/userId 至少一个）"}
        return {"ok": True, "chat": result}


class ShirleyQywxMarkTagsTool(BaseTool):
    name = "shirley_qywx_mark_tags"
    description = (
        "给 Shirley 企微客户打标签。会做进程内 60s 幂等保护 —— 60 秒内"
        "同一客户+标签集合不重复写。"
        "需 corpid + external_userid + tagIds（字符串数组）。"
    )

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=[
                ToolParameter(name="corpid", type="string", required=True),
                ToolParameter(name="external_userid", type="string", required=True),
                ToolParameter(
                    name="tag_ids", type="array",
                    description="要打的标签 ID 数组",
                    required=True,
                    items={"type": "string"},
                ),
                ToolParameter(
                    name="follow_userid", type="string", required=False,
                    description="跟进成员企微 userid，可选（不传自动从 5.1 解）",
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        raw = kwargs.get("tag_ids") or kwargs.get("tagIds") or []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = [t.strip() for t in raw.replace(",", ",").split(",") if t.strip()]
        if not isinstance(raw, list):
            raw = [str(raw)] if raw else []

        result = await mark_tags(
            corpid=str(kwargs.get("corpid") or ""),
            external_userid=str(kwargs.get("external_userid") or ""),
            tag_ids=[str(t) for t in raw],
            follow_userid=kwargs.get("follow_userid") or None,
        )
        if result is None:
            return {"ok": False, "error": "打标签失败"}
        return {"ok": True, "result": result}


class ShirleyQywxSendMessageTool(BaseTool):
    name = "shirley_qywx_send_message"
    description = (
        "通过 Shirley Lisa 账号给企微客户发送自动回复消息（text / image / file）。"
        "会做进程内 30s 幂等保护 —— 30 秒内同一客户+同文本不重复发。"
        "需 corpid + external_userid + 消息文本。"
    )

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=[
                ToolParameter(name="corpid", type="string", required=True),
                ToolParameter(name="external_userid", type="string", required=True),
                ToolParameter(
                    name="msg_text", type="string", required=True,
                    description="消息内容（文本类型为纯文本）",
                ),
                ToolParameter(
                    name="msg_type", type="string", required=False, default="text",
                    description="消息类型: text / image / file / markdown",
                    enum=["text", "image", "file", "markdown"],
                ),
                ToolParameter(
                    name="third_sale_uuid", type="string", required=False,
                    description="Sales 账号 uuid，可选（不传自动从 5.1/7 解）",
                ),
                ToolParameter(
                    name="third_user_id", type="integer", required=False,
                    description="Sales 客户 thirdUserId，可选（不传自动解）",
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        result = await send_lisa_message(
            corpid=str(kwargs.get("corpid") or ""),
            external_userid=str(kwargs.get("external_userid") or ""),
            msg_text=str(kwargs.get("msg_text") or ""),
            msg_type=str(kwargs.get("msg_type") or "text"),
            third_sale_uuid=kwargs.get("third_sale_uuid") or None,
            third_user_id=kwargs.get("third_user_id"),
        )
        if result is None:
            return {"ok": False, "error": "发送失败或缺少 thirdSaleUuid/thirdUserId"}
        return {"ok": True, "result": result}

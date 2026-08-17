"""
Sale Chat HTTP 路由
===================

给企微/Lisa 销售侧的专用聊天入口。与 partners.py 的 admin 路由隔离，
免认证，独立前缀 /api/v1/partners/lisa/saleChat。

请求进来立即返回 200 + ok=true；实际业务逻辑（10s debounce 聚合 → LLM →
逐句 MCP 6 推送）跑在后台 asyncio.Task 里。

文档对应: https://jcnmgzcga30e.feishu.cn/file/Fdx3bQbkWoFcZbxkYnjcBqjvnbS
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

router = APIRouter()

DEFAULT_CAMP_ID = 19913


class PrimeInfo(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    thirdSaleUuid: str = ""
    thirdUserId: int | None = None
    originalUserId: str = ""
    corpid: str = ""
    vid: int | None = None
    qywxUserid: str = ""
    isProd: bool = False


class SaleChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    content: str = ""
    session_id: str | None = Field(default=None, alias="sessionId")
    chat_id: str | None = Field(default=None, alias="chatId")
    msgType: int | None = None
    reqType: str = "chat"
    primeInfo: PrimeInfo | None = None


@router.post("/lisa/saleChat")
async def sale_chat_endpoint(payload: SaleChatRequest) -> dict[str, Any]:
    """接收企微/Lisa 销售侧的一条用户消息。

    立即返回成功；实际处理走 10s debounce 聚合 + 后台业务逻辑。
    """
    # 1. 基础参数
    prime = payload.primeInfo or PrimeInfo()
    content = (payload.content or "").strip()

    # 聚合 key: 优先用 primeInfo 里的 originalUserId；没有就用请求里的 session_id
    # 再没有 fallback 到 prime.thirdSaleUuid
    sid_raw = (payload.session_id or "").strip()
    session_id = (
        sid_raw
        or prime.originalUserId.strip()
        or prime.thirdSaleUuid.strip()
        or ""
    )

    if not session_id:
        return {
            "partner_id": "lisa",
            "ok": False,
            "error": "缺少聚合 session_id（传 session_id / primeInfo.originalUserId / primeInfo.thirdSaleUuid 任一）",
            "received": payload.model_dump(by_alias=True),
        }

    # corpid: primeInfo 里取；没有就用环境变量；都没就空
    corpid = prime.corpid or os.getenv("SHIRLEY_CORPID", "")

    # external_userid: 用 primeInfo.originalUserId
    external_userid = prime.originalUserId or ""

    # 把 corpid 也补进 primeInfo 方便后续透传
    prime_info = prime.model_dump(by_alias=True)
    prime_info.setdefault("corpid", corpid)
    # campId 固定 19913 + 默认 qywxUserid 兜底
    prime_info.setdefault("campId", DEFAULT_CAMP_ID)
    if not prime_info.get("qywxUserid"):
        prime_info["qywxUserid"] = os.getenv("SHIRLEY_QYWX_USERID", "")

    message_envelope = {
        "content": content,
        "msgType": payload.msgType,
        "reqType": payload.reqType,
        "chat_id": payload.chat_id,
        "received_at": __import__("time").time(),
    }

    # welcome 类型是销售自己发的欢迎语，不走 AI 链路，直接忽略
    if payload.reqType == "welcome":
        logger.info("[saleChat] 忽略 welcome 类型消息: session_id=%s content_len=%d", session_id, len(content))
        return {
            "partner_id": "lisa",
            "ok": True,
            "session_id": session_id,
            "skipped": True,
            "reason": "welcome_type_ignored",
        }

    # 2. 进 debounce 队列（立即返回）
    try:
        from deeptutor.services.partners import sale_chat as sale_chat_svc
        await sale_chat_svc.enqueue(
            session_id=session_id,
            message=message_envelope,
            corpid=corpid,
            external_userid=external_userid,
            prime_info=prime_info,
        )
    except Exception as e:
        logger.exception("sale_chat.enqueue failed: %s", e)
        return {
            "partner_id": "lisa",
            "ok": False,
            "error": f"enqueue failed: {e}",
            "session_id": session_id,
        }

    return {
        "partner_id": "lisa",
        "ok": True,
        "session_id": session_id,
        "corpid": corpid,
        "message_enqueued": True,
        "received": payload.model_dump(by_alias=True),
    }

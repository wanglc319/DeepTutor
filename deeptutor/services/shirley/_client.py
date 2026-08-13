"""
Shirley MCP 底层客户端 —— Streamable HTTP 短连接会话 + tools/call 统一封装.

所有业务模块共用这一层，不重复写 initialize / tools/call 的样板代码。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SHIRLEY_MCP_URL = "https://prod-shirley-gateway.xueliyingyu.com/ai/mcp"


class ShirleyClient:
    """Shirley MCP 网关客户端。每次调用 create_session() 拿到独立的 Streamable HTTP 短连接会话。"""

    def __init__(self, base_url: str = SHIRLEY_MCP_URL, timeout: float = 30.0):
        self.base_url = base_url
        self.timeout = timeout

    async def create_session(self) -> "_Session":
        sess = _Session(self.base_url, self.timeout)
        await sess._init()
        return sess


class _Session:
    """单次 MCP 调用的短连接会话：initialize → tools/call → notifications/initialized."""

    def __init__(self, url: str, timeout: float):
        self.url = url
        self.timeout = timeout
        self.session_id: str | None = None

    async def _init(self) -> None:
        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as c:
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            r = await c.post(self.url, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "deeptutor", "version": "1.0"},
                },
            }, headers=headers)
            r.raise_for_status()
            self.session_id = r.headers.get("mcp-session-id")
            await c.post(self.url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as c:
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            if self.session_id:
                headers["mcp-session-id"] = self.session_id
            r = await c.post(self.url, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }, headers=headers)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise RuntimeError(f"Shirley MCP JSON-RPC error [{tool_name}]: {data['error']}")
            result = data.get("result", {})
            if result.get("isError"):
                raise RuntimeError(f"Shirley MCP tool error [{tool_name}]: {result}")
            content = result.get("content", [])
            if isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict) and "text" in first:
                    try:
                        import json as _json
                        return _json.loads(first["text"])
                    except (ValueError, TypeError):
                        return first["text"]
            return result


_default_client: ShirleyClient | None = None


def get_client() -> ShirleyClient:
    global _default_client
    if _default_client is None:
        _default_client = ShirleyClient()
    return _default_client


async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """便捷调用：自动管理 session 生命周期。"""
    sess = await get_client().create_session()
    try:
        return await sess.call(tool_name, arguments)
    finally:
        pass

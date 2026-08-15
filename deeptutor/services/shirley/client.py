"""
Shirley MCP Streamable HTTP Client
==================================

所有 Shirley MCP 工具调用的底层入口。封装了 MCP 会话生命周期、
错误检查、超时重试。上层 Skill（profile / live / qywx）只调
:func:`call_tool`，不用关心 Streamable HTTP 细节。

MCP 服务地址: https://prod-shirley-gateway.xueliyingyu.com/ai/mcp
协议: Streamable HTTP (jsonrpc 2.0)
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

from deeptutor.observability.agent_monitor import mark_mcp_failed, observation, redact_pii

logger = logging.getLogger(__name__)

SHIRLEY_MCP_URL = os.getenv(
    "SHIRLEY_MCP_URL", "https://prod-shirley-gateway.xueliyingyu.com/ai/mcp"
)
SHIRLEY_TIMEOUT = float(os.getenv("SHIRLEY_TIMEOUT", "30"))
SHIRLEY_API_TOKEN = os.getenv("SHIRLEY_API_TOKEN", "")

_PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "deeptutor", "version": "2.0"}

_MAX_LOG_LEN = 400


def _snippet(obj: Any) -> str:
    try:
        if isinstance(obj, str):
            s = obj
        else:
            s = json.dumps(obj, ensure_ascii=False, default=str)
        if len(s) > _MAX_LOG_LEN:
            s = s[:_MAX_LOG_LEN] + "...(truncated)"
        return s
    except Exception:
        return repr(obj)[:_MAX_LOG_LEN]


class ShirleyMCPError(RuntimeError):
    """所有 Shirley MCP 调用错误的基类。"""


class ShirleyMCPToolError(ShirleyMCPError):
    """MCP 工具返回了 isError=true 或 error 字段。"""


class ShirleyMCPNetworkError(ShirleyMCPError):
    """网络层失败（连接超时、HTTP 5xx 等）。"""


async def call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """调用任意 Shirley MCP 工具，返回解析后的 JSON 结果。

    所有网络异常 → ShirleyMCPNetworkError（上层可以捕获降级）。
    自动打结构化日志：入参 / 出参 / 耗时 / 成功失败。
    """
    with observation(
        f"mcp.{tool_name}",
        input=arguments,
        metadata={"tool_name": tool_name, "endpoint": SHIRLEY_MCP_URL},
        as_type="tool",
    ) as span:
        try:
            result = await _call_tool(tool_name, arguments)
        except Exception as exc:
            mark_mcp_failed()
            if span is not None:
                span.update(level="ERROR", status_message=str(exc))
            raise

        if span is not None:
            span.update(output=redact_pii(result))
        return result


async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """执行 Shirley MCP 工具调用。"""
    t0 = time.perf_counter()
    logger.info("[Shirley MCP →] %s | args=%s", tool_name, _snippet(arguments))

    url = SHIRLEY_MCP_URL
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if SHIRLEY_API_TOKEN:
        headers["Authorization"] = f"Bearer {SHIRLEY_API_TOKEN}"

    try:
        async with httpx.AsyncClient(timeout=SHIRLEY_TIMEOUT, verify=False) as c:
            # --- 1. Initialize ---
            init_resp = await c.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": _PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": _CLIENT_INFO,
                    },
                },
                headers=headers,
            )
            init_resp.raise_for_status()
            session_id = init_resp.headers.get("mcp-session-id")
            if session_id:
                headers["mcp-session-id"] = session_id

            # --- 2. Initialized notification ---
            await c.post(
                url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers,
            )

            # --- 3. Call tool ---
            call_resp = await c.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                },
                headers=headers,
            )
            call_resp.raise_for_status()
            raw = call_resp.json()

        # --- 4. Check JSON-RPC error ---
        if "error" in raw:
            err = raw["error"]
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.warning(
                "[Shirley MCP ←✗] %s | elapsed_ms=%d | jsonrpc_error=%s",
                tool_name, elapsed_ms, _snippet(err),
            )
            raise ShirleyMCPToolError(f"[{tool_name}] JSON-RPC error: {err}")

        result = raw.get("result") or {}

        # --- 5. Check MCP isError ---
        if result.get("isError"):
            content = result.get("content") or []
            err_text = ""
            if content and isinstance(content, list):
                first = content[0]
                if isinstance(first, dict):
                    err_text = first.get("text") or ""
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.warning(
                "[Shirley MCP ←✗] %s | elapsed_ms=%d | tool_isError=%s",
                tool_name, elapsed_ms, err_text or _snippet(result),
            )
            raise ShirleyMCPToolError(
                f"[{tool_name}] tool-level error: {err_text or result}"
            )

        # --- 6. Parse content ---
        content = result.get("content") or []
        parsed: Any
        if not content:
            parsed = result
        else:
            first = content[0]
            if isinstance(first, dict):
                text = first.get("text", "")
                if isinstance(text, str):
                    try:
                        parsed = json.loads(text)
                    except (ValueError, TypeError):
                        parsed = text
                else:
                    parsed = first
            else:
                parsed = content

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "[Shirley MCP ←✓] %s | elapsed_ms=%d | result=%s",
            tool_name, elapsed_ms, _snippet(parsed),
        )
        return parsed

    except httpx.TimeoutException as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.error(
            "[Shirley MCP ←✗] %s | elapsed_ms=%d | TIMEOUT %s",
            tool_name, elapsed_ms, e,
        )
        raise ShirleyMCPNetworkError(f"[{tool_name}] timeout: {e}") from e
    except httpx.HTTPStatusError as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.error(
            "[Shirley MCP ←✗] %s | elapsed_ms=%d | HTTP %d %s",
            tool_name, elapsed_ms, e.response.status_code, _snippet(str(e)),
        )
        raise ShirleyMCPNetworkError(f"[{tool_name}] HTTP {e.response.status_code}") from e
    except ShirleyMCPError:
        raise
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.error(
            "[Shirley MCP ←✗] %s | elapsed_ms=%d | UNEXPECTED %s: %s",
            tool_name, elapsed_ms, type(e).__name__, e,
        )
        raise ShirleyMCPNetworkError(f"[{tool_name}] unexpected: {e}") from e


async def safe_call_tool(tool_name: str, arguments: dict[str, Any]) -> Any | None:
    """call_tool 的吞掉异常版本，失败返回 None 不抛异常。"""
    try:
        return await call_tool(tool_name, arguments)
    except ShirleyMCPToolError as e:
        logger.warning("[Shirley MCP safe_call ←✗ tool_error] %s | %s", tool_name, e)
        return None
    except ShirleyMCPNetworkError as e:
        logger.warning("[Shirley MCP safe_call ←✗ net_error] %s | %s", tool_name, e)
        return None
    except Exception as e:
        logger.warning("[Shirley MCP safe_call ←✗ unexpected] %s | %s", tool_name, e)
        return None


async def call_tool_with_retry(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    retries: int = 2,
    delay: float = 1.0,
) -> Any | None:
    """call_tool + 失败重试。重试耗尽后记异常日志并返回 None（调用方跳过该步骤）。"""
    import asyncio

    last_err: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            return await call_tool(tool_name, arguments)
        except Exception as e:
            last_err = e
            logger.warning(
                "[Shirley MCP retry] %s | attempt=%d/%d | err=%s",
                tool_name, attempt, retries + 1, e,
            )
            if attempt <= retries:
                await asyncio.sleep(delay * attempt)
    logger.error(
        "[Shirley MCP retry ←✗ GIVE UP] %s | after %d attempts | last_err=%s",
        tool_name, retries + 1, last_err,
    )
    return None

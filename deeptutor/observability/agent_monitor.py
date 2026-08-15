from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import logging
import os
import re
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_PHONE_RE = re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)")
_IDENTITY_KEYS = {"externaluserid", "originaluserid", "qywxuserid", "thirdsaleuuid"}
_NAME_KEYS = {"customername", "name", "nickname"}
_langfuse_client: Any | None = None
_langfuse_initialized = False
_mcp_failed: ContextVar[bool] = ContextVar("agent_monitor_mcp_failed", default=False)


def reset_trace_state() -> None:
    _mcp_failed.set(False)


def mark_mcp_failed() -> None:
    _mcp_failed.set(True)


def has_mcp_failed() -> bool:
    return _mcp_failed.get()


def reset_monitor() -> None:
    global _langfuse_client, _langfuse_initialized
    _langfuse_client = None
    _langfuse_initialized = False


def monitor_enabled() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def flush_monitor() -> None:
    client = _langfuse_client
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        logger.warning("Langfuse flush 失败，业务不受影响", exc_info=True)


def get_monitor() -> Any | None:
    global _langfuse_client, _langfuse_initialized
    if _langfuse_initialized:
        return _langfuse_client
    _langfuse_initialized = True
    if not monitor_enabled():
        return None
    try:
        from langfuse import Langfuse

        _langfuse_client = Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            environment=os.getenv("LANGFUSE_ENVIRONMENT", "development"),
            release=os.getenv("LANGFUSE_RELEASE"),
        )
    except Exception:
        logger.warning("Langfuse 初始化失败，监控已降级关闭", exc_info=True)
        _langfuse_client = None
    return _langfuse_client


@contextmanager
def observation(
    name: str,
    *,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
    as_type: str = "span",
) -> Iterator[Any | None]:
    client = get_monitor()
    if client is None:
        yield None
        return
    redacted_input = redact_pii(input)
    redacted_metadata = redact_pii(metadata or {})
    try:
        context = client.start_as_current_observation(
            name=name,
            input=redacted_input,
            metadata=redacted_metadata,
            as_type=as_type,
        )
    except Exception:
        logger.warning("Langfuse span 创建失败，监控已跳过 | name=%s", name, exc_info=True)
        yield None
        return
    with context as span:
        yield span


@contextmanager
def generation(
    name: str,
    *,
    input: Any,
    model: str,
    model_parameters: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[Any | None]:
    client = get_monitor()
    if client is None:
        yield None
        return
    try:
        context = client.start_as_current_generation(
            name=name,
            input=redact_pii(input),
            model=model,
            model_parameters=model_parameters or {},
            metadata=redact_pii(metadata or {}),
        )
    except Exception:
        logger.warning("Langfuse generation 创建失败，监控已跳过 | name=%s", name, exc_info=True)
        yield None
        return
    with context as current:
        yield current


@dataclass(frozen=True)
class BadCase:
    is_bad: bool
    reasons: tuple[str, ...]


def _mask_identifier(value: str) -> str:
    if len(value) >= 8:
        return f"{value[:4]}***{value[-4:]}"
    if len(value) >= 6:
        return f"{value[:3]}***{value[-3:]}"
    return "***"


def _mask_name(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"用户#{digest}"


def _mask_phone_text(value: str) -> str:
    return _PHONE_RE.sub(r"\1****\2", value)


def redact_pii(value: Any, *, key: str = "") -> Any:
    normalized_key = key.replace("_", "").lower()
    if isinstance(value, dict):
        return {item_key: redact_pii(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact_pii(item, key=key) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_pii(item, key=key) for item in value)
    if not isinstance(value, str):
        return value
    if normalized_key in _NAME_KEYS:
        return _mask_name(value)
    if normalized_key in _IDENTITY_KEYS:
        return _mask_identifier(value)
    return _mask_phone_text(value)


def should_sample(*, sample_rate: float, is_bad_case: bool, random_value: float) -> bool:
    if is_bad_case:
        return True
    rate = max(0.0, min(1.0, sample_rate))
    return random_value < rate


def detect_bad_case(
    *,
    reply: str,
    mcp_failed: bool,
    llm_empty: bool,
    transferred: bool,
    explicit_refusal: bool,
) -> BadCase:
    reasons: list[str] = []
    if mcp_failed:
        reasons.append("mcp_failed")
    if llm_empty:
        reasons.append("llm_empty")
    if transferred:
        reasons.append("transferred")
    if explicit_refusal:
        reasons.append("explicit_refusal")
    if any(text in reply for text in ("我不太理解", "请您再说一遍", "可以再说清楚一点")):
        reasons.append("clarification_fallback")
    lowered = reply.lower()
    if "tool_call" in lowered or "function_call" in lowered or "partner_memorize" in lowered:
        reasons.append("tool_call_leak")
    compact_reply = re.sub(r"\s+", "", reply)
    if len(compact_reply) > 200 and len(set(compact_reply)) / len(compact_reply) < 0.12:
        reasons.append("overlong_low_information")
    unique_reasons = tuple(dict.fromkeys(reasons))
    return BadCase(is_bad=bool(unique_reasons), reasons=unique_reasons)
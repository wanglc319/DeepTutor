from __future__ import annotations

import pytest

from deeptutor.sales import qywx_tags
from deeptutor.services.partners import sale_chat


def test_format_reject_answer_contains_analysis_reason_and_user_message() -> None:
    answer = sale_chat._format_reject_answer(
        "AI分析判定用户明确拒绝或要求停止触达",
        "不用了，别再联系我",
    )

    assert answer == (
        "转人工原因：AI分析判定用户明确拒绝或要求停止触达\n"
        "用户原话：不用了，别再联系我"
    )


def test_translate_refusal_source_to_chinese() -> None:
    assert sale_chat._translate_refusal_source("regex") == "关键词命中"
    assert sale_chat._translate_refusal_source("llm") == "AI判断"
    assert sale_chat._translate_refusal_source("unknown") == "自动判定"
    assert sale_chat._translate_refusal_source("") == "自动判定"
    assert sale_chat._translate_refusal_source(None) == "自动判定"
    # 未知值兜底
    assert sale_chat._translate_refusal_source("some_weird_value") == "some_weird_value"


def test_build_refusal_reason_fallback_is_chinese() -> None:
    reason = sale_chat._build_refusal_reason(
        refusal_text="",
        tag_source={"explicit_refusal": "regex"},
    )
    assert reason == "用户明确拒绝（触发来源：关键词命中）"

    reason_llm = sale_chat._build_refusal_reason(
        refusal_text="",
        tag_source={"explicit_refusal": "llm"},
    )
    assert reason_llm == "用户明确拒绝（触发来源：AI判断）"

    # 有 refusal_text 就直接用
    reason_with_text = sale_chat._build_refusal_reason(
        refusal_text="用户说「不用了，谢谢」",
        tag_source={"explicit_refusal": "regex"},
    )
    assert reason_with_text == "用户说「不用了，谢谢」"


@pytest.mark.asyncio
async def test_apply_do_not_disturb_tag_uses_exact_tag_name(monkeypatch) -> None:
    resolved_names: list[list[str]] = []
    applied: list[dict[str, object]] = []

    async def fake_resolve(names: list[str]) -> list[str]:
        resolved_names.append(names)
        return ["dnd-tag-id"]

    async def fake_apply(**kwargs) -> None:
        applied.append(kwargs)

    monkeypatch.setattr(qywx_tags, "resolve_extra_tag_ids", fake_resolve)
    monkeypatch.setattr(qywx_tags, "apply_tags_to_customer", fake_apply)

    applied_ok = await qywx_tags.apply_do_not_disturb_tag(
        corpid="corp-id",
        external_userid="external-id",
        follow_userid="sale-id",
    )

    assert applied_ok is True
    assert resolved_names == [["勿扰"]]
    assert applied == [
        {
            "corpid": "corp-id",
            "external_userid": "external-id",
            "tag_ids": ["dnd-tag-id"],
            "follow_userid": "sale-id",
        }
    ]

@pytest.mark.asyncio
async def test_push_sentences_marks_mcp_failed_on_send_failure(monkeypatch) -> None:
    from deeptutor.observability.agent_monitor import has_mcp_failed, reset_trace_state

    async def fail_send(**kwargs) -> None:
        raise RuntimeError("send failed")

    reset_trace_state()
    monkeypatch.setattr(sale_chat.qywx, "send_lisa_message", fail_send)

    await sale_chat._push_sentences(
        corpid="corp-id",
        external_userid="external-id",
        prime_info={},
        text="第一句",
    )

    assert has_mcp_failed() is True


@pytest.mark.asyncio
async def test_push_reject_marks_mcp_failed_on_send_failure(monkeypatch) -> None:
    from deeptutor.observability.agent_monitor import has_mcp_failed, reset_trace_state

    async def fail_send(**kwargs) -> None:
        raise RuntimeError("send failed")

    reset_trace_state()
    monkeypatch.setattr(sale_chat.qywx, "send_lisa_message", fail_send)

    await sale_chat._push_reject(
        corpid="corp-id",
        external_userid="external-id",
        prime_info={},
        reason="知识库无法回答",
        user_original="课程怎么购买",
    )

    assert has_mcp_failed() is True

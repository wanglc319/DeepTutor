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


def test_strip_tool_calls_removes_decorative_corner_brackets() -> None:
    cleaned = sale_chat._strip_tool_calls("可以先体验「学霸营直播课」，今晚有直播。")

    assert cleaned == "可以先体验学霸营直播课，今晚有直播。"


def test_build_system_prompt_forbids_decorative_corner_brackets(monkeypatch) -> None:
    monkeypatch.setattr(
        "deeptutor.services.partners.workspace.read_soul",
        lambda partner_id: "你是 Lisa。",
    )

    prompt = sale_chat._build_system_prompt(
        "【用户画像】\n- 画像核心: child_name=悦悦; grade=四年级"
    )

    required_rules = ("不要使用「」", "不得重复询问")
    assert all(rule in prompt for rule in required_rules)


@pytest.mark.asyncio
async def test_llm_summarize_chunk_uses_strict_fact_prompt(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class CapturingLLM:
        async def complete(self, *, prompt: str, system_prompt: str) -> str:
            captured["prompt"] = prompt
            captured["system_prompt"] = system_prompt
            return (
                "SUMMARY: 客户表示孩子目前正在学习 RAZ。\n"
                "KEY_POINTS:\n"
                "- 客户的孩子目前正在学习 RAZ。"
            )

    monkeypatch.setattr(
        "deeptutor.services.llm.get_llm_client",
        lambda: CapturingLLM(),
    )

    summary, key_points = await sale_chat._llm_summarize_chunk(
        [
            {"sender": "assistant", "content": "孩子目前在学什么？"},
            {"sender": "customer", "content": "正在学 RAZ。"},
        ]
    )

    required_rules = (
        "AI 销售对话记忆压缩工具",
        "不负责生成画像字段",
        "不负责销售判断",
        "通常输出 1—12 条",
        "图片 OCR 文字是“图片内容”",
        "正在使用／正在学习",
        "不得把 Lisa 的提问、复述、推荐或假设直接作为客户事实",
        "SUMMARY: <一段客观摘要>",
        "KEY_POINTS:",
    )
    assert all(rule in captured["system_prompt"] for rule in required_rules)
    assert "[AI] 孩子目前在学什么？" in captured["prompt"]
    assert "[客户] 正在学 RAZ。" in captured["prompt"]
    assert summary == "客户表示孩子目前正在学习 RAZ。"
    assert key_points == ["客户的孩子目前正在学习 RAZ。"]


def test_build_kb_query_keeps_user_text_and_image_description() -> None:
    query = sale_chat._build_kb_query("这门课多少钱", "图片中是英语课程介绍")

    assert query == "这门课多少钱\n[客户图片内容识别] 图片中是英语课程介绍"


def test_build_kb_query_does_not_repeat_existing_image_description() -> None:
    text = "这门课多少钱\n[客户图片内容识别] 图片中是英语课程介绍"

    assert sale_chat._build_kb_query(text, "图片中是英语课程介绍") == text


def test_normalize_kb_score_rejects_non_finite_and_boolean_values() -> None:
    assert sale_chat._normalize_kb_score(float("nan")) == 0.0
    assert sale_chat._normalize_kb_score(float("inf")) == 0.0
    assert sale_chat._normalize_kb_score(True) == 0.0
    assert sale_chat._normalize_kb_score("0.72") == pytest.approx(0.72)


def test_merge_kb_snippets_removes_duplicate_content() -> None:
    context = sale_chat._merge_kb_snippets(
        [
            ("faq", "课程有效期是一年。"),
            ("policy", " 课程有效期是一年。 "),
            ("price", "课程价格以当前报价为准。"),
        ]
    )

    assert context.count("课程有效期是一年。") == 1
    assert "【faq】课程有效期是一年。" in context
    assert "【price】课程价格以当前报价为准。" in context


def test_merge_kb_snippets_preserves_multiline_content() -> None:
    content = "价格：\n- 标准版：1000 元\n- 进阶版：2000 元"

    context = sale_chat._merge_kb_snippets([("price", content)])

    assert f"【price】{content}" in context


@pytest.mark.asyncio
async def test_kb_can_answer_fails_closed_when_judge_errors(monkeypatch) -> None:
    from deeptutor.services import llm

    class FailingLLM:
        async def complete(self, prompt: str) -> str:
            raise RuntimeError("judge failed")

    monkeypatch.setattr(llm, "get_llm_client", lambda: FailingLLM())

    assert await sale_chat._kb_can_answer("课程多少钱", "价格资料") is False


def test_kb_guardrail_decision_exposes_each_branch() -> None:
    assert sale_chat._kb_guardrail_decision(False, "", 0.0, 0.55) == "skip"
    assert sale_chat._kb_guardrail_decision(True, "", 0.9, 0.55) == "transfer"
    assert sale_chat._kb_guardrail_decision(True, "资料", 0.54, 0.55) == "transfer"
    assert sale_chat._kb_guardrail_decision(True, "资料", 0.55, 0.55) == "judge"
    assert sale_chat._kb_guardrail_decision(True, "资料", 0.65, 0.55) == "pass"


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

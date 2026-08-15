from __future__ import annotations

from deeptutor.observability.agent_monitor import (
    BadCase,
    detect_bad_case,
    redact_pii,
    should_sample,
)


def test_redact_pii_masks_customer_identity_and_phone() -> None:
    payload = {
        "customerName": "超人不会流眼泪",
        "externalUserid": "wm5oClDAAApbXPEi1o0HjaS6ScAbpBtQ",
        "originalUserId": "wm5oClDAAApbXPEi1o0HjaS6ScAbpBtQ",
        "mobile": "13812345678",
        "content": "联系我 13812345678",
        "nested": {"qywxUserid": "WenHui", "answer": "正常回复"},
    }

    result = redact_pii(payload)

    assert result["customerName"].startswith("用户#")
    assert result["externalUserid"] == "wm5o***pBtQ"
    assert result["originalUserId"] == "wm5o***pBtQ"
    assert result["mobile"] == "138****5678"
    assert result["content"] == "联系我 138****5678"
    assert result["nested"]["qywxUserid"] == "Wen***Hui"
    assert result["nested"]["answer"] == "正常回复"


def test_should_sample_always_keeps_bad_case() -> None:
    assert should_sample(sample_rate=0.0, is_bad_case=True, random_value=1.0) is True
    assert should_sample(sample_rate=0.0, is_bad_case=False, random_value=1.0) is False
    assert should_sample(sample_rate=0.5, is_bad_case=False, random_value=0.49) is True
    assert should_sample(sample_rate=0.5, is_bad_case=False, random_value=0.5) is False


def test_detect_bad_case_combines_system_and_semantic_rules() -> None:
    result = detect_bad_case(
        reply='我不太理解，请您再说一遍。 {"tool_call":"reply"}',
        mcp_failed=True,
        llm_empty=False,
        transferred=False,
        explicit_refusal=False,
    )

    assert isinstance(result, BadCase)
    assert result.is_bad is True
    assert set(result.reasons) == {"mcp_failed", "clarification_fallback", "tool_call_leak"}


def test_detect_bad_case_marks_empty_reply_transfer_and_refusal() -> None:
    result = detect_bad_case(
        reply="",
        mcp_failed=False,
        llm_empty=True,
        transferred=True,
        explicit_refusal=True,
    )

    assert result.is_bad is True
    assert set(result.reasons) == {"llm_empty", "transferred", "explicit_refusal"}


def test_detect_bad_case_marks_overlong_low_information_reply() -> None:
    result = detect_bad_case(
        reply="好的好的，" * 45,
        mcp_failed=False,
        llm_empty=False,
        transferred=False,
        explicit_refusal=False,
    )

    assert result.is_bad is True
    assert "overlong_low_information" in result.reasons


def test_detect_bad_case_ignores_normal_reply() -> None:
    result = detect_bad_case(
        reply="家长，明晚有一场自然拼读直播课，我把入口发您。",
        mcp_failed=False,
        llm_empty=False,
        transferred=False,
        explicit_refusal=False,
    )

    assert result.is_bad is False
    assert result.reasons == ()

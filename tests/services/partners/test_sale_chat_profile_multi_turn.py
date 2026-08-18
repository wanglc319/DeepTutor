from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from deeptutor.sales.schemas import CustomerProfile
from deeptutor.services.partners import sale_chat
from deeptutor.services.shirley import profile as shirley_profile


@pytest.mark.asyncio
async def test_sale_chat_multi_turn_accumulates_profile_fields(monkeypatch) -> None:
    first_pain = "旧痛点甲" + "甲" * 20
    second_pain = "旧痛点乙" + "乙" * 20
    latest_pain = "最新痛点丙" + "丙" * 55
    extracts = [
        {
            "owned_products": "RAZ、牛津树",
            "pain_points": first_pain,
        },
        {
            "owned_products": "牛津树、海尼曼",
            "pain_points": second_pain,
        },
        {
            "owned_products": "RAZ、点读笔",
            "pain_points": latest_pain,
        },
    ]
    state: dict[str, Any] = {
        "profile": None,
        "saved": [],
        "extract_index": 0,
    }

    class ProfileLLM:
        async def complete(self, prompt: str) -> str:
            current = extracts[state["extract_index"]]
            state["extract_index"] += 1
            payload = {
                key: current.get(key, "")
                for key in shirley_profile._PROFILE_KEYWORDS
            }
            return json.dumps(payload, ensure_ascii=False)

    async def safe_call_tool(
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        assert tool_name == "get_user_profile"
        return copy.deepcopy(state["profile"])

    async def call_tool(
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        assert tool_name == "save_user_profile_analysis"
        saved = copy.deepcopy(arguments)
        state["saved"].append(saved)
        state["profile"] = {
            "aiAnalysis": {
                "attributes": copy.deepcopy(saved["attributes"]),
            }
        }
        return {"ok": True}

    async def no_pg(external_userid: str) -> None:
        return None

    async def no_history(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def no_kb(*args: Any, **kwargs: Any) -> tuple[str, float]:
        return "", 0.0

    async def sales_result(**kwargs: Any) -> tuple[CustomerProfile, None]:
        customer = CustomerProfile(intent_temperature="warm")
        return customer, None

    async def no_tags(*args: Any, **kwargs: Any) -> list[str]:
        return []

    async def no_tag_push(*args: Any, **kwargs: Any) -> None:
        return None

    async def reply(*args: Any, **kwargs: Any) -> str:
        return "收到，我已经记录。"

    monkeypatch.setattr(shirley_profile.client, "safe_call_tool", safe_call_tool)
    monkeypatch.setattr(shirley_profile.client, "call_tool", call_tool)
    monkeypatch.setattr(
        "deeptutor.services.llm.get_llm_client",
        lambda: ProfileLLM(),
    )
    monkeypatch.setattr(sale_chat, "_pg_ensure_customer_conv", no_pg)
    monkeypatch.setattr(sale_chat, "_fetch_history", no_history)
    monkeypatch.setattr(sale_chat, "_fetch_kb_context", no_kb)
    monkeypatch.setattr(sale_chat, "_stream_llm_and_push", reply)
    monkeypatch.setattr(
        "deeptutor.sales.service.process_customer_message",
        sales_result,
    )
    monkeypatch.setattr(
        "deeptutor.sales.qywx_tags.tag_ids_for_profile",
        no_tags,
    )
    monkeypatch.setattr(
        "deeptutor.sales.qywx_tags.apply_tags_to_customer",
        no_tag_push,
    )

    turns = [
        "孩子目前正在使用RAZ和牛津树，主要问题是不敢开口。",
        "后来又开始学习海尼曼，牛津树也还在用，现在背了忘。",
        "RAZ仍在使用，还买了点读笔；最近发音不准的问题更明显。",
    ]
    expected_products = [
        "RAZ、牛津树",
        "RAZ、牛津树、海尼曼",
        "RAZ、牛津树、海尼曼、点读笔",
    ]

    for index, text in enumerate(turns):
        await sale_chat._process_session_core(
            session_id="profile-multi-turn",
            messages=[{"content": text, "msgType": 2, "reqType": "chat"}],
            corpid="corp-test",
            external_userid="external-test",
            prime_info={
                "corpid": "corp-test",
                "originalUserId": "external-test",
                "isProd": False,
            },
            trace_outcome=sale_chat._TraceOutcome(),
        )

        attributes = state["saved"][-1]["attributes"]
        summaries = {item["key"]: item["summary"] for item in attributes}
        assert summaries["owned_products"] == expected_products[index]
        assert len(attributes) == 14
        assert [item["key"] for item in attributes] == shirley_profile.ALL_PROFILE_KEYS

    final_attributes = {
        item["key"]: item["summary"]
        for item in state["saved"][-1]["attributes"]
    }
    final_pain = final_attributes["pain_points"]
    assert first_pain not in final_pain
    assert second_pain in final_pain
    assert final_pain.endswith(latest_pain)
    assert len(final_pain) <= 100
    assert state["extract_index"] == 3
    assert len(state["saved"]) == 3

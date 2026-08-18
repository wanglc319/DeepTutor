from __future__ import annotations

import pytest

from deeptutor.services.shirley import profile


def _base_attr(key: str, value: str) -> dict[str, object]:
    return {
        "key": key,
        "values": [value],
        "summary": value,
        "confidence": 0.6,
        "evidence": [],
    }


@pytest.mark.parametrize(
    ("key", "limit"),
    [("owned_products", 300), ("pain_points", 100)],
)
def test_incremental_profile_fields_append_to_existing_values(
    key: str,
    limit: int,
) -> None:
    result = profile.merge_into_attributes(
        [_base_attr(key, "旧信息一、旧信息二")],
        {key: "新信息、旧信息二"},
    )[0]

    assert profile._ATTR_LENGTH_LIMITS[key] == limit
    assert result["values"] == ["旧信息一、旧信息二、新信息"]
    assert result["summary"] == "旧信息一、旧信息二、新信息"


def test_incremental_profile_fields_ignore_empty_profile_placeholder() -> None:
    result = profile.merge_into_attributes(
        [_base_attr("pain_points", "未提取到")],
        {"pain_points": "背单词容易忘"},
    )[0]

    assert result["summary"] == "背单词容易忘"


@pytest.mark.parametrize(
    ("key", "limit"),
    [("owned_products", 300), ("pain_points", 100)],
)
def test_incremental_profile_fields_drop_oldest_items_when_over_limit(
    key: str,
    limit: int,
) -> None:
    old_items = [f"旧{i:03d}" for i in range(100)]
    new_items = ["最新一", "最新二"]

    result = profile.merge_into_attributes(
        [_base_attr(key, "、".join(old_items))],
        {key: "、".join(new_items)},
    )[0]
    summary = result["summary"]

    assert len(summary) <= limit
    assert summary.endswith("旧099、最新一、最新二")
    assert "旧000" not in summary
    assert result["values"] == [summary]

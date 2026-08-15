from __future__ import annotations

import asyncio

from deeptutor.services.shirley import live
from deeptutor.services.shirley.resolver import ResolvedIDs


def test_list_weekly_lives_accepts_and_uses_fallback_identity_fields(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_resolve_all(
        corpid: str,
        external_userid: str,
        **kwargs: object,
    ) -> ResolvedIDs:
        return ResolvedIDs(
            corpid=corpid,
            external_userid=external_userid,
            camp_id=19913,
        )

    async def fake_call_tool_with_retry(
        tool_name: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        captured.update(params)
        return {"lives": []}

    monkeypatch.setattr(live, "resolve_all", fake_resolve_all)
    monkeypatch.setattr(live.client, "call_tool_with_retry", fake_call_tool_with_retry)

    result = asyncio.run(
        live.list_weekly_lives(
            "corp-id",
            "external-id",
            qywx_userid_fallback="fallback-user",
            third_sale_uuid_fallback="sale-id",
            third_user_id_fallback=123,
            vid_fallback=456,
        )
    )

    assert result == []
    assert captured["qywxUserid"] == "fallback-user"

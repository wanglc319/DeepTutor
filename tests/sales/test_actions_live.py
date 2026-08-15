from __future__ import annotations

from deeptutor.sales import actions
from deeptutor.services.shirley import live


def test_live_url_forwards_fallback_identity_fields(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_list_weekly_lives(
        corpid: str,
        external_userid: str,
        **kwargs: object,
    ) -> list[live.LiveSession]:
        captured.update(kwargs)
        return [
            live.LiveSession(
                name="训练营直播课",
                play_url="https://example.com/live",
                start_time="2099-08-21 19:00:00",
            )
        ]

    monkeypatch.setattr(live, "list_weekly_lives", fake_list_weekly_lives)

    result = __import__("asyncio").run(
        actions._get_live_url(
            corpid="corp-id",
            external_userid="external-id",
            qywx_userid="resolved-user",
            qywx_userid_fallback="fallback-user",
            third_sale_uuid_fallback="sale-id",
            third_user_id_fallback=123,
            vid_fallback=456,
        )
    )

    assert result is not None
    assert captured == {
        "qywx_userid": "resolved-user",
        "qywx_userid_fallback": "fallback-user",
        "third_sale_uuid_fallback": "sale-id",
        "third_user_id_fallback": 123,
        "vid_fallback": 456,
    }

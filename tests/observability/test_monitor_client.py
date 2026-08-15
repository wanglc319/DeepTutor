from __future__ import annotations

from deeptutor.observability import agent_monitor
from deeptutor.observability.agent_monitor import (
    flush_monitor,
    monitor_enabled,
    observation,
    reset_monitor,
)


def test_monitor_is_disabled_without_credentials(monkeypatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    reset_monitor()

    assert monitor_enabled() is False
    with observation("test-span", input={"content": "你好"}) as span:
        assert span is None


def test_monitor_requires_both_credentials(monkeypatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    reset_monitor()

    assert monitor_enabled() is False


def test_flush_monitor_flushes_initialized_client(monkeypatch) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.flush_count = 0

        def flush(self) -> None:
            self.flush_count += 1

    client = FakeClient()
    monkeypatch.setattr(agent_monitor, "_langfuse_client", client)
    monkeypatch.setattr(agent_monitor, "_langfuse_initialized", True)

    flush_monitor()

    assert client.flush_count == 1


def test_flush_monitor_does_not_raise_when_client_flush_fails(monkeypatch) -> None:
    class FakeClient:
        def flush(self) -> None:
            raise RuntimeError("network unavailable")

    monkeypatch.setattr(agent_monitor, "_langfuse_client", FakeClient())
    monkeypatch.setattr(agent_monitor, "_langfuse_initialized", True)

    flush_monitor()

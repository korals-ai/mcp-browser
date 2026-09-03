"""Agent → human take-over (MFA handoff): pause + push a takeover frame."""

from __future__ import annotations

from typing import Any

from src import agent_ops
from tests.conftest import FakeDriver, make_manager


async def test_request_takeover_pauses_and_broadcasts() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")

    sent: list[dict[str, Any]] = []

    async def sink(frame: dict[str, Any]) -> None:
        sent.append(frame)

    session.add_viewer_sink(sink)

    result = await agent_ops.request_takeover(manager, "c1", "Enter the 2FA code")

    assert result["status"] == "handed_to_human"
    assert session.agent_paused is True
    types = [f["type"] for f in sent]
    assert "browser_takeover_request" in types
    req = next(f for f in sent if f["type"] == "browser_takeover_request")
    assert req["reason"] == "Enter the 2FA code"


async def test_takeover_survives_a_dead_viewer_sink() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")

    async def bad_sink(_frame: dict[str, Any]) -> None:
        raise RuntimeError("socket gone")

    session.add_viewer_sink(bad_sink)
    # Must not raise even though the only viewer errors.
    result = await agent_ops.request_takeover(manager, "c1", "help")
    assert result["status"] == "handed_to_human"


async def test_paused_agent_can_still_request_takeover() -> None:
    # Unlike click/type, request_takeover must work while already paused.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    result = await agent_ops.request_takeover(manager, "c1", "captcha")
    assert result["status"] == "handed_to_human"

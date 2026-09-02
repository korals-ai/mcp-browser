"""Agent-plane ops: actions reach the driver, redaction holds, pause is honoured."""

from __future__ import annotations

import pytest

from src import agent_ops
from src.snapshot import REDACTED, Element
from tests.conftest import FakeDriver, make_manager


async def test_open_navigates_and_returns_nav_state() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    nav = await agent_ops.open_url(manager, "c1", "https://example.com/form")
    assert driver.opened == ["https://example.com/form"]
    assert nav["url"] == "https://example.com/form"


async def test_open_broadcasts_nav_to_viewers() -> None:
    # The viewer's address bar must update on navigation, not just on connect.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    sent: list[dict[str, object]] = []

    async def sink(frame: dict[str, object]) -> None:
        sent.append(frame)

    session.add_viewer_sink(sink)
    await agent_ops.open_url(manager, "c1", "https://example.com/next")
    navs = [f for f in sent if f["type"] == "browser_nav"]
    assert navs and navs[-1]["url"] == "https://example.com/next"


async def test_snapshot_is_redacted_at_the_op_boundary() -> None:
    snap: list[Element] = [{"ref": "e0", "tag": "input", "type": "password", "value": "pw"}]
    driver = FakeDriver(snapshot=snap)
    manager, _ = make_manager(driver)
    out = await agent_ops.snapshot(manager, "c1")
    assert out[0]["value"] == REDACTED
    assert "pw" not in str(out)


async def test_click_and_type_reach_driver() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    await agent_ops.click(manager, "c1", "e3")
    await agent_ops.type_text(manager, "c1", "e4", "hello")
    assert driver.clicks == ["e3"]
    assert driver.typed == [("e4", "hello")]


async def test_scroll_reaches_driver() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    await agent_ops.scroll(manager, "c1", "down", 600)
    await agent_ops.scroll(manager, "c1", "up", 300)
    assert driver.scrolls == [("down", 600), ("up", 300)]


async def test_paused_session_blocks_click_and_type() -> None:
    # Mid-task actions still respect a human pause (so a pause can interrupt the
    # agent) — but see test_open_clears_pause: a fresh navigation resumes.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    session.agent_paused = True  # human took over
    with pytest.raises(agent_ops.AgentPaused):
        await agent_ops.click(manager, "c1", "e0")
    with pytest.raises(agent_ops.AgentPaused):
        await agent_ops.type_text(manager, "c1", "e0", "x")
    assert driver.clicks == []


async def test_open_clears_pause_and_never_locks_out() -> None:
    # The lock-out fix: browser_open on a paused session (e.g. a stale takeover)
    # resumes the agent instead of raising, and tells viewers the agent is back.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    sent: list[dict[str, object]] = []

    async def sink(frame: dict[str, object]) -> None:
        sent.append(frame)

    session.add_viewer_sink(sink)
    nav = await agent_ops.open_url(manager, "c1", "https://example.com")
    assert nav["url"] == "https://example.com"
    assert driver.opened == ["https://example.com"]
    assert session.agent_paused is False  # resumed
    states = [f for f in sent if f["type"] == "browser_agent_state"]
    assert states and states[-1]["state"] == "idle"


async def test_agent_action_records_last_actor() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    await agent_ops.click(manager, "c1", "e0")
    assert manager.get("c1").last_actor == "agent"

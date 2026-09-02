"""Reading + navigation + extra-action tools reach the driver, and observation
works even while the human has paused the agent."""

from __future__ import annotations

import pytest

from src import agent_ops
from tests.conftest import FakeDriver, make_manager

# --- reading / understanding (allowed while paused) -------------------------


async def test_read_find_inspect_table_reach_driver() -> None:
    driver = FakeDriver()
    driver.page_text = "hello WORLD hello"
    manager, _ = make_manager(driver)

    assert await agent_ops.read(manager, "c1") == "hello WORLD hello"
    found = await agent_ops.find_text(manager, "c1", "hello")
    assert found["count"] == 2
    ins = await agent_ops.inspect(manager, "c1", "e3")
    assert ins["ref"] == "e3"
    assert await agent_ops.get_table(manager, "c1") == [[["a", "b"], ["1", "2"]]]
    png = await agent_ops.screenshot(manager, "c1")
    assert png.startswith(b"\x89PNG")


async def test_observation_works_while_paused() -> None:
    # Reading is passive — the agent can still observe while the human drives.
    driver = FakeDriver()
    driver.page_text = "still readable"
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    assert await agent_ops.read(manager, "c1") == "still readable"
    await agent_ops.screenshot(manager, "c1")  # no AgentPaused


async def test_wait_for_passes_args() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    ok = await agent_ops.wait_for(manager, "c1", text="Done", timeout_ms=1234)
    assert ok is True
    assert driver.waited == [{"text": "Done", "selector": None, "timeout_ms": 1234}]


# --- history / navigation (pause-gated) -------------------------------------


async def test_history_nav_reaches_driver() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    await agent_ops.go_back(manager, "c1")
    await agent_ops.go_forward(manager, "c1")
    await agent_ops.reload(manager, "c1")
    assert driver.nav_ops == ["back", "forward", "reload"]


async def test_history_nav_blocked_when_paused() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    with pytest.raises(agent_ops.AgentPaused):
        await agent_ops.go_back(manager, "c1")
    assert driver.nav_ops == []


# --- extra actions (pause-gated) --------------------------------------------


async def test_key_select_upload_reach_driver() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    await agent_ops.press_key(manager, "c1", "Enter")
    await agent_ops.select_option(manager, "c1", "e2", "UK")
    await agent_ops.upload_file(manager, "c1", "e5", "/home/agent/proposal.pdf")
    assert driver.keys == ["Enter"]
    assert driver.selects == [("e2", "UK")]
    assert driver.uploads == [("e5", "/home/agent/proposal.pdf")]


async def test_download_reaches_driver_and_returns_result() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    result = await agent_ops.download(manager, "c1", "e7", "/home/agent/downloads/x.pdf")
    assert driver.downloads == [("e7", "/home/agent/downloads/x.pdf")]
    assert result["filename"] == "report.pdf" and result["saved"] is True


async def test_actions_blocked_when_paused() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    for call in (
        agent_ops.press_key(manager, "c1", "Enter"),
        agent_ops.select_option(manager, "c1", "e2", "x"),
        agent_ops.upload_file(manager, "c1", "e5", "/home/agent/x"),
    ):
        with pytest.raises(agent_ops.AgentPaused):
            await call
    assert driver.keys == [] and driver.selects == [] and driver.uploads == []

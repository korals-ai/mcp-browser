"""Interaction extras: hover/drag (pause-gated actions), scroll_to/get_options/
get_links (viewing), and eval_js (pause-gated, error-safe)."""

from __future__ import annotations

import pytest

from src import agent_ops
from tests.conftest import FakeDriver, make_manager


async def test_hover_and_drag_reach_driver_and_block_when_paused() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    await agent_ops.hover(manager, "c1", "e2")
    await agent_ops.drag(manager, "c1", "e2", "e5")
    assert driver.hovers == ["e2"]
    assert driver.drags == [("e2", "e5")]
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    with pytest.raises(agent_ops.AgentPaused):
        await agent_ops.hover(manager, "c1", "e0")
    with pytest.raises(agent_ops.AgentPaused):
        await agent_ops.drag(manager, "c1", "e0", "e1")


async def test_scroll_to_found_not_found_and_pause_gated() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    driver.scroll_to_result = True
    assert await agent_ops.scroll_to(manager, "c1", "e7") == {"status": "scrolled", "ref": "e7"}
    driver.scroll_to_result = False
    assert await agent_ops.scroll_to(manager, "c1", "e9") == {"status": "not_found", "ref": "e9"}
    assert driver.scrolled_to == ["e7", "e9"]
    # scroll_to MOVES the shared viewport, so like scroll()/find_text() it's
    # pause-gated — a paused agent must not scroll the view out from under a human.
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    with pytest.raises(agent_ops.AgentPaused):
        await agent_ops.scroll_to(manager, "c1", "e0")


async def test_get_options_and_links() -> None:
    driver = FakeDriver()
    driver.options = [
        {"value": "ca", "label": "Canada", "selected": True},
        {"value": "us", "label": "United States", "selected": False},
    ]
    driver.links = [{"text": "Apply", "href": "https://portal.example/apply"}]
    manager, _ = make_manager(driver)
    opts = await agent_ops.get_options(manager, "c1", "e4")
    assert [o["value"] for o in opts] == ["ca", "us"] and opts[0]["selected"] is True
    assert await agent_ops.get_options(manager, "c1", "gone") == opts  # fake returns same list
    # get_links works while paused (pure read)
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    links = await agent_ops.get_links(manager, "c1")
    assert links[0]["href"] == "https://portal.example/apply"


async def test_get_options_empty_for_non_select() -> None:
    driver = FakeDriver()  # options defaults to []
    manager, _ = make_manager(driver)
    assert await agent_ops.get_options(manager, "c1", "e0") == []


async def test_eval_returns_result_error_and_blocks_when_paused() -> None:
    driver = FakeDriver()
    driver.eval_result = {"result": "42"}
    manager, _ = make_manager(driver)
    assert await agent_ops.eval_js(manager, "c1", "6*7") == {"result": "42"}
    assert driver.evals == ["6*7"]
    driver.eval_result = {"error": "ReferenceError: foo is not defined"}
    out = await agent_ops.eval_js(manager, "c1", "foo()")
    assert "error" in out and "foo" in out["error"]
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    with pytest.raises(agent_ops.AgentPaused):
        await agent_ops.eval_js(manager, "c1", "1+1")


async def test_playwright_scroll_to_missing_ref_returns_false() -> None:
    from src.browser_driver import PlaywrightDriver

    class _Loc:
        async def scroll_into_view_if_needed(self, timeout: int = 0) -> None:  # noqa: ASYNC109
            raise RuntimeError("Timeout: no element matches selector")

    class _Frame:
        def locator(self, _sel: str) -> _Loc:
            return _Loc()

    d = PlaywrightDriver.__new__(PlaywrightDriver)
    d._active_frame = lambda: _Frame()  # type: ignore[method-assign]
    assert await d.scroll_to("gone") is False

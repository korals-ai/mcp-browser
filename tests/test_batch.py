"""browser_batch: several calls behind one round trip, stopping at the first
error, never nesting."""

from __future__ import annotations

from typing import Any

from src import agent_ops


def _dispatch(log: list[str]) -> dict[str, Any]:
    async def read_page(tabId: int) -> str:
        log.append(f"read_page {tabId}")
        return "- tree"

    async def computer(tabId: int, action: str, ref: str | None = None) -> str:
        log.append(f"computer {action} {ref}")
        if action == "left_click" and ref == "boom":
            raise ValueError("could not click boom")
        return f"Clicked {ref}"

    return {"read_page": read_page, "computer": computer}


async def test_batch_runs_in_order_and_returns_every_result() -> None:
    log: list[str] = []
    out = await agent_ops.run_batch(
        [
            {"name": "computer", "input": {"tabId": 1, "action": "left_click", "ref": "e5"}},
            {"name": "mcp__workspace-tool-browser__read_page", "input": {"tabId": 1}},
        ],
        _dispatch(log),
    )
    assert log == ["computer left_click e5", "read_page 1"]
    assert out == [
        {"name": "computer", "status": "ok", "result": "Clicked e5"},
        {"name": "read_page", "status": "ok", "result": "- tree"},
    ]


async def test_batch_stops_at_the_first_error_and_reports_it() -> None:
    log: list[str] = []
    out = await agent_ops.run_batch(
        [
            {"name": "computer", "input": {"tabId": 1, "action": "left_click", "ref": "boom"}},
            {"name": "read_page", "input": {"tabId": 1}},
        ],
        _dispatch(log),
    )
    assert log == ["computer left_click boom"]
    assert out[-1] == {
        "name": "computer",
        "status": "error",
        "error": "ValueError: could not click boom",
    }
    assert len(out) == 1


async def test_batch_refuses_nesting_unknown_tools_and_bad_arguments() -> None:
    log: list[str] = []
    nested = await agent_ops.run_batch(
        [{"name": "browser_batch", "input": {"actions": []}}], _dispatch(log)
    )
    assert nested[0]["error"] == "browser_batch cannot be nested"
    unknown = await agent_ops.run_batch([{"name": "no_such_tool", "input": {}}], _dispatch(log))
    assert "unknown tool 'no_such_tool'" in unknown[0]["error"]
    bad = await agent_ops.run_batch([{"name": "read_page", "input": {"tab": 1}}], _dispatch(log))
    assert bad[0]["error"].startswith("bad arguments:")
    malformed = await agent_ops.run_batch(["read_page"], _dispatch(log))  # type: ignore[list-item]
    assert "must be {name, input}" in malformed[0]["error"]
    assert log == []


async def test_batch_runs_after_item_between_items_never_after_the_last() -> None:
    """The between-items hook (the navigation wait) runs after every item but
    the last — the last item's result goes straight back to the model — and
    sees the item's bare name and input."""
    log: list[str] = []

    async def after(name: str, inp: dict[str, Any]) -> None:
        log.append(f"after {name} {inp.get('action', '')}".rstrip())

    out = await agent_ops.run_batch(
        [
            {"name": "computer", "input": {"tabId": 1, "action": "left_click", "ref": "e5"}},
            {"name": "mcp__workspace-tool-browser__read_page", "input": {"tabId": 1}},
            {"name": "read_page", "input": {"tabId": 1}},
        ],
        _dispatch(log),
        after_item=after,
    )
    assert [r["status"] for r in out] == ["ok", "ok", "ok"]
    assert log == [
        "computer left_click e5",
        "after computer left_click",
        "read_page 1",
        "after read_page",
        "read_page 1",
    ]


async def test_batch_does_not_run_after_item_past_an_error() -> None:
    log: list[str] = []

    async def after(name: str, inp: dict[str, Any]) -> None:
        log.append("after")

    await agent_ops.run_batch(
        [
            {"name": "computer", "input": {"tabId": 1, "action": "left_click", "ref": "boom"}},
            {"name": "read_page", "input": {"tabId": 1}},
        ],
        _dispatch(log),
        after_item=after,
    )
    assert log == ["computer left_click boom"]


def test_item_may_navigate_is_keyed_on_the_computer_action() -> None:
    """Only an item that can start a navigation earns the between-items wait:
    an acting `computer` action, a form/script/upload — never a read or a
    view-only action, and not navigate/wait_for/login, which wait themselves."""
    assert agent_ops.item_may_navigate("computer", {"action": "left_click"})
    assert agent_ops.item_may_navigate("computer", {"action": "key"})
    assert agent_ops.item_may_navigate("form_input", {"ref": "e1", "value": "x"})
    assert agent_ops.item_may_navigate("javascript_tool", {"text": "1"})
    for action in ("screenshot", "zoom", "wait", "scroll", "scroll_to", "hover"):
        assert not agent_ops.item_may_navigate("computer", {"action": action})
    for name in ("read_page", "get_page_text", "find", "navigate", "wait_for", "login"):
        assert not agent_ops.item_may_navigate(name, {})

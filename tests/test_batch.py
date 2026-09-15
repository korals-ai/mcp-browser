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
    unknown = await agent_ops.run_batch([{"name": "browser_click", "input": {}}], _dispatch(log))
    assert "unknown tool 'browser_click'" in unknown[0]["error"]
    bad = await agent_ops.run_batch([{"name": "read_page", "input": {"tab": 1}}], _dispatch(log))
    assert bad[0]["error"].startswith("bad arguments:")
    malformed = await agent_ops.run_batch(["read_page"], _dispatch(log))  # type: ignore[list-item]
    assert "must be {name, input}" in malformed[0]["error"]
    assert log == []

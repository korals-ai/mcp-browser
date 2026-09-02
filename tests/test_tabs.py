"""Multi-tab: agent + human open/switch/close, viewers get the tab list."""

from __future__ import annotations

from typing import Any

from src import agent_ops
from src.cobrowse_ws import CoBrowseConnection
from src.protocol import (
    BrowserCloseTab,
    BrowserNewTab,
    BrowserSwitchTab,
    parse_client_message,
)
from tests.conftest import FakeDriver, make_manager

# ---- driver / agent_ops ----------------------------------------------------


async def test_open_new_tab_adds_and_activates() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    await agent_ops.open_url(manager, "c1", "https://a.com")
    await agent_ops.open_url(manager, "c1", "https://b.com", new_tab=True)
    tabs = await agent_ops.list_tabs(manager, "c1")
    assert len(tabs) == 2
    active = [t for t in tabs if t["active"]]
    assert len(active) == 1 and active[0]["url"] == "https://b.com"


async def test_switch_tab_changes_active() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    await agent_ops.open_url(manager, "c1", "https://a.com")  # t1
    await agent_ops.open_url(manager, "c1", "https://b.com", new_tab=True)  # t2 active
    res = await agent_ops.switch_tab(manager, "c1", "t1")
    assert res["status"] == "switched"
    tabs = await agent_ops.list_tabs(manager, "c1")
    assert next(t for t in tabs if t["active"])["id"] == "t1"


async def test_switch_unknown_tab_reports() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    res = await agent_ops.switch_tab(manager, "c1", "nope")
    assert res["status"] == "unknown_tab"


async def test_close_tab_and_never_zero() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    await agent_ops.open_url(manager, "c1", "https://a.com", new_tab=True)
    tabs = await agent_ops.list_tabs(manager, "c1")
    # close every tab; the driver must always keep at least one
    for t in list(tabs):
        await agent_ops.close_tab(manager, "c1", t["id"])
    remaining = await agent_ops.list_tabs(manager, "c1")
    assert len(remaining) >= 1


async def test_open_broadcasts_tab_list() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    sent: list[dict[str, Any]] = []

    async def sink(f: dict[str, Any]) -> None:
        sent.append(f)

    session.add_viewer_sink(sink)
    await agent_ops.open_url(manager, "c1", "https://a.com", new_tab=True)
    tab_frames = [f for f in sent if f["type"] == "browser_tabs"]
    assert tab_frames and any(t["url"] == "https://a.com" for t in tab_frames[-1]["tabs"])


# ---- cobrowse WS (human tab actions) ---------------------------------------


class _FakeWs:
    def __init__(self, inbound: list[dict[str, Any]]) -> None:
        self._inbound = list(inbound)
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)

    async def receive_json(self) -> dict[str, Any] | None:
        return self._inbound.pop(0) if self._inbound else None


async def test_viewer_gets_tabs_on_connect() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    ws = _FakeWs(inbound=[])
    await CoBrowseConnection(manager, "c1", ws).run()
    assert any(m["type"] == "browser_tabs" for m in ws.sent)


async def test_human_new_tab_then_switch_then_close() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    ws = _FakeWs(
        inbound=[
            {"type": "browser_new_tab"},
            {"type": "browser_switch_tab", "tab_id": "t1"},
            {"type": "browser_close_tab", "tab_id": "t1"},
        ]
    )
    await CoBrowseConnection(manager, "c1", ws).run()
    # each action re-broadcasts the tab list
    assert sum(1 for m in ws.sent if m["type"] == "browser_tabs") >= 3


# ---- protocol --------------------------------------------------------------


def test_parse_tab_frames() -> None:
    assert isinstance(parse_client_message({"type": "browser_new_tab"}), BrowserNewTab)
    assert parse_client_message({"type": "browser_switch_tab", "tab_id": "t2"}) == BrowserSwitchTab(
        "t2"
    )
    assert parse_client_message({"type": "browser_close_tab", "tab_id": "t2"}) == BrowserCloseTab(
        "t2"
    )


def test_parse_rejects_tab_frame_without_id() -> None:
    for bad in ({"type": "browser_switch_tab"}, {"type": "browser_close_tab", "tab_id": ""}):
        try:
            parse_client_message(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")

"""Iframe support: an active-frame model per tab, list/switch, and the driver's
detached-frame fallback."""

from __future__ import annotations

from src import agent_ops
from tests.conftest import FakeDriver, make_manager


async def test_active_frame_defaults_to_main() -> None:
    d = FakeDriver()
    manager, _ = make_manager(d)
    assert d.active_frame_idx() == 0
    await agent_ops.click(manager, "c1", "e1")
    assert d.clicks == ["e1"]  # acted on main frame, no error


async def test_list_frames_returns_index_name_url() -> None:
    d = FakeDriver()
    d._frames["t1"] = [
        {"index": 0, "name": "", "url": "https://portal.example/app"},
        {"index": 1, "name": "loginform", "url": "https://auth.example/embed"},
        {"index": 2, "name": "", "url": "https://viewer.example/pdf"},
    ]
    manager, _ = make_manager(d)
    frames = await agent_ops.list_frames(manager, "c1")
    assert frames[1] == {"index": 1, "name": "loginform", "url": "https://auth.example/embed"}
    assert [f["index"] for f in frames] == [0, 1, 2]


async def test_switch_frame_by_index_and_name_and_reset_and_unknown() -> None:
    d = FakeDriver()
    d._frames["t1"] = [
        {"index": 0, "name": "", "url": "about:blank"},
        {"index": 1, "name": "loginform", "url": "https://auth.example"},
    ]
    manager, _ = make_manager(d)
    assert (await agent_ops.switch_frame(manager, "c1", "1"))["status"] == "switched"
    assert d.active_frame_idx() == 1
    assert (await agent_ops.switch_frame(manager, "c1", "main"))["status"] == "reset"
    assert d.active_frame_idx() == 0
    assert (await agent_ops.switch_frame(manager, "c1", "loginform"))["status"] == "switched"
    assert d.active_frame_idx() == 1
    miss = await agent_ops.switch_frame(manager, "c1", "nope")
    assert miss["status"] == "unknown_frame"
    assert d.active_frame_idx() == 1  # unchanged on a miss


async def test_switch_tab_resets_active_frame() -> None:
    d = FakeDriver()
    await d.open("https://a.example", new_tab=True)  # t2 active
    d._frames["t2"] = [
        {"index": 0, "name": "", "url": "https://a.example"},
        {"index": 1, "name": "c", "url": "https://a.example/frame"},
    ]
    manager, _ = make_manager(d)
    await agent_ops.switch_frame(manager, "c1", "1")
    assert d.active_frame_idx() == 1
    await agent_ops.switch_tab(manager, "c1", "t1")
    assert d.active_frame_idx() == 0  # a tab switch reset the frame


async def test_frame_ops_work_while_paused() -> None:
    d = FakeDriver()
    manager, _ = make_manager(d)
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    assert isinstance(await agent_ops.list_frames(manager, "c1"), list)  # no raise
    assert (await agent_ops.switch_frame(manager, "c1", "main"))["status"] == "reset"


async def test_playwright_active_frame_falls_back_when_detached() -> None:
    from src.browser_driver import PlaywrightDriver, _Tab

    class _FakeFrame:
        def __init__(self, name: str, url: str, detached: bool = False) -> None:
            self._name, self._url, self._det = name, url, detached

        @property
        def name(self) -> str:
            return self._name

        @property
        def url(self) -> str:
            return self._url

        def is_detached(self) -> bool:
            return self._det

    class _FakePage:
        def __init__(self, main: object, children: list[object]) -> None:
            self.main_frame = main
            self.frames = [main, *children]

    drv = PlaywrightDriver()
    main = _FakeFrame("", "https://portal.example")
    child = _FakeFrame("loginform", "https://auth.example")
    page = _FakePage(main, [child])
    tab = _Tab("t1", page, cdp=None)
    drv._tabs = [tab]
    drv._active_id = "t1"
    tab.active_frame_key = drv._frame_key(child, page)
    assert drv._active_frame() is child  # pinned child resolves
    child._det = True  # frame detaches
    assert drv._active_frame() is main  # falls back to main
    assert tab.active_frame_key is None  # and clears the stale pin

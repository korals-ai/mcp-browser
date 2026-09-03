"""Native JS dialogs: safe-by-default dismiss, opt-in accept, last-dialog read,
and the per-page handler wiring on every tab."""

from __future__ import annotations

from typing import Any

import pytest

from src import agent_ops
from tests.conftest import FakeDriver, make_manager


async def test_set_dialog_mode_defaults_and_normalises() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    assert driver.dialog_mode == "dismiss"  # safe default
    assert await agent_ops.set_dialog_mode(manager, "c1", "accept") == {
        "status": "set",
        "mode": "accept",
    }
    assert driver.dialog_mode == "accept"
    # A garbage mode can never auto-accept a destructive confirm.
    assert await agent_ops.set_dialog_mode(manager, "c1", "Yes please") == {
        "status": "set",
        "mode": "dismiss",
    }
    assert driver.dialog_mode == "dismiss"


async def test_last_dialog_none_then_handled() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    assert await agent_ops.last_dialog(manager, "c1") == {"status": "none"}
    await agent_ops.set_dialog_mode(manager, "c1", "accept")
    await driver.fire_dialog("confirm", "Submit application?")
    out = await agent_ops.last_dialog(manager, "c1")
    assert out["status"] == "handled"
    assert out["type"] == "confirm"
    assert out["message"] == "Submit application?"
    assert out["action"] == "accept"


async def test_last_dialog_works_while_paused() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    await driver.fire_dialog("alert", "Saved")
    out = await agent_ops.last_dialog(manager, "c1")  # no AgentPaused
    assert out["status"] == "handled" and out["message"] == "Saved"


async def test_set_dialog_mode_blocked_when_paused() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    with pytest.raises(agent_ops.AgentPaused):
        await agent_ops.set_dialog_mode(manager, "c1", "accept")
    assert driver.dialog_mode == "dismiss"  # unchanged


async def test_playwright_installs_dialog_handler_on_every_tab() -> None:
    from src.browser_driver import PlaywrightDriver

    class _FakePage:
        def __init__(self) -> None:
            self.handlers: dict[str, Any] = {}
            self.url = "about:blank"

        def on(self, event: str, cb: Any) -> None:
            self.handlers[event] = cb

    class _FakeCtx:
        def __init__(self) -> None:
            self.pages: list[_FakePage] = [_FakePage()]

        async def new_page(self) -> _FakePage:
            p = _FakePage()
            self.pages.append(p)
            return p

        async def new_cdp_session(self, _page: Any) -> Any:
            return object()

    d = PlaywrightDriver()
    d._context = _FakeCtx()
    first = await d._adopt_first_tab()
    assert "dialog" in first.page.handlers  # first (adopted) tab covered
    new = await d._new_page_tab()
    assert "dialog" in new.page.handlers  # new tab covered


async def test_dialog_handler_honours_mode_records_and_swallows_race() -> None:
    from src.browser_driver import PlaywrightDriver

    class _FakeDialog:
        def __init__(self, dtype: str, message: str, raises: bool = False) -> None:
            self.type = dtype
            self.message = message
            self.default_value = ""
            self.accepted = False
            self.dismissed = False
            self._raises = raises

        async def accept(self, *_a: Any) -> None:
            if self._raises:
                raise RuntimeError("dialog already handled")
            self.accepted = True

        async def dismiss(self) -> None:
            if self._raises:
                raise RuntimeError("dialog already handled")
            self.dismissed = True

    class _FakePage:
        def __init__(self) -> None:
            self.cb: Any = None
            self.url = "https://portal/form"

        def on(self, event: str, cb: Any) -> None:
            if event == "dialog":
                self.cb = cb

    d = PlaywrightDriver()
    page = _FakePage()
    d._install_dialog_handler(page)
    # default dismiss
    dlg = _FakeDialog("confirm", "Delete record?")
    await page.cb(dlg)
    assert dlg.dismissed and not dlg.accepted
    assert (await d.last_dialog())["action"] == "dismiss"
    # flip to accept
    await d.set_dialog_mode("accept")
    dlg2 = _FakeDialog("confirm", "Accept cookies?")
    await page.cb(dlg2)
    assert dlg2.accepted
    last = await d.last_dialog()
    assert last["message"] == "Accept cookies?" and last["url"] == "https://portal/form"
    # a handle() race must NOT propagate (it'd crash the page's event task)
    await page.cb(_FakeDialog("beforeunload", "", raises=True))
    assert (await d.last_dialog())["type"] == "beforeunload"

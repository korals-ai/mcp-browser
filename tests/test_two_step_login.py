"""Two-step portal login: username screen first, password screen second.

These drive the REAL :meth:`PlaywrightDriver.fill_login` against a fake frame.
``FakeDriver`` in conftest stubs ``fill_login`` wholesale, so the portal-login
tests next door cannot see a bug inside it — the single-step regression here is
the one that would have caught the two-step gap.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.browser_driver import (
    _PASSWORD_SELECTOR,
    _USERNAME_SELECTOR,
    PlaywrightDriver,
)


class FakeElement:
    """A form field recording what was filled into and pressed on it."""

    def __init__(self, visible: bool = True) -> None:
        self.visible = visible
        self.filled: list[str] = []
        self.pressed: list[str] = []
        self.on_press: Any = None

    async def is_visible(self) -> bool:
        return self.visible

    async def fill(self, value: str) -> None:
        self.filled.append(value)

    async def press(self, key: str) -> None:
        self.pressed.append(key)
        if self.on_press is not None:
            self.on_press()


class FakeLocator:
    """Playwright's locator surface, as much of it as ``fill_login_at`` uses."""

    def __init__(self, element: FakeElement | None) -> None:
        self.element = element

    async def count(self) -> int:
        return 1 if self.element is not None else 0

    async def fill(self, value: str) -> None:
        assert self.element is not None
        await self.element.fill(value)

    async def press(self, key: str) -> None:
        assert self.element is not None
        await self.element.press(key)


class FakeFrame:
    """Models a login screen: which fields exist, and whether they're visible."""

    def __init__(
        self,
        username: FakeElement | None = None,
        password: FakeElement | None = None,
        by_ref: dict[str, FakeElement] | None = None,
    ) -> None:
        self.username = username
        self.password = password
        self.waited: list[str] = []
        # Elements addressable by ref (aria-ref=eN), for fill_login_at.
        self.by_ref = by_ref or {}

    def locator(self, selector: str) -> FakeLocator:
        prefix = "aria-ref="
        assert selector.startswith(prefix), selector
        return FakeLocator(self.by_ref.get(selector[len(prefix) :]))

    def _match(self, selector: str) -> FakeElement | None:
        if selector == _PASSWORD_SELECTOR:
            return self.password
        if selector == _USERNAME_SELECTOR:
            return self.username
        raise AssertionError(f"unexpected selector: {selector!r}")

    async def query_selector(self, selector: str) -> FakeElement | None:
        return self._match(selector)

    # ASYNC109 is silenced on `timeout` below: this mirrors Playwright's real
    # signature, which the code under test calls with `timeout=`. A fake that
    # renamed the parameter wouldn't be a fake.
    async def wait_for_selector(
        self,
        selector: str,
        *,
        state: str,
        timeout: float,  # noqa: ASYNC109
    ) -> FakeElement | None:
        self.waited.append(selector)
        el = self._match(selector)
        if el is None or not el.visible:
            raise TimeoutError("no such visible element")
        return el


def _driver(frame: FakeFrame) -> PlaywrightDriver:
    """A driver whose only live part is ``_active_frame``.

    ``object.__new__`` skips ``__init__`` so no browser, playwright instance or
    tab bookkeeping is needed — ``fill_login`` reaches for nothing else.
    """
    drv = object.__new__(PlaywrightDriver)
    drv._active_frame = lambda: frame  # type: ignore[method-assign]
    # fill_login_at resolves its ref through the tab's ref registry; the
    # registry is covered by test_refs.py, so here the ref goes straight to
    # the frame — the login logic is what this file tests.
    drv._active = lambda: None  # type: ignore[method-assign]
    drv._ref_locator = lambda _tab, ref: frame.locator(f"aria-ref={ref}")  # type: ignore[method-assign]
    return drv


# --- single step (regression: must keep working exactly as before) ----------


async def test_single_step_fills_both_and_submits() -> None:
    user, pw = FakeElement(), FakeElement()
    frame = FakeFrame(username=user, password=pw)

    assert await _driver(frame).fill_login("alice", "s3cret") is True

    assert user.filled == ["alice"]
    assert pw.filled == ["s3cret"]
    # Submit happens on the password field, as before.
    assert pw.pressed == ["Enter"]
    assert frame.waited == []  # never took the two-step path


async def test_single_step_without_username_field_still_submits() -> None:
    """Some portals pre-fill the username and show only a password box."""
    pw = FakeElement()
    frame = FakeFrame(username=None, password=pw)

    assert await _driver(frame).fill_login("alice", "s3cret") is True
    assert pw.filled == ["s3cret"]
    assert pw.pressed == ["Enter"]


# --- two step ---------------------------------------------------------------


async def test_two_step_submits_username_then_password() -> None:
    """The SAP Ariba shape: no password field until the username is submitted."""
    user = FakeElement()
    pw = FakeElement(visible=False)  # exists but hidden until step two
    frame = FakeFrame(username=user, password=pw)
    # Submitting the username is what reveals the password screen.
    user.on_press = lambda: setattr(pw, "visible", True)

    assert await _driver(frame).fill_login("ali@example.com", "s3cret") is True

    assert user.filled == ["ali@example.com"]
    assert user.pressed == ["Enter"]
    assert pw.filled == ["s3cret"]
    assert pw.pressed == ["Enter"]
    assert frame.waited == [_PASSWORD_SELECTOR]


async def test_two_step_absent_password_field_appears_after_username() -> None:
    """Password input isn't in the DOM at all on screen one."""
    user = FakeElement()
    frame = FakeFrame(username=user, password=None)

    def reveal() -> None:
        frame.password = FakeElement()

    user.on_press = reveal

    assert await _driver(frame).fill_login("alice", "s3cret") is True
    assert frame.password is not None
    assert frame.password.filled == ["s3cret"]


async def test_hidden_password_field_is_not_mistaken_for_a_form() -> None:
    """The bug this fixes.

    A hidden password input on the username screen used to be found, filled and
    submitted — sending the password nowhere. It must route to the two-step path
    instead, which is what ``_visible_selector`` guarantees.
    """
    user = FakeElement()
    pw = FakeElement(visible=False)
    frame = FakeFrame(username=user, password=pw)
    user.on_press = lambda: setattr(pw, "visible", True)

    await _driver(frame).fill_login("alice", "s3cret")

    # The username was submitted FIRST — the hidden field was never filled blind.
    assert user.pressed == ["Enter"]
    assert pw.filled == ["s3cret"]


# --- failure modes ----------------------------------------------------------


async def test_no_fields_at_all_reports_no_form() -> None:
    frame = FakeFrame(username=None, password=None)
    assert await _driver(frame).fill_login("alice", "s3cret") is False


async def test_password_screen_never_arrives_reports_no_form() -> None:
    """A portal that swallows the username must fail, not hang the turn."""
    user = FakeElement()
    frame = FakeFrame(username=user, password=None)  # nothing ever reveals it

    assert await _driver(frame).fill_login("alice", "s3cret") is False
    assert user.filled == ["alice"]  # we did try
    assert frame.waited == [_PASSWORD_SELECTOR]


async def test_invisible_username_field_reports_no_form() -> None:
    frame = FakeFrame(username=FakeElement(visible=False), password=None)
    assert await _driver(frame).fill_login("alice", "s3cret") is False


@pytest.mark.parametrize("boom", [RuntimeError("detached"), TimeoutError("gone")])
async def test_visibility_probe_failure_is_not_fatal(boom: Exception) -> None:
    """A detached handle mid-probe reads as 'not visible', never an exception."""
    pw = FakeElement()

    async def explode() -> bool:
        raise boom

    pw.is_visible = explode  # type: ignore[method-assign]
    frame = FakeFrame(username=None, password=pw)

    # No username to fall back to, so this is a clean False rather than a raise.
    assert await _driver(frame).fill_login("alice", "s3cret") is False


# --- fill at a caller-chosen ref --------------------------------------------


async def test_fill_at_ref_single_step_fills_both_and_submits() -> None:
    user, pw = FakeElement(), FakeElement()
    frame = FakeFrame(password=pw, by_ref={"e7": user})

    assert await _driver(frame).fill_login_at("e7", "alice", "s3cret") is True

    assert user.filled == ["alice"]
    assert pw.filled == ["s3cret"]
    assert pw.pressed == ["Enter"]
    assert frame.waited == []


async def test_fill_at_ref_takes_the_two_step_path_when_no_password_yet() -> None:
    """The case that motivated the ref form: a progressive-disclosure form the
    caller reached itself, where the password screen only follows the username."""
    user = FakeElement()
    pw = FakeElement(visible=False)
    frame = FakeFrame(password=pw, by_ref={"e7": user})
    user.on_press = lambda: setattr(pw, "visible", True)

    assert await _driver(frame).fill_login_at("e7", "alice", "s3cret") is True

    assert user.filled == ["alice"]
    assert user.pressed == ["Enter"]
    assert pw.filled == ["s3cret"]
    assert frame.waited == [_PASSWORD_SELECTOR]


async def test_fill_at_ref_never_consults_the_username_selector() -> None:
    """The whole point of the ref form.

    ``_USERNAME_SELECTOR`` matches a bare ``input[type=text]``, so guessing on a
    page the caller navigated to can land on a search box and submit it. Here the
    frame has NO selector-findable username field and the fill still works — proof
    the ref is what is used.
    """
    user, pw = FakeElement(), FakeElement()
    frame = FakeFrame(username=None, password=pw, by_ref={"e7": user})

    assert await _driver(frame).fill_login_at("e7", "alice", "s3cret") is True
    assert user.filled == ["alice"]


async def test_fill_at_a_ref_that_matches_nothing_is_a_clean_false() -> None:
    frame = FakeFrame(password=FakeElement(), by_ref={})

    assert await _driver(frame).fill_login_at("e99", "alice", "s3cret") is False


async def test_fill_at_ref_returns_false_when_the_password_screen_never_arrives() -> None:
    user = FakeElement()
    frame = FakeFrame(password=None, by_ref={"e7": user})  # nothing ever reveals one

    assert await _driver(frame).fill_login_at("e7", "alice", "s3cret") is False
    assert user.pressed == ["Enter"], "it must have tried to advance the form"

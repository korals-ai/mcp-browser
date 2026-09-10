"""Stale-ref failures must teach the agent the corrective call.

A Playwright selector timeout on a ref action reads as noise; the rewrite
names both possible causes (stale ref vs blocked element) because they need
opposite fixes and the timeout alone cannot tell them apart.
"""

from __future__ import annotations

import pytest

from src.browser_driver import PlaywrightDriver, _raise_ref_action_error


class TimeoutError(Exception):  # the NAME is what the rewrite keys on
    """Stands in for playwright's TimeoutError without importing playwright."""


def test_timeout_is_rewritten_with_corrective_action() -> None:
    with pytest.raises(ValueError, match="browser_snapshot") as exc_info:
        _raise_ref_action_error("click", "e7", TimeoutError("Timeout 30000ms exceeded"))
    message = str(exc_info.value)
    assert "e7" in message
    assert "stale" in message  # cause 1
    assert "covered" in message or "not interactable" in message  # cause 2


def test_non_timeout_errors_pass_through_unchanged() -> None:
    original = RuntimeError("browser crashed")
    with pytest.raises(RuntimeError) as exc_info:
        _raise_ref_action_error("type", "e7", original)
    assert exc_info.value is original


async def test_driver_click_rewrites_stale_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TimeoutFrame:
        async def click(self, selector: str) -> None:
            raise TimeoutError("Timeout 30000ms exceeded")

    d = PlaywrightDriver()
    monkeypatch.setattr(d, "_active_frame", lambda: _TimeoutFrame())
    with pytest.raises(ValueError, match="browser_snapshot"):
        await d.click("e42")

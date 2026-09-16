"""Refs are valid for ONE document: the driver's stale-ref rule, and the
error text a blocked action gets. Bare driver instances, no browser."""

from __future__ import annotations

from typing import Any

import pytest

from src.browser_driver import (
    PlaywrightDriver,
    StaleRefError,
    _action_error,
    _is_textual,
    _png_size,
    _redact_headers,
    _Tab,
)


class _Page:
    def __init__(self, url: str = "about:blank") -> None:
        self.url = url
        self.main_frame = object()

    def on(self, _event: str, _cb: Any) -> None:
        pass


def _tab(num: int = 1) -> _Tab:
    return _Tab(f"t{num}", _Page(), object())


def _driver() -> PlaywrightDriver:
    return object.__new__(PlaywrightDriver)


def test_tab_number_comes_from_its_id() -> None:
    assert _tab(7).num == 7


def test_a_ref_before_any_read_page_is_refused_by_name() -> None:
    tab = _tab()
    with pytest.raises(StaleRefError, match="No read_page has been taken for tab 1"):
        _driver()._check_ref(tab, "e5")


def test_a_ref_from_the_latest_read_is_accepted() -> None:
    tab = _tab()
    tab.refs, tab.refs_epoch = {"e5"}, tab.doc_epoch
    _driver()._check_ref(tab, "e5")  # no raise


def test_a_ref_the_latest_read_did_not_return_is_refused() -> None:
    tab = _tab()
    tab.refs, tab.refs_epoch = {"e5"}, tab.doc_epoch
    with pytest.raises(StaleRefError, match="e9 is not in the latest read_page of tab 1"):
        _driver()._check_ref(tab, "e9")


def test_a_ref_from_a_previous_document_is_refused_after_a_navigation() -> None:
    tab = _tab(2)
    tab.refs, tab.refs_epoch = {"e5"}, tab.doc_epoch
    tab.doc_epoch += 1  # what a main-frame framenavigated does
    with pytest.raises(StaleRefError, match="e5 is from a previous page \\(tab 2 navigated"):
        _driver()._check_ref(tab, "e5")


def test_action_error_names_the_covering_element_from_playwrights_log() -> None:
    class TimeoutError(Exception):  # the NAME is what the rewrite keys on
        pass

    exc = TimeoutError(
        'Locator.click: Timeout 5000ms exceeded.\n  - <div class="overlay"></div> intercepts pointer events\n'
    )
    err = _action_error("click", "e5", exc)
    assert isinstance(err, ValueError)
    assert "could not click e5" in str(err)
    assert '<div class="overlay"> covers it' in str(err)
    assert "scroll_to" in str(err) and "read_page" in str(err)


def test_action_error_without_a_named_cover_says_not_interactable() -> None:
    class TimeoutError(Exception):
        pass

    err = _action_error("hover", "e5", TimeoutError("Timeout 5000ms exceeded."))
    assert "not interactable right now" in str(err)


def test_a_frame_detached_by_a_racing_navigation_reads_as_a_stale_ref() -> None:
    """The ref check passed (same epoch), then a navigation the previous click
    started committed and the aria-ref selector resolved in a detached frame.
    Playwright's words for that become the stale-ref message, so the agent
    reads the page again instead of retrying the ref."""
    for text in (
        'Locator.click: Invalid frame in aria-ref selector "aria-ref=e5"',
        "Frame was detached",
        "Execution context was destroyed, most likely because of a navigation",
    ):
        err = _action_error("click", "e5", Exception(text))
        assert isinstance(err, StaleRefError), text
        assert "e5 is from a previous page" in str(err)
        assert "read_page again" in str(err)


def test_action_error_passes_other_exceptions_through() -> None:
    original = RuntimeError("browser crashed")
    assert _action_error("click", "e5", original) is original


def test_png_size_reads_the_ihdr() -> None:
    header = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (640).to_bytes(4, "big")
        + (480).to_bytes(4, "big")
    )
    assert _png_size(header + b"\x00" * 8) == (640, 480)
    assert _png_size(b"not a png") == (0, 0)


def test_textual_content_types_and_header_redaction() -> None:
    assert _is_textual("application/json; charset=utf-8")
    assert _is_textual("text/html")
    assert not _is_textual("image/png")
    assert not _is_textual("application/octet-stream")
    out = _redact_headers({"Cookie": "a=b", "Authorization": "Bearer x", "Accept": "*/*"})
    assert out == {"Cookie": "<redacted>", "Authorization": "<redacted>", "Accept": "*/*"}

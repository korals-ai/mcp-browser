"""normalize_cursor: the closed allowlist that keeps a page-supplied cursor from
reaching the viewer verbatim. Anything not on the list collapses to "" so the
viewer keeps its own resting crosshair — a hostile/exotic cursor can't blank or
hijack the human's pointer."""

from __future__ import annotations

import pytest

from src.protocol import BrowserCursor, normalize_cursor


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("pointer", "pointer"),
        ("TEXT", "text"),  # case-folded
        ("  grab  ", "grab"),  # trimmed
        ('url("hand.cur") 4 4, pointer', "pointer"),  # takes the keyword fallback
        ("url(x), text", "text"),
        ("auto", ""),  # not mirrored — viewer keeps its default
        ("default", ""),
        ("none", ""),  # never hide the human's cursor
        ("", ""),
        ("evil-injection; }", ""),  # anything off-list is dropped
        ("url(only-an-image)", ""),  # no keyword fallback → dropped
    ],
)
def test_normalize_cursor(raw: str, expected: str) -> None:
    assert normalize_cursor(raw) == expected


def test_browser_cursor_wire_shape() -> None:
    assert BrowserCursor(cursor="pointer").to_json() == {
        "type": "browser_cursor",
        "cursor": "pointer",
    }

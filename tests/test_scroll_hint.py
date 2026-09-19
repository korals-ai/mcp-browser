"""The scroll hint a ``find`` miss carries.

The 2026-09-18 miss: a sidebar list rendering only rows in view hid its target
from both find tiers, and a bare "no match" read as "not on this page". Each
test pins one thing the calling model must be able to tell apart — hidden
content vs none vs a measurement that never ran.
"""

from __future__ import annotations

from typing import Any

from src.scroll_hint import format_scroll_hint


def _measured(
    regions: list[dict[str, Any]], *, below: float = 0.0, main: bool = True
) -> dict[str, Any]:
    return {
        "in_main_frame": main,
        "page": {"pages_above": 0.0, "pages_below": below},
        "regions": regions,
    }


_FILES = {
    "label": "div",
    "pages_above": 0.0,
    "pages_below": 20.47,
    "first": "tender-leads",
    "last": "080_W2-8-E651(PDF).pdf",
    "on_screen": True,
    "x": 200,
    "y": 700,
}


def test_a_list_with_rows_below_is_named_with_where_to_scroll() -> None:
    out = format_scroll_hint(_measured([_FILES]))
    assert out.startswith("Content is scrolled out of view")
    assert '"tender-leads" … "080_W2-8-E651(PDF).pdf": 20.5 pages below' in out
    assert "computer scroll at coordinate [200, 700]" in out
    assert "the page itself: all in view" in out


def test_nothing_hidden_is_stated_as_absence_not_as_silence() -> None:
    out = format_scroll_hint(_measured([]))
    assert out == "Nothing on this page is scrolled out of view — the target is not rendered here."


def test_a_measurement_that_never_ran_is_not_read_as_nothing_hidden() -> None:
    assert format_scroll_hint(None).startswith("Could not measure")
    assert "Nothing on this page" not in format_scroll_hint(None)


def test_rounding_slivers_are_not_reported_as_content() -> None:
    sliver = {**_FILES, "pages_below": 0.01}
    assert format_scroll_hint(_measured([sliver])).startswith("Nothing on this page")


def test_the_page_itself_scrolling_is_reported() -> None:
    out = format_scroll_hint(_measured([], below=2.0))
    assert "the page itself: 2.0 pages below" in out


def test_biggest_hidden_region_comes_first_and_the_list_is_capped() -> None:
    small = [{**_FILES, "first": f"row{i}", "last": "", "pages_below": 0.5} for i in range(6)]
    out = format_scroll_hint(_measured([*small, _FILES]))
    lines = out.splitlines()
    assert '"tender-leads"' in lines[1]
    assert "(+2 smaller scroll boxes)" in out


def test_coordinates_are_withheld_inside_a_frame() -> None:
    # Frame-relative coordinates would land the wheel in the wrong place.
    out = format_scroll_hint(_measured([_FILES], main=False))
    assert "20.5 pages below" in out and "coordinate" not in out


def test_an_off_screen_box_says_to_scroll_the_page_first() -> None:
    off = {**_FILES, "on_screen": False, "first": "", "last": "", "x": None, "y": None}
    out = format_scroll_hint(_measured([off], below=3.0))
    assert "the box itself is off screen" in out

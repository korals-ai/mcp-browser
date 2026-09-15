"""xdotool-style key names (the `computer` contract) → Playwright's."""

from __future__ import annotations

import pytest

from src.keys import click_modifiers, to_playwright_combo


@pytest.mark.parametrize(
    ("combo", "expected"),
    [
        ("Return", "Enter"),
        ("enter", "Enter"),
        ("ctrl+a", "Control+a"),
        ("shift+Tab", "Shift+Tab"),
        ("cmd+shift+t", "Meta+Shift+t"),
        ("Escape", "Escape"),
        ("Page_Down", "PageDown"),
        ("f5", "F5"),
        ("a", "a"),
        ("space", " "),
        ("ArrowDown", "ArrowDown"),
    ],
)
def test_to_playwright_combo(combo: str, expected: str) -> None:
    assert to_playwright_combo(combo) == expected


def test_empty_combo_is_an_error() -> None:
    with pytest.raises(ValueError, match="empty"):
        to_playwright_combo("+")


def test_click_modifiers_map_and_reject_unknown() -> None:
    assert click_modifiers(["ctrl", "Shift"]) == ["Control", "Shift"]
    assert click_modifiers(None) == []
    with pytest.raises(ValueError, match="unknown modifier"):
        click_modifiers(["hyper"])

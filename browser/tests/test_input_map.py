"""Normalized input event → CDP command mapping."""

from __future__ import annotations

import pytest

from src.input_map import MAX_PASTE_CHARS, to_cdp_command


def test_mouse_move_has_no_button() -> None:
    method, params = to_cdp_command("mouse", {"kind": "move", "x": 10, "y": 20})
    assert method == "Input.dispatchMouseEvent"
    assert params["type"] == "mouseMoved"
    assert params["button"] == "none"
    assert (params["x"], params["y"]) == (10.0, 20.0)


def test_mouse_press_carries_button_and_clickcount() -> None:
    _, params = to_cdp_command(
        "mouse", {"kind": "down", "x": 1, "y": 2, "button": "left", "clickCount": 2}
    )
    assert params["type"] == "mousePressed"
    assert params["button"] == "left"
    assert params["clickCount"] == 2


def test_wheel_maps_to_mousewheel_with_deltas() -> None:
    method, params = to_cdp_command("wheel", {"x": 5, "y": 6, "deltaX": 0, "deltaY": -120})
    assert method == "Input.dispatchMouseEvent"
    assert params["type"] == "mouseWheel"
    assert params["deltaY"] == -120.0


def test_key_down_forwards_only_present_text_fields() -> None:
    method, params = to_cdp_command("key", {"kind": "down", "key": "a", "code": "KeyA"})
    assert method == "Input.dispatchKeyEvent"
    assert params == {"type": "keyDown", "key": "a", "code": "KeyA"}


def test_char_event() -> None:
    _, params = to_cdp_command("key", {"kind": "char", "text": "A"})
    assert params == {"type": "char", "text": "A"}


def test_unknown_event_raises() -> None:
    with pytest.raises(ValueError):
        to_cdp_command("teleport", {})


def test_bad_mouse_kind_raises() -> None:
    with pytest.raises(ValueError):
        to_cdp_command("mouse", {"kind": "wiggle", "x": 0, "y": 0})


def test_bad_button_raises() -> None:
    with pytest.raises(ValueError):
        to_cdp_command("mouse", {"kind": "down", "x": 0, "y": 0, "button": "elbow"})


def test_non_numeric_coord_raises() -> None:
    with pytest.raises(ValueError):
        to_cdp_command("mouse", {"kind": "move", "x": "left", "y": 0})


def test_paste_maps_to_insert_text() -> None:
    method, params = to_cdp_command("paste", {"text": "RFP-2026-0447"})
    assert method == "Input.insertText"
    assert params == {"text": "RFP-2026-0447"}


def test_paste_keeps_multiline_and_non_latin_text_verbatim() -> None:
    text = "خط اول\nline two\t3"
    _, params = to_cdp_command("paste", {"text": text})
    assert params == {"text": text}


def test_paste_without_text_raises() -> None:
    with pytest.raises(ValueError):
        to_cdp_command("paste", {})


def test_paste_empty_text_raises() -> None:
    with pytest.raises(ValueError):
        to_cdp_command("paste", {"text": ""})


def test_paste_non_string_text_raises() -> None:
    with pytest.raises(ValueError):
        to_cdp_command("paste", {"text": ["a"]})


def test_paste_over_the_cap_raises() -> None:
    with pytest.raises(ValueError):
        to_cdp_command("paste", {"text": "x" * (MAX_PASTE_CHARS + 1)})


def test_paste_at_the_cap_is_accepted() -> None:
    _, params = to_cdp_command("paste", {"text": "x" * MAX_PASTE_CHARS})
    assert len(params["text"]) == MAX_PASTE_CHARS

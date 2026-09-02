"""Map a viewer's normalized input event to a CDP ``Input.*`` command.

The co-browse viewer sends browser-agnostic events (``{event, fields}``) — it
shouldn't have to know CDP's wire shape. This pure function does the translation
so :class:`PlaywrightDriver.send_input` is a one-liner and the mapping is
unit-tested without a browser. Coordinates arrive already mapped to the remote
viewport (the viewer scales client→remote before sending).

See docs/plan/20260706T014659Z-cobrowse-agent-poc.md §4.6 (v1 input plane).
"""

from __future__ import annotations

from typing import Any

# CDP button values; the viewer sends "left"/"middle"/"right" (or omits → left).
_BUTTONS = {"left", "middle", "right", "none"}

_MOUSE_TYPE = {"move": "mouseMoved", "down": "mousePressed", "up": "mouseReleased"}
_KEY_TYPE = {"down": "keyDown", "up": "keyUp", "char": "char"}

# A paste carries the VIEWER's clipboard text: the remote Chromium's own clipboard
# is empty, so replaying Ctrl+V as key events pastes nothing. `Input.insertText`
# puts the text into the focused element in one call, which is what a human
# copying a reference number out of the chat actually means.
#
# Bounded because the text arrives from the browser and we replay it verbatim: a
# portal field holds hundreds of characters, so anything past this is a malformed
# or hostile frame, not a person pasting. The viewer knows the same limit
# (apps/chat-ui/src/types/cobrowseProtocol.ts) — over it, the frame is dropped.
MAX_PASTE_CHARS = 100_000


def to_cdp_command(event: str, fields: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return ``(cdp_method, params)`` for one normalized input event.

    Raises ``ValueError`` on an unknown event/kind so a malformed frame is
    dropped by the caller instead of dispatching a bogus CDP command."""
    if event == "mouse":
        return "Input.dispatchMouseEvent", _mouse_params(fields)
    if event == "wheel":
        return "Input.dispatchMouseEvent", _wheel_params(fields)
    if event == "key":
        return "Input.dispatchKeyEvent", _key_params(fields)
    if event == "paste":
        return "Input.insertText", _paste_params(fields)
    raise ValueError(f"unknown input event: {event!r}")


def _num(fields: dict[str, Any], key: str, default: float = 0.0) -> float:
    v = fields.get(key, default)
    if not isinstance(v, (int, float)):
        raise ValueError(f"input field {key!r} must be a number, got {v!r}")
    return float(v)


def _mouse_params(fields: dict[str, Any]) -> dict[str, Any]:
    kind = fields.get("kind")
    if kind not in _MOUSE_TYPE:
        raise ValueError(f"bad mouse kind: {kind!r}")
    button = fields.get("button", "left")
    if button not in _BUTTONS:
        raise ValueError(f"bad mouse button: {button!r}")
    return {
        "type": _MOUSE_TYPE[kind],
        "x": _num(fields, "x"),
        "y": _num(fields, "y"),
        # A move carries no button; a press/release needs one + a click count.
        "button": "none" if kind == "move" else button,
        "clickCount": int(fields.get("clickCount", 0 if kind == "move" else 1)),
    }


def _wheel_params(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "mouseWheel",
        "x": _num(fields, "x"),
        "y": _num(fields, "y"),
        "deltaX": _num(fields, "deltaX"),
        "deltaY": _num(fields, "deltaY"),
    }


def _key_params(fields: dict[str, Any]) -> dict[str, Any]:
    kind = fields.get("kind")
    if kind not in _KEY_TYPE:
        raise ValueError(f"bad key kind: {kind!r}")
    params: dict[str, Any] = {"type": _KEY_TYPE[kind]}
    # Only forward the CDP-recognised text fields that are present.
    for k in ("key", "code", "text"):
        v = fields.get(k)
        if isinstance(v, str) and v:
            params[k] = v
    return params


def _paste_params(fields: dict[str, Any]) -> dict[str, Any]:
    text = fields.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("paste needs a non-empty text field")
    if len(text) > MAX_PASTE_CHARS:
        raise ValueError(f"paste text too long: {len(text)} > {MAX_PASTE_CHARS}")
    return {"text": text}

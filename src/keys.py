"""Key names the ``computer`` tool speaks, mapped to Playwright's.

The extension's ``key`` action takes xdotool-style combos (``ctrl+a``,
``Return``, ``shift+Tab``); Playwright wants ``Control+a``, ``Enter``,
``Shift+Tab``. One table, so the two can't drift, and a pure function so the
mapping is unit-tested without a browser.
"""

from __future__ import annotations

_MODIFIERS: dict[str, str] = {
    "ctrl": "Control",
    "control": "Control",
    "alt": "Alt",
    "option": "Alt",
    "shift": "Shift",
    "cmd": "Meta",
    "command": "Meta",
    "meta": "Meta",
    "super": "Meta",
    "win": "Meta",
}

_KEYS: dict[str, str] = {
    "return": "Enter",
    "enter": "Enter",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": " ",
    "backspace": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "pageup": "PageUp",
    "page_up": "PageUp",
    "pagedown": "PageDown",
    "page_down": "PageDown",
    "home": "Home",
    "end": "End",
    "insert": "Insert",
    "capslock": "CapsLock",
    "caps_lock": "CapsLock",
}
_KEYS.update({f"f{i}": f"F{i}" for i in range(1, 13)})


def to_playwright_combo(text: str) -> str:
    """``ctrl+shift+t`` → ``Control+Shift+t``; ``Return`` → ``Enter``; a
    single character stays itself. Raises ``ValueError`` on an empty key."""
    parts = [p for p in text.strip().split("+") if p != ""]
    if not parts:
        raise ValueError("key combo is empty")
    *mods, key = parts
    mapped_mods = [_MODIFIERS.get(m.lower(), m) for m in mods]
    lowered = key.lower()
    if len(key) == 1:
        mapped_key = key
    elif lowered in _KEYS:
        mapped_key = _KEYS[lowered]
    elif lowered in _MODIFIERS:
        mapped_key = _MODIFIERS[lowered]
    else:
        mapped_key = key[0].upper() + key[1:]  # "PageDown" / "Enter" already fine
    return "+".join([*mapped_mods, mapped_key])


def click_modifiers(names: list[str] | None) -> list[str]:
    """The ``modifiers`` a click holds down, as Playwright spells them.
    Unknown names raise so a typo never silently clicks without its modifier."""
    out: list[str] = []
    for name in names or []:
        key = _MODIFIERS.get(str(name).lower())
        if key is None:
            raise ValueError(f"unknown modifier {name!r} (use ctrl, alt, shift or cmd)")
        out.append(key)
    return out

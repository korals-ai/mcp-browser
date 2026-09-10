"""Post-type read-back: the note phrasing rules and their security floor.

A page that reformats or autocompletes typed input used to be invisible until
submit time; the read-back names it in the same turn. The one hard rule: a
secret field's value is NEVER echoed back into model context.
"""

from __future__ import annotations

from typing import Any

import pytest

from src import agent_ops
from src.browser_driver import PlaywrightDriver, _type_note
from tests.conftest import FakeDriver, make_manager


def test_matching_value_is_plain_ok() -> None:
    assert _type_note("hello", {"tag": "input", "value": "hello"}) == "ok"


def test_missing_readback_degrades_to_ok() -> None:
    # The fill itself succeeded; a failed/absent read-back must not invent a note.
    assert _type_note("hello", None) == "ok"
    assert _type_note("hello", {"tag": "input"}) == "ok"


def test_reformatted_value_is_reported_with_the_actual_value() -> None:
    note = _type_note("2026-09-15", {"tag": "input", "type": "text", "value": "09/15/2026"})
    assert note.startswith("ok — ")
    assert "09/15/2026" in note
    assert "reformatted or restricted" in note


def test_secret_field_mismatch_never_echoes_the_value() -> None:
    note = _type_note("hunter2", {"tag": "input", "type": "password", "value": "•••"})
    assert "hunter2" not in note
    assert "•••" not in note
    assert "value hidden" in note


def test_autocomplete_field_gets_the_suggestion_hint() -> None:
    note = _type_note("Tor", {"tag": "input", "value": "Tor", "role": "combobox"})
    assert "browser_snapshot" in note
    assert "instead of" in note and "Enter" in note
    # aria-autocomplete works too; "none" does not.
    assert "browser_snapshot" in _type_note("x", {"value": "x", "autocomplete": "list"})
    assert _type_note("x", {"value": "x", "autocomplete": "none"}) == "ok"


async def test_driver_type_returns_note_and_survives_readback_failure() -> None:
    class _Frame:
        def __init__(self, raw: Any) -> None:
            self.raw = raw
            self.filled: list[tuple[str, str]] = []

        async def fill(self, selector: str, text: str) -> None:
            self.filled.append((selector, text))

        async def evaluate(self, js: str, arg: Any = None) -> Any:
            if isinstance(self.raw, Exception):
                raise self.raw
            return self.raw

    d = PlaywrightDriver()
    frame = _Frame({"tag": "input", "value": "09/15/2026"})
    d._active_frame = lambda: frame  # type: ignore[method-assign]
    note = await d.type_text("e3", "2026-09-15")
    assert "09/15/2026" in note
    # Read-back blowing up must not fail a type that already landed.
    d._active_frame = lambda: _Frame(RuntimeError("ctx destroyed"))  # type: ignore[method-assign]
    assert await d.type_text("e3", "2026-09-15") == "ok"


async def test_agent_ops_type_propagates_note_and_fill_form_carries_it() -> None:
    driver = FakeDriver()
    driver.type_note = "ok — note: the field now contains '09/15/2026', not the text you typed"
    manager, _ = make_manager(driver)
    out = await agent_ops.type_text(manager, "c1", "e3", "2026-09-15")
    assert "09/15/2026" in out
    form = await agent_ops.fill_form(manager, "c1", [{"ref": "e3", "value": "2026-09-15"}])
    assert form["fields"][0]["status"] == "ok"
    assert "09/15/2026" in form["fields"][0]["note"]


async def test_fill_form_omits_note_when_clean() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    form = await agent_ops.fill_form(manager, "c1", [{"ref": "e1", "value": "abc"}])
    assert form["fields"][0]["status"] == "ok"
    assert "note" not in form["fields"][0]


@pytest.mark.parametrize(
    "fragment",
    [
        "[role=checkbox]",
        "[role=tab]",
        "[role=menuitem]",
        "[role=combobox]",
        "[role=slider]",
        "[onclick]",
        '[tabindex]:not([tabindex="-1"])',
        "summary",
    ],
)
def test_snapshot_selector_covers_widget_classes(fragment: str) -> None:
    # Custom widgets the old selector missed: the agent cannot click what the
    # snapshot does not show, so the selector IS the agent's field of vision.
    from src.browser_driver import _SNAPSHOT_JS

    assert fragment in _SNAPSHOT_JS

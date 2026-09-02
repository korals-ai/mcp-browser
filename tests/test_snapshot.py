"""The security-critical redaction rule: passwords never reach the agent."""

from __future__ import annotations

from src.snapshot import REDACTED, Element, redact_snapshot, snapshot_to_json


def test_password_field_value_is_redacted() -> None:
    raw: list[Element] = [
        {"ref": "e0", "tag": "input", "type": "password", "value": "hunter2"},
    ]
    out = redact_snapshot(raw)
    assert out[0]["value"] == REDACTED
    assert "hunter2" not in str(out)


def test_non_password_value_survives() -> None:
    raw: list[Element] = [
        {"ref": "e0", "tag": "input", "type": "email", "value": "a@b.com"},
    ]
    assert redact_snapshot(raw)[0]["value"] == "a@b.com"


def test_secret_ref_redacts_even_when_type_is_text() -> None:
    # A portal recipe can flag a mislabeled (type=text) password field by ref.
    raw: list[Element] = [{"ref": "e9", "tag": "input", "type": "text", "value": "s3cret"}]
    out = redact_snapshot(raw, secret_refs=frozenset({"e9"}))
    assert out[0]["value"] == REDACTED


def test_password_element_still_present_so_agent_sees_it_exists() -> None:
    raw: list[Element] = [{"ref": "e0", "tag": "input", "type": "password", "value": "x"}]
    out = redact_snapshot(raw)
    assert len(out) == 1
    assert out[0]["ref"] == "e0"
    assert out[0]["type"] == "password"


def test_redact_does_not_mutate_input() -> None:
    raw: list[Element] = [{"ref": "e0", "tag": "input", "type": "password", "value": "x"}]
    redact_snapshot(raw)
    assert raw[0]["value"] == "x"  # original untouched


def test_snapshot_to_json_drops_unknown_keys() -> None:
    els: list[Element] = [{"ref": "e0", "tag": "a", "name": "Home"}]
    js = snapshot_to_json(els)
    assert js == [{"ref": "e0", "tag": "a", "name": "Home"}]

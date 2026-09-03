"""DOM snapshot shape + the password-redaction rule.

The agent "sees" the page through :func:`redact_snapshot` — a trimmed list of
interactable elements it reads to decide what to click/type. This module is the
SECURITY-CRITICAL seam: even after ``browser_login`` injects a password
server-side, a naive snapshot could read the field's ``value`` back and pull the
plaintext into the model's context (chat JSONL, Bedrock logs, model memory).

So redaction happens HERE, as a pure function over the raw element list, with no
browser dependency — it is unit-tested in isolation. The driver produces raw
elements; the MCP ``browser_snapshot`` tool returns ``redact_snapshot(raw)``.
See docs/plan/20260706T014659Z-cobrowse-agent-poc.md §4.5.
"""

from __future__ import annotations

from typing import Any, TypedDict, cast

# A password value is never returned to the agent; this sentinel makes the
# redaction VISIBLE in the snapshot (so the agent knows a secret field exists
# and is filled) without leaking the bytes.
REDACTED = "••• [redacted]"


class Element(TypedDict, total=False):
    """One interactable node the agent can target. ``ref`` is a stable handle
    the agent passes back to click/type; ``value`` is present only for inputs."""

    ref: str
    tag: str
    type: str  # input type attr (text, password, email, checkbox, ...)
    role: str
    name: str  # accessible name / label
    value: str
    placeholder: str


def _is_secret_field(el: Element, secret_refs: frozenset[str]) -> bool:
    """A field whose value must never reach the agent.

    Two triggers: an HTML ``type=password`` input, OR a ref the caller flagged
    as secret (e.g. a portal login recipe names its password field by ref even
    when the site mislabels it as ``type=text``)."""
    if el.get("type", "").lower() == "password":
        return True
    return el.get("ref", "") in secret_refs


def redact_snapshot(
    raw: list[Element], *, secret_refs: frozenset[str] = frozenset()
) -> list[Element]:
    """Return a copy of ``raw`` with every secret field's ``value`` masked.

    Pure, side-effect free. ``secret_refs`` lets a login recipe mark extra fields
    secret beyond the ``type=password`` default. The element is kept (the agent
    still needs to know the field exists and is populated) — only the ``value``
    is replaced with :data:`REDACTED`."""
    out: list[Element] = []
    for el in raw:
        if "value" in el and _is_secret_field(el, secret_refs):
            masked = cast(Element, {**el})  # shallow copy — values are scalars
            masked["value"] = REDACTED
            out.append(masked)
        else:
            out.append(el)
    return out


def snapshot_to_json(elements: list[Element]) -> list[dict[str, Any]]:
    """Serialise for the MCP tool result (TypedDict is already a dict at runtime,
    but this pins the boundary + drops any non-Element keys a driver slipped in)."""
    keys = ("ref", "tag", "type", "role", "name", "value", "placeholder")
    out: list[dict[str, Any]] = []
    for el in elements:
        d = dict(el)  # plain dict -> dynamic key access is type-clean
        out.append({k: d[k] for k in keys if k in d})
    return out

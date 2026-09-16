"""The model tier of ``find``: a small model reads the tree, names the refs.

The extension's ``find`` is model-backed: the accessibility tree plus the
query go to a small fast model that answers with refs and a one-line reason
each, so the main model never pays 20-50k tokens of tree to locate one
control. This module is that call, against any endpoint that speaks the
Anthropic Messages API (``/v1/messages``): an AI gateway, OpenRouter, or
Anthropic itself — which one is the operator's choice, never this code's.

Configured by :class:`FindConfig`, built from env in ``server.py``. The
endpoint URL is REQUIRED and an explicit empty value is the declared sentinel
for "no model tier" — ``find`` then runs its literal tier only and says so.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("workspace-tool-browser")

# Answer lines look like ``e12: the search box in the header``. Refs may carry
# a frame prefix (``f3e12``), so the class is wider than ``e\d+``.
_ANSWER_RE = re.compile(r"^\s*[-*]?\s*(?P<ref>[A-Za-z]+\d+(?:e\d+)?)\s*[:—-]\s*(?P<reason>.+?)\s*$")
_MAX_HITS = 20
_TIMEOUT_S = 20.0

# Who is calling. OpenRouter ranks and attributes apps by these two headers
# (the Referer is the app's identity, the title its display name — both, for
# an app that runs locally with no site of its own); every other Messages
# endpoint ignores headers it does not know. So they ride on every call
# rather than branching on the configured provider.
APP_URL = "https://github.com/korals-ai/mcp-browser"
APP_TITLE = "mcp-browser"


@dataclass(frozen=True)
class FindConfig:
    """Where the model tier calls. ``url == ""`` disables it."""

    url: str
    key: str
    model: str

    def __post_init__(self) -> None:
        # Model ids are the provider's own (`anthropic/claude-haiku-4.5` on
        # OpenRouter, `claude-haiku-4-5-20251001` on Anthropic), so an endpoint
        # without one is a misconfiguration to stop at startup, not a call to
        # fail on the first `find`.
        if self.url and not self.model:
            raise ValueError(
                "BROWSER_FIND_INFERENCE_URL names an endpoint but BROWSER_FIND_MODEL is "
                "empty — name the model find should ask, in that provider's own naming"
            )

    @property
    def enabled(self) -> bool:
        return bool(self.url)


def build_prompt(tree: str, query: str) -> str:
    return (
        "You are locating elements on a web page for another agent.\n"
        "Below is the page's accessibility tree; interactive nodes carry a ref like "
        "[ref=e12].\n\n"
        f"Query: {query}\n\n"
        "Answer with up to 20 lines, best match first, each exactly:\n"
        "<ref>: <one short reason it matches>\n"
        "Use only refs that appear in the tree. If nothing matches, answer exactly: "
        "NO MATCH, then one line naming the closest candidates.\n\n"
        f"Tree:\n{tree}"
    )


def parse_answer(text: str, known_refs: set[str]) -> list[dict[str, object]]:
    """The model's lines as hits, keeping only refs that exist in the tree —
    a hallucinated ref must never reach the agent as something to click."""
    hits: list[dict[str, object]] = []
    for line in text.splitlines():
        m = _ANSWER_RE.match(line)
        if not m:
            continue
        ref = m.group("ref")
        if ref not in known_refs:
            continue
        hits.append({"ref": ref, "reason": m.group("reason")})
        if len(hits) >= _MAX_HITS:
            break
    return hits


async def find_with_model(
    config: FindConfig,
    *,
    tree: str,
    query: str,
    known_refs: set[str],
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, object]]:
    """Ask the configured model; hits are ``{ref, reason}``. Raises on a
    transport or non-2xx failure — the caller reports it, it does not fall
    back silently to "no match"."""
    payload: dict[str, Any] = {
        "model": config.model,
        "max_tokens": 600,
        "messages": [{"role": "user", "content": build_prompt(tree, query)}],
    }
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "HTTP-Referer": APP_URL,
        "X-OpenRouter-Title": APP_TITLE,
    }
    if config.key:
        headers["x-api-key"] = config.key
        headers["authorization"] = f"Bearer {config.key}"
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=_TIMEOUT_S)
    try:
        resp = await http.post(
            config.url.rstrip("/") + "/v1/messages", json=payload, headers=headers
        )
        resp.raise_for_status()
        body = resp.json()
    finally:
        if own_client:
            await http.aclose()
    text = "".join(
        str(block.get("text", "")) for block in body.get("content", []) if isinstance(block, dict)
    )
    usage = body.get("usage") or {}
    log.info(
        "find model tier model=%s in=%s out=%s",
        config.model,
        usage.get("input_tokens"),
        usage.get("output_tokens"),
    )
    return parse_answer(text, known_refs)

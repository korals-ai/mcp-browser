"""Classify what a navigation actually landed on: content, or a wall.

Anti-bot walls (Cloudflare challenges, retailer robot checks) come back as a
plausible-looking HTML page, so the agent re-reads and retries them as if they
were content — burning turns on a page no retry will change (observed in a
live tenant chat, 2026-09-09). This module names the wall from protocol-level
evidence in ONE place, as a pure function; the navigate tool puts the result
on its response as ``page_state``.

Evidence order: the ``cf-mitigated: challenge`` response header is
Cloudflare's own protocol marker for a served challenge and outranks status
codes; the title/URL markers are the vendors' documented interstitials
(Cloudflare "Just a moment…" / "Attention Required!", Amazon "Robot Check" /
``/errors/validateCaptcha``). Extend the marker tables only from block pages
actually captured in real runs — a guessed marker matches nothing and reads
as coverage (match-list rule, ``.claude/CLAUDE.md`` Tier 2).
"""

from __future__ import annotations

from collections.abc import Mapping

# Every value classify_page_state can return. "unknown" is deliberately NOT
# produced here — it is the DRIVER's label for "navigation succeeded but
# classification itself failed", kept distinct from "ok" so a broken
# classifier can never report a wall as content (false-"ok" family).
PAGE_STATES = ("ok", "blocked_challenge", "blocked_denied", "rate_limited", "server_error")

_CHALLENGE_TITLE_PREFIXES = ("just a moment", "attention required", "robot check")
_CHALLENGE_URL_MARKERS = ("/errors/validatecaptcha",)


def classify_page_state(
    status: int | None,
    headers: Mapping[str, str],
    url: str,
    title: str,
) -> str:
    """Name the page a completed navigation landed on.

    ``status``/``headers`` come from the navigation response (``None``/empty
    for same-document navigations, which are always content). Pure and
    side-effect free so the taxonomy is unit-testable without a browser.
    """
    header_lookup = {k.lower(): v for k, v in headers.items()}
    if header_lookup.get("cf-mitigated", "").lower() == "challenge":
        return "blocked_challenge"
    title_norm = title.strip().lower()
    if title_norm.startswith(_CHALLENGE_TITLE_PREFIXES):
        return "blocked_challenge"
    url_norm = url.lower()
    if any(marker in url_norm for marker in _CHALLENGE_URL_MARKERS):
        return "blocked_challenge"
    if status is None:
        return "ok"
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "blocked_denied"
    if status >= 500:
        return "server_error"
    return "ok"

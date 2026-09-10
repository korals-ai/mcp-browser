"""The page_state taxonomy: pure classification, no browser.

Each class must be reachable AND the absent case must be distinguishable — a
classifier whose PASS and FAIL look identical is the repo's recurring bug
class, so the "ok" cases are asserted as explicitly as the walls.
"""

from __future__ import annotations

from src.page_state import PAGE_STATES, classify_page_state


def test_plain_200_is_ok() -> None:
    assert classify_page_state(200, {}, "https://example.com/", "Example") == "ok"


def test_same_document_navigation_without_response_is_ok() -> None:
    # page.goto returns None for same-document navigations — always content.
    assert classify_page_state(None, {}, "https://example.com/#a", "Example") == "ok"


def test_cf_mitigated_header_wins_even_on_200() -> None:
    # Cloudflare's protocol-level challenge marker outranks the status code —
    # a managed challenge can be served with a 200.
    headers = {"CF-Mitigated": "challenge"}
    assert classify_page_state(200, headers, "https://shop.example/", "Shop") == (
        "blocked_challenge"
    )


def test_cloudflare_interstitial_title() -> None:
    assert (
        classify_page_state(403, {}, "https://shop.example/", "Just a moment...")
        == "blocked_challenge"
    )


def test_amazon_robot_check_title_and_captcha_url() -> None:
    assert classify_page_state(200, {}, "https://x.example/", "Robot Check") == (
        "blocked_challenge"
    )
    assert (
        classify_page_state(200, {}, "https://x.example/errors/validateCaptcha?u=1", "Amazon")
        == "blocked_challenge"
    )


def test_denied_and_rate_limited_and_server_error() -> None:
    assert classify_page_state(403, {}, "https://x.example/", "Access Denied") == ("blocked_denied")
    assert classify_page_state(401, {}, "https://x.example/", "Login") == "blocked_denied"
    assert classify_page_state(429, {}, "https://x.example/", "Too Many Requests") == (
        "rate_limited"
    )
    assert classify_page_state(503, {}, "https://x.example/", "Oops") == "server_error"


async def test_driver_reports_unknown_when_classification_fails() -> None:
    # A broken classifier must degrade to "unknown", never "ok" — otherwise a
    # wall gets passed off as content the moment the classifier breaks.
    from src.browser_driver import PlaywrightDriver

    class _ExplodingPage:
        url = "https://x.example/"

        async def title(self) -> str:
            raise RuntimeError("page gone")

    class _Tab:
        page = _ExplodingPage()

    state = await PlaywrightDriver()._classify_landed_page(_Tab(), None)  # type: ignore[arg-type]
    assert state == "unknown"


def test_every_returned_state_is_declared() -> None:
    # The tool docstring enumerates PAGE_STATES for the agent; a state the
    # function can return but the docs don't declare would be a silent contract
    # break.
    cases = [
        (200, {}, "https://a/", "A"),
        (200, {"cf-mitigated": "challenge"}, "https://a/", "A"),
        (403, {}, "https://a/", "A"),
        (429, {}, "https://a/", "A"),
        (500, {}, "https://a/", "A"),
    ]
    for status, headers, url, title in cases:
        assert classify_page_state(status, headers, url, title) in PAGE_STATES

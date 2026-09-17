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

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from src.refs_tree import ref_lines

log = logging.getLogger("workspace-tool-browser")

# Answer lines look like ``e12: the search box in the header``. Refs may carry
# a frame prefix (``f3e12``), so the class is wider than ``e\d+``. Models copy
# the tree's own notation back — measured 2026-09-16: Claude Haiku answered
# ``[ref=e31]: …`` on every line, and a bare-ref-only pattern turned every real
# answer into "No match" — so the ref may arrive wrapped as ``[ref=e31]``,
# ``ref=e31``, `` `e31` `` or ``**e31**``.
_ANSWER_RE = re.compile(
    r"^\s*[-*]?\s*[\[`*]*\s*(?:ref\s*=\s*)?"
    r"(?P<ref>[A-Za-z]+\d+(?:e\d+)?)"
    r"\s*[\]`*]*\s*[:—-]\s*(?P<reason>.+?)\s*$"
)
# The prompt's example answer is built from a ref IN the tree it sends: every
# page after a tab's first carries frame-prefixed refs (``f5e33``), and an
# example showing a bare ``e12`` taught a model to answer ``e33`` — the right
# element, dropped because no such ref exists (measured 2026-09-17). A test
# holds that the parser reads the example, so instruction and parser can't drift.
EXAMPLE_REASON = "why this element matches the query"
_FALLBACK_EXAMPLE_REF = "e12"
_MAX_HITS = 20
# Thinking models spend their answer budget reasoning first: at 600 tokens
# they stopped with no answer text on most calls; at 4000 they answered.
# A model that doesn't think uses ~50, so the ceiling costs it nothing.
_ANSWER_BUDGET_TOKENS = 4000
# Free thinking models took 22-50 s on 3 of 16 calls (2026-09-17). Longer than
# the agent waiting on a word-match miss would like, shorter than failing them.
_TIMEOUT_S = 60.0
_PROVIDER_MESSAGE_CHARS = 200
# An overloaded free model answers in under a second; asked again 2 s later,
# 12 of 13 overloaded calls answered, with no rate-limit error (2026-09-17).
_OVERLOAD_RETRY_WAIT_S = 2.0

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


class ModelTierFailed(Exception):
    """The model tier gave no usable answer. The message names the cause and
    what the person can do — it reaches the agent in place of "no match",
    which would claim the model looked and found nothing."""


class ProviderOverloaded(ModelTierFailed):
    """The provider said it is overloaded — the one failure worth asking again."""


def example_answer(tree: str) -> str:
    """One answer line in the exact shape asked for, using the tree's own first ref."""
    refs = list(ref_lines(tree))
    return f"{refs[0] if refs else _FALLBACK_EXAMPLE_REF}: {EXAMPLE_REASON}"


def build_prompt(tree: str, query: str) -> str:
    example = example_answer(tree)
    ref = example.split(":", 1)[0]
    return (
        "You are locating elements on a web page for another agent.\n"
        "Below is the page's accessibility tree; interactive nodes carry a ref like "
        f"[ref={ref}].\n\n"
        f"Query: {query}\n\n"
        "Answer with up to 20 lines, best match first, each exactly:\n"
        "<ref>: <one short reason it matches>\n"
        "with the ref copied exactly as the tree writes it (including any letter-and-number "
        f"prefix), without brackets, for example:\n{example}\n"
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
    """Ask the configured model; hits are ``{ref, reason}``, or ``[]`` when the
    model answered that nothing matches. Anything else — a failed call, an
    empty reply, an answer naming no ref on the page — raises
    :class:`ModelTierFailed`, never an empty list.

    Only an overloaded provider is asked a second time. Nothing else is
    retried: a rejected call still counts against a free model's per-minute
    limit (measured on OpenRouter), so retrying a 429 spends the person's next
    minute too."""
    payload: dict[str, Any] = {
        "model": config.model,
        "max_tokens": _ANSWER_BUDGET_TOKENS,
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
        try:
            return await _ask_once(http, payload, headers, config, known_refs)
        except ProviderOverloaded:
            await asyncio.sleep(_OVERLOAD_RETRY_WAIT_S)
            return await _ask_once(http, payload, headers, config, known_refs)
    finally:
        if own_client:
            await http.aclose()


async def _ask_once(
    http: httpx.AsyncClient,
    payload: dict[str, Any],
    headers: dict[str, str],
    config: FindConfig,
    known_refs: set[str],
) -> list[dict[str, object]]:
    try:
        resp = await http.post(
            config.url.rstrip("/") + "/v1/messages", json=payload, headers=headers
        )
    except httpx.TimeoutException as exc:
        raise ModelTierFailed(
            f"{config.model} did not answer within {_TIMEOUT_S:.0f} s. Try again, or "
            "choose a faster model (BROWSER_FIND_MODEL)."
        ) from exc
    except httpx.HTTPError as exc:
        raise ModelTierFailed(
            f"could not reach {config.url} ({type(exc).__name__}). Check "
            "BROWSER_FIND_INFERENCE_URL and the network."
        ) from exc
    if resp.status_code >= 400:
        raise ModelTierFailed(_explain_status(resp.status_code, _provider_message(resp), config))
    try:
        body = resp.json()
    except ValueError as exc:
        raise ModelTierFailed(f"{config.url} answered with something that isn't JSON.") from exc
    return _hits_from_body(body, config, known_refs)


def _hits_from_body(
    body: dict[str, Any], config: FindConfig, known_refs: set[str]
) -> list[dict[str, object]]:
    text = "".join(
        str(block.get("text", "")) for block in body.get("content", []) if isinstance(block, dict)
    )
    # OpenRouter delivers some upstream failures as HTTP 200 whose body is an
    # Anthropic-shaped error, e.g. {"type": "error", "error": {"type":
    # "overloaded_error", "message": "Upstream error from Nvidia: Service
    # temporarily overloaded"}} — 15 of 48 free-model calls on 2026-09-17.
    if body.get("type") == "error" or "error" in body:
        error = body.get("error")
        kind = error.get("type") if isinstance(error, dict) else None
        failure = ProviderOverloaded if kind == "overloaded_error" else ModelTierFailed
        raise failure(_explain_error_body(error, config))
    usage = body.get("usage") or {}
    log.info(
        "find model tier model=%s in=%s out=%s stop=%s",
        config.model,
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        body.get("stop_reason"),
    )
    if not text.strip():
        if body.get("stop_reason") == "max_tokens":
            raise ModelTierFailed(
                f"{config.model} spent its whole {_ANSWER_BUDGET_TOKENS}-token budget thinking "
                "and wrote no answer. Try again, or choose a model that answers directly."
            )
        raise ModelTierFailed(
            f"{config.model} returned an empty reply "
            f"(stop_reason={body.get('stop_reason')!r}). Try again."
        )
    hits = parse_answer(text, known_refs)
    if hits or "NO MATCH" in text.upper():
        return hits
    raise ModelTierFailed(
        f"{config.model} answered, but named no element on this page: "
        f"{_one_line(text)!r}. Try again, or choose another model."
    )


def _explain_status(status: int, message: str, config: FindConfig) -> str:
    said = f" The provider said: {message!r}." if message else ""
    if status in (401, 403):
        cause = (
            f"{config.url} refused the key or refuses {config.model} for this caller "
            f"(HTTP {status}). Check BROWSER_FIND_INFERENCE_KEY, or choose another model."
        )
    elif status == 402:
        cause = "the account behind the key is out of credit (HTTP 402)."
    elif status == 404:
        cause = f"{config.url} has no model {config.model!r} (HTTP 404). Check BROWSER_FIND_MODEL."
    elif status == 429:
        cause = (
            f"{config.model} is rate-limited (HTTP 429) — by its provider, or by the "
            "account's per-minute or daily limit. Not retried; try again in a minute."
        )
    elif status >= 500:
        cause = f"{config.url} failed on its side (HTTP {status}). Try again shortly."
    else:
        cause = f"{config.url} rejected the request (HTTP {status})."
    return cause + said


def _explain_error_body(error: object, config: FindConfig) -> str:
    kind = error.get("type") if isinstance(error, dict) else None
    message = error.get("message") if isinstance(error, dict) else error
    said = f" The provider said: {_one_line(str(message))!r}." if message else ""
    if kind == "overloaded_error":
        return (
            f"{config.model}'s provider is overloaded right now. Try again in a moment, or "
            f"choose another model.{said}"
        )
    return f"{config.model}'s provider returned an error ({kind or 'unknown'}).{said}"


def _provider_message(resp: httpx.Response) -> str:
    """The provider's own one-line reason, when its error body has one."""
    try:
        err = resp.json().get("error")
    except (ValueError, AttributeError):
        return ""
    message = err.get("message") if isinstance(err, dict) else err
    return _one_line(str(message)) if message else ""


def _one_line(text: str) -> str:
    flat = " ".join(text.split())
    return flat[:_PROVIDER_MESSAGE_CHARS]

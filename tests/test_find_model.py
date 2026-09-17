"""The model tier of `find`: prompt shape, answer parsing, the HTTP call."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from src import find_model
from src.find_model import (
    APP_TITLE,
    APP_URL,
    EXAMPLE_REASON,
    FindConfig,
    ModelTierFailed,
    build_prompt,
    example_answer,
    find_with_model,
    parse_answer,
)


def test_config_enabled_only_with_a_url() -> None:
    assert FindConfig(url="", key="", model="m").enabled is False
    assert FindConfig(url="https://gw", key="k", model="m").enabled is True


def test_prompt_carries_query_and_tree_and_the_answer_shape() -> None:
    p = build_prompt('- button "Go" [ref=e1]', "the go button")
    assert "Query: the go button" in p
    assert "[ref=e1]" in p
    assert "<ref>: <one short reason it matches>" in p


def test_parse_answer_reads_refs_in_the_trees_own_notation() -> None:
    # The shape a real model returned (Claude Haiku through OpenRouter,
    # 2026-09-16): it copies `[ref=eN]` from the tree it was shown.
    text = (
        "[ref=e31]: Primary button to sign in with credentials\n"
        "[ref=e36]: Button to sign in with Google\n"
        "ref=e46 — Link to create a new account\n"
        "`e49`: passkey button\n"
        "**f2e7**: inside the frame\n"
        "[ref=e999]: hallucinated"
    )
    hits = parse_answer(text, {"e31", "e36", "e46", "e49", "f2e7"})
    assert [h["ref"] for h in hits] == ["e31", "e36", "e46", "e49", "f2e7"]
    assert hits[0]["reason"] == "Primary button to sign in with credentials"


def test_the_prompts_example_uses_the_trees_own_ref_and_the_parser_reads_it() -> None:
    # Every page after a tab's first has frame-prefixed refs. An example showing
    # a bare `e12` taught a model to answer `e33` for `f5e33` (2026-09-17).
    tree = '- banner [ref=f5e2]:\n  - link "Login" [ref=f5e33]'
    example = example_answer(tree)
    assert example == f"f5e2: {EXAMPLE_REASON}"
    prompt = build_prompt(tree, "the way in")
    assert example in prompt and "[ref=f5e2]" in prompt and "e12" not in prompt
    assert parse_answer(example, {"f5e2", "f5e33"}) == [{"ref": "f5e2", "reason": EXAMPLE_REASON}]


def test_an_empty_tree_still_gets_an_example_the_parser_reads() -> None:
    example = example_answer("")
    assert parse_answer(example, {example.split(":")[0]})


def test_parse_answer_keeps_only_refs_that_exist_in_the_tree() -> None:
    text = "e4: the sign-in button\n- e99: hallucinated\nf2e7 — inside the frame\nNO MATCH"
    hits = parse_answer(text, {"e4", "f2e7"})
    assert hits == [
        {"ref": "e4", "reason": "the sign-in button"},
        {"ref": "f2e7", "reason": "inside the frame"},
    ]


class _Resp:
    def __init__(self, body: Any, status: int = 200) -> None:
        self._body, self.status_code = body, status

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Client:
    def __init__(self, resp: _Resp | Exception) -> None:
        self.resp = resp
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _Resp:
        self.calls.append((url, json, headers))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


_CONFIG = FindConfig(url="https://gw.example", key="", model="some/model:free")


async def _ask(resp: _Resp | Exception, known: set[str] | None = None) -> tuple[Any, _Client]:
    client = _Client(resp)
    hits = await find_with_model(
        _CONFIG,
        tree='- button "Sign in" [ref=e4]',
        query="sign in",
        known_refs={"e4"} if known is None else known,
        client=client,  # type: ignore[arg-type]
    )
    return hits, client


async def _failure(resp: _Resp | Exception) -> tuple[str, _Client]:
    client = _Client(resp)
    with pytest.raises(ModelTierFailed) as info:
        await find_with_model(
            _CONFIG,
            tree="",
            query="x",
            known_refs={"e4"},
            client=client,  # type: ignore[arg-type]
        )
    return str(info.value), client


async def test_find_with_model_posts_to_v1_messages_with_the_key() -> None:
    config = FindConfig(url="https://gw.example/", key="vk-1", model="claude-haiku-4-5-20251001")
    client = _Client(
        _Resp({"content": [{"type": "text", "text": "e4: the sign-in button"}], "usage": {}})
    )
    hits = await find_with_model(
        config,
        tree='- button "Sign in" [ref=e4]',
        query="sign in",
        known_refs={"e4"},
        client=client,  # type: ignore[arg-type]
    )
    assert hits == [{"ref": "e4", "reason": "the sign-in button"}]
    url, payload, headers = client.calls[0]
    assert url == "https://gw.example/v1/messages"
    assert payload["model"] == "claude-haiku-4-5-20251001"
    # Room for a thinking model to finish reasoning AND answer.
    assert payload["max_tokens"] == 4000
    assert headers["x-api-key"] == "vk-1"
    assert headers["authorization"] == "Bearer vk-1"
    assert payload["messages"][0]["content"].startswith("You are locating elements")
    # Attribution rides on every call, whichever provider the URL names.
    assert headers["HTTP-Referer"] == APP_URL == "https://github.com/korals-ai/mcp-browser"
    assert headers["X-OpenRouter-Title"] == APP_TITLE


def test_an_endpoint_without_a_model_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="BROWSER_FIND_MODEL is empty"):
        FindConfig(url="https://openrouter.ai/api", key="sk-or-x", model="")
    # The literal-only tier names no model, and needs none.
    assert FindConfig(url="", key="", model="").enabled is False


async def test_an_honest_no_match_is_an_empty_answer_not_a_failure() -> None:
    text = "NO MATCH\nClosest candidates: e4 (the sign-in button)"
    hits, _ = await _ask(_Resp({"content": [{"type": "text", "text": text}]}), known=set())
    assert hits == []


async def test_a_rate_limit_is_explained_with_the_providers_words_and_not_retried() -> None:
    body = {
        "error": {
            "type": "rate_limit_error",
            "message": "Rate limit exceeded: free-models-per-min.",
        }
    }
    message, client = await _failure(_Resp(body, status=429))
    assert "rate-limited (HTTP 429)" in message and "Not retried" in message
    assert "free-models-per-min" in message
    # A rejected call still spends the free per-minute limit: exactly one call.
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("status", "expect"),
    [
        (401, "refused the key"),
        (403, "refused the key"),
        (402, "out of credit"),
        (404, "has no model 'some/model:free'"),
        (502, "failed on its side (HTTP 502)"),
        (400, "rejected the request (HTTP 400)"),
    ],
)
async def test_every_failed_status_names_its_cause(status: int, expect: str) -> None:
    message, _ = await _failure(_Resp(ValueError("not json"), status=status))
    assert expect in message


async def test_a_timeout_is_explained_though_httpx_gives_it_no_message() -> None:
    # httpx's timeout carries an empty message; the agent once received
    # "Error executing tool find: " with nothing after it.
    message, _ = await _failure(httpx.ReadTimeout(""))
    assert "did not answer within 60 s" in message


async def test_an_unreachable_endpoint_is_explained() -> None:
    message, _ = await _failure(httpx.ConnectError("refused"))
    assert "could not reach https://gw.example (ConnectError)" in message


async def test_a_thinking_model_that_ran_out_of_budget_is_a_failure_not_no_match() -> None:
    body = {"content": [{"type": "thinking", "thinking": "..."}], "stop_reason": "max_tokens"}
    message, _ = await _failure(_Resp(body))
    assert "whole 4000-token budget thinking" in message


async def test_an_empty_reply_is_a_failure_not_no_match() -> None:
    # Seen 4 times: HTTP 200 with no stop_reason, no usage, no text.
    message, _ = await _failure(_Resp({"content": []}))
    assert "returned an empty reply (stop_reason=None)" in message


async def test_an_answer_naming_nothing_on_the_page_is_a_failure_quoting_it() -> None:
    # `openrouter/free` once routed the prompt to a safety classifier.
    message, _ = await _failure(_Resp({"content": [{"type": "text", "text": "User Safety: safe"}]}))
    assert "named no element on this page: 'User Safety: safe'" in message


_OVERLOADED = {
    # OpenRouter's shape for an overloaded free model: status 200, error body.
    "type": "error",
    "error": {
        "type": "overloaded_error",
        "message": "Upstream error from Nvidia: Service temporarily overloaded",
    },
}


class _Sequence(_Client):
    """Answers each call with the next response in order."""

    def __init__(self, *resps: _Resp) -> None:
        super().__init__(resps[0])
        self._queue = list(resps)

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _Resp:
        self.calls.append((url, json, headers))
        return self._queue.pop(0)


@pytest.fixture
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(find_model, "_OVERLOAD_RETRY_WAIT_S", 0.0)


@pytest.mark.usefixtures("no_retry_wait")
async def test_an_overloaded_provider_is_asked_once_more_and_its_answer_used() -> None:
    answer = _Resp({"content": [{"type": "text", "text": "e4: the sign-in button"}]})
    client = _Sequence(_Resp(_OVERLOADED), answer)
    hits = await find_with_model(
        _CONFIG,
        tree="",
        query="x",
        known_refs={"e4"},
        client=client,  # type: ignore[arg-type]
    )
    assert hits == [{"ref": "e4", "reason": "the sign-in button"}]
    assert len(client.calls) == 2


@pytest.mark.usefixtures("no_retry_wait")
async def test_overloaded_twice_is_explained_after_exactly_two_calls() -> None:
    client = _Sequence(_Resp(_OVERLOADED), _Resp(_OVERLOADED), _Resp(_OVERLOADED))
    with pytest.raises(ModelTierFailed) as info:
        await find_with_model(
            _CONFIG,
            tree="",
            query="x",
            known_refs={"e4"},
            client=client,  # type: ignore[arg-type]
        )
    assert "provider is overloaded right now" in str(info.value)
    assert "Upstream error from Nvidia" in str(info.value)
    assert len(client.calls) == 2


async def test_an_unknown_error_body_still_names_its_type() -> None:
    message, _ = await _failure(_Resp({"type": "error", "error": {"type": "api_error"}}))
    assert "returned an error (api_error)" in message

"""The model tier of `find`: prompt shape, answer parsing, the HTTP call."""

from __future__ import annotations

from typing import Any

import pytest

from src.find_model import (
    ANSWER_EXAMPLE,
    APP_TITLE,
    APP_URL,
    FindConfig,
    build_prompt,
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


def test_the_prompts_example_answer_is_one_the_parser_reads() -> None:
    assert ANSWER_EXAMPLE in build_prompt('- searchbox "Search" [ref=e12]', "search")
    assert parse_answer(ANSWER_EXAMPLE, {"e12"}) == [
        {"ref": "e12", "reason": "the search box in the header"}
    ]


def test_parse_answer_keeps_only_refs_that_exist_in_the_tree() -> None:
    text = "e4: the sign-in button\n- e99: hallucinated\nf2e7 — inside the frame\nNO MATCH"
    hits = parse_answer(text, {"e4", "f2e7"})
    assert hits == [
        {"ref": "e4", "reason": "the sign-in button"},
        {"ref": "f2e7", "reason": "inside the frame"},
    ]


class _Resp:
    def __init__(self, body: dict[str, Any], status: int = 200) -> None:
        self._body, self.status_code = body, status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._body


class _Client:
    def __init__(self, resp: _Resp) -> None:
        self.resp = resp
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _Resp:
        self.calls.append((url, json, headers))
        return self.resp


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


async def test_find_with_model_raises_on_a_bad_status() -> None:
    config = FindConfig(url="https://gw.example", key="", model="m")
    client = _Client(_Resp({}, status=502))
    with pytest.raises(RuntimeError, match="HTTP 502"):
        await find_with_model(config, tree="", query="x", known_refs=set(), client=client)  # type: ignore[arg-type]

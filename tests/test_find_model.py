"""The model tier of `find`: prompt shape, answer parsing, the HTTP call."""

from __future__ import annotations

from typing import Any

import pytest

from src.find_model import FindConfig, build_prompt, find_with_model, parse_answer


def test_config_enabled_only_with_a_url() -> None:
    assert FindConfig(url="", key="", model="m").enabled is False
    assert FindConfig(url="https://gw", key="k", model="m").enabled is True


def test_prompt_carries_query_and_tree_and_the_answer_shape() -> None:
    p = build_prompt('- button "Go" [ref=e1]', "the go button")
    assert "Query: the go button" in p
    assert "[ref=e1]" in p
    assert "<ref>: <one short reason it matches>" in p


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
    assert payload["messages"][0]["content"].startswith("You are locating elements")


async def test_find_with_model_raises_on_a_bad_status() -> None:
    config = FindConfig(url="https://gw.example", key="", model="m")
    client = _Client(_Resp({}, status=502))
    with pytest.raises(RuntimeError, match="HTTP 502"):
        await find_with_model(config, tree="", query="x", known_refs=set(), client=client)  # type: ignore[arg-type]

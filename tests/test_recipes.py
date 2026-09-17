"""Recipes as stored batches: parsing, descriptor targets, and the runner."""

from __future__ import annotations

from typing import Any

import pytest

from src import agent_ops, recipes
from tests.conftest import DEFAULT_TREE

# --- parse ---------------------------------------------------------------------


def test_parse_accepts_a_batch_shaped_recipe() -> None:
    raw = {
        "name": "search",
        "params": ["keyword"],
        "steps": [
            {"name": "navigate", "input": {"url": "https://x"}},
            {"name": "read_page", "input": {}},
            {
                "name": "computer",
                "input": {"action": "type", "text": "param:keyword"},
                "target": {"role": "searchbox"},
            },
            {"name": "computer", "input": {"action": "key", "text": "Return"}},
            {"name": "get_page_text", "input": {}},
        ],
    }
    recipe = recipes.parse(raw)
    assert recipe["params"] == ["keyword"]
    assert len(recipe["steps"]) == 5


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "must be an object"),
        ({"steps": []}, "non-empty 'steps'"),
        ({"steps": [{"name": "javascript_tool", "input": {"text": "1"}}]}, "not allowed"),
        ({"steps": [{"name": "file_upload", "input": {}}]}, "not allowed"),
        (
            {"steps": [{"name": "computer", "input": {"action": "screenshot"}}]},
            "not allowed in a recipe",
        ),
        (
            {"steps": [{"name": "computer", "input": {"action": "left_click", "ref": "e5"}}]},
            "cannot store 'ref'",
        ),
        (
            {"steps": [{"name": "read_page", "input": {}, "target": {"role": "button"}}]},
            "takes no ref",
        ),
        (
            {
                "steps": [
                    {"name": "form_input", "input": {"value": "x"}, "target": {"kind": "button"}}
                ]
            },
            "unknown target key",
        ),
        (
            {"steps": [{"name": "form_input", "input": {"value": "x"}, "target": {"nth": 0}}]},
            "needs 'role' and/or 'name'",
        ),
        (
            {
                "steps": [
                    {
                        "name": "form_input",
                        "input": {"value": "x"},
                        "target": {"role": "textbox", "nth": True},
                    }
                ]
            },
            "non-negative integer",
        ),
        ({"steps": [{"tool": "navigate"}]}, "has no 'name'"),
        ({"steps": [{"name": "navigate", "input": "x"}]}, "'input' must be an object"),
        ({"steps": [1], "params": "k"}, "must be a list of names"),
    ],
)
def test_parse_rejects(raw: Any, message: str) -> None:
    with pytest.raises(recipes.RecipeError, match=message):
        recipes.parse(raw)


# --- params ---------------------------------------------------------------------


def test_substitute_walks_the_input_object() -> None:
    inp = {"text": "param:q", "nested": {"value": "param:q"}, "list": ["param:q", "lit"], "n": 1}
    assert recipes.substitute(inp, {"q": "pumps"}) == {
        "text": "pumps",
        "nested": {"value": "pumps"},
        "list": ["pumps", "lit"],
        "n": 1,
    }
    with pytest.raises(recipes.RecipeError, match="parameter 'q'"):
        recipes.substitute("param:q", {})
    assert recipes.missing_params({"params": ["a", "b"]}, {"a": "1"}) == ["b"]


# --- targets --------------------------------------------------------------------


def test_resolve_target_by_role_and_name() -> None:
    assert recipes.resolve_target(DEFAULT_TREE, {"role": "button", "name": "Sign in"}) == "e4"
    assert recipes.resolve_target(DEFAULT_TREE, {"name": "Search products"}) == "e2"
    assert recipes.resolve_target(DEFAULT_TREE, {"role": "searchbox"}) == "e2"


def test_resolve_target_refuses_ambiguity_unless_nth_says_which() -> None:
    tree = '- button "Add" [ref=e1]\n- button "Add" [ref=e2]\n'
    with pytest.raises(recipes.RecipeError, match="2 elements match"):
        recipes.resolve_target(tree, {"role": "button", "name": "Add"})
    assert recipes.resolve_target(tree, {"role": "button", "name": "Add", "nth": 1}) == "e2"
    with pytest.raises(recipes.RecipeError, match="out of range"):
        recipes.resolve_target(tree, {"role": "button", "name": "Add", "nth": 5})


def test_resolve_target_names_what_it_looked_for_on_a_miss() -> None:
    with pytest.raises(recipes.RecipeError, match="no element matches"):
        recipes.resolve_target(DEFAULT_TREE, {"role": "button", "name": "Buy"})


# --- the runner -------------------------------------------------------------------


def _dispatch(calls: list[tuple[str, dict[str, Any]]], **overrides: Any) -> dict[str, Any]:
    """A fake tool table: records every call, answers like the real tools do."""

    def handler_for(name: str) -> Any:
        async def h(**kw: Any) -> Any:
            calls.append((name, kw))
            if name in overrides:
                return overrides[name]
            if name == "read_page":
                return DEFAULT_TREE
            if name == "navigate":
                return {"page_state": "ok", "url": kw.get("url")}
            if name == "wait_for":
                return {"ready": True}
            if name == "login":
                return {"status": "submitted"}
            return "ok"

        return h

    return {n: handler_for(n) for n in recipes.ALLOWED_TOOLS}


async def test_runner_resolves_targets_against_its_own_read_page() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    recipe = recipes.parse(
        {
            "params": ["q"],
            "steps": [
                {"name": "navigate", "input": {"url": "https://x"}},
                {"name": "read_page", "input": {}},
                {
                    "name": "computer",
                    "input": {"action": "type", "text": "param:q"},
                    "target": {"role": "searchbox"},
                },
                {"name": "get_page_text", "input": {}},
            ],
        }
    )
    out = await agent_ops.run_recipe(recipe, {"q": "pumps"}, tab_id=3, dispatch=_dispatch(calls))
    assert out["status"] == "ok" and out["steps_run"] == 4
    typed = next(kw for name, kw in calls if name == "computer")
    assert typed == {"action": "type", "text": "pumps", "tabId": 3, "ref": "e2"}
    assert all(kw["tabId"] == 3 for _, kw in calls)
    assert [e["tool"] for e in out["extracted"]] == ["read_page", "get_page_text"]


async def test_runner_drops_read_so_a_wall_is_still_judged_by_its_dict() -> None:
    """No model reads between steps; a stored `read` would turn navigate's
    reply into text and hide the `page_state` the runner stops on."""
    calls: list[tuple[str, dict[str, Any]]] = []
    recipe = recipes.parse(
        {"steps": [{"name": "navigate", "input": {"url": "https://x", "read": "text"}}]}
    )
    out = await agent_ops.run_recipe(recipe, {}, tab_id=1, dispatch=_dispatch(calls))
    assert out["status"] == "ok"
    assert calls == [("navigate", {"url": "https://x", "tabId": 1})]


async def test_runner_refuses_a_target_before_any_read_page() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    recipe = recipes.parse(
        {
            "steps": [
                {"name": "form_input", "input": {"value": "x"}, "target": {"role": "searchbox"}}
            ]
        }
    )
    out = await agent_ops.run_recipe(recipe, {}, tab_id=1, dispatch=_dispatch(calls))
    assert out["status"] == "step_failed" and out["failed_at"] == 0
    assert "no read_page step" in out["reason"]
    assert calls == []


async def test_runner_stops_at_a_stale_descriptor_with_the_step_index() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    recipe = recipes.parse(
        {
            "steps": [
                {"name": "read_page", "input": {}},
                {
                    "name": "computer",
                    "input": {"action": "left_click"},
                    "target": {"role": "button", "name": "Buy"},
                },
                {"name": "get_page_text", "input": {}},
            ]
        }
    )
    out = await agent_ops.run_recipe(recipe, {}, tab_id=1, dispatch=_dispatch(calls))
    assert out["status"] == "step_failed"
    assert out["failed_at"] == 1 and out["tool"] == "computer"
    assert "no element matches" in out["reason"]
    assert out["steps_run"] == 1
    assert [n for n, _ in calls] == ["read_page"]  # never reached the third step


@pytest.mark.parametrize(
    ("name", "output", "reason"),
    [
        ("wait_for", {"ready": False}, "did not"),
        ("login", {"status": "no_login_form"}, "login did not complete: no_login_form"),
        ("navigate", {"page_state": "blocked_challenge"}, "landed on a wall"),
    ],
)
async def test_runner_treats_a_reported_failure_as_a_failed_step(
    name: str, output: Any, reason: str
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    recipe = recipes.parse(
        {
            "steps": [
                {"name": name, "input": {"url": "https://x", "portal_id": "p"}},
                {"name": "get_page_text", "input": {}},
            ]
        }
    )
    out = await agent_ops.run_recipe(
        recipe, {}, tab_id=1, dispatch=_dispatch(calls, **{name: output})
    )
    assert out["status"] == "step_failed" and out["failed_at"] == 0
    assert reason in out["reason"]


async def test_runner_reports_missing_params_before_touching_the_site() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    recipe = recipes.parse(
        {"params": ["q"], "steps": [{"name": "navigate", "input": {"url": "https://x"}}]}
    )
    out = await agent_ops.run_recipe(recipe, {}, tab_id=1, dispatch=_dispatch(calls))
    assert out == {"status": "missing_params", "missing": ["q"], "steps_run": 0}
    assert calls == []

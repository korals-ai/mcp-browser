"""Recipe parsing, targeting, and the boundaries around what a recipe may do.

A recipe is data that arrives on the tenant volume, so anything it can reach is
reachable by anyone who can drop a file there. These tests pin the two rules
that make that safe — the tool allowlist and no-secrets — plus the targeting
rule that keeps a stale recipe from silently clicking the wrong thing.
"""

from __future__ import annotations

import pytest

from src import recipes


def _snapshot() -> list[dict[str, str]]:
    return [
        {"ref": "e0", "tag": "input", "type": "text", "name": "Username"},
        {"ref": "e1", "tag": "input", "type": "password", "name": "Password"},
        {"ref": "e2", "tag": "button", "type": "submit", "name": "Sign in"},
        {"ref": "e3", "tag": "input", "type": "text", "name": "Search"},
        {"ref": "e4", "tag": "a", "type": "", "name": "Next"},
        {"ref": "e5", "tag": "a", "type": "", "name": "Next"},
    ]


# --- what a recipe may execute ----------------------------------------------


def test_parse_rejects_a_tool_outside_the_allowlist() -> None:
    # browser_eval runs arbitrary JS. A recipe file that could call it turns
    # "drop a file in the workspace" into "run code in the tenant's browser".
    with pytest.raises(recipes.RecipeError, match="not allowed"):
        recipes.parse({"steps": [{"tool": "browser_eval", "args": {"js": "alert(1)"}}]})


def test_parse_rejects_file_reaching_tools() -> None:
    for tool in ("browser_upload_file", "browser_download"):
        with pytest.raises(recipes.RecipeError, match="not allowed"):
            recipes.parse({"steps": [{"tool": tool, "args": {}}]})


async def test_every_allowlisted_tool_actually_executes() -> None:
    # Was a grep of the executor's SOURCE for each tool name, which passes while
    # a branch does nothing at all — 12 of 17 branches were never run. Drive each
    # one for real and assert it reaches the driver.
    from src import agent_ops
    from src.snapshot import Element
    from tests.conftest import FakeDriver, make_manager

    snapshot = [
        Element(ref="e0", tag="input", type="text", name="Search"),
        Element(ref="e1", tag="button", type="submit", name="Go"),
    ]
    args_for: dict[str, dict[str, object]] = {
        "browser_open": {"url": "https://example.test/"},
        "browser_snapshot": {},
        "browser_click": {"ref": "e1"},
        "browser_type": {"ref": "e0", "text": "hello"},
        "browser_fill_form": {"fields": [{"ref": "e0", "value": "x", "kind": "text"}]},
        "browser_select_option": {"ref": "e0", "value": "x"},
        "browser_press_key": {"key": "Enter"},
        "browser_scroll": {"direction": "down", "amount": 100},
        "browser_wait_for": {"text": "done", "timeout_ms": 10},
        "browser_read": {},
        "browser_get_table": {},
        "browser_get_links": {},
        "browser_find": {"query": "Go"},
        "browser_login": {"portal_id": "p1"},
        "browser_back": {},
        "browser_forward": {},
        "browser_reload": {},
    }
    assert set(args_for) == set(recipes.ALLOWED_TOOLS), "update this table when the allowlist moves"

    unreached = []
    for tool, args in args_for.items():
        driver = FakeDriver(snapshot=list(snapshot))
        manager, _ = make_manager(driver)
        session = await manager.get_or_create("c1")
        before = driver.calls_made()
        await agent_ops._run_recipe_step(
            manager, "c1", session, tool, dict(args), {"p1": _portal()}
        )
        if driver.calls_made() == before:
            unreached.append(tool)
    assert not unreached, f"allowlisted but the branch touches no driver: {unreached}"


def _portal():
    from src.portal_creds import PortalCred

    return PortalCred(
        portal_id="p1", login_url="https://example.test/login", username="u", password="p"
    )


# --- structure a recipe must have --------------------------------------------


def test_parse_rejects_a_stored_ref() -> None:
    # A ref is a handle into ONE page render. Pasting a working tool call into a
    # recipe produces exactly this, and it skips resolve_target entirely.
    with pytest.raises(recipes.RecipeError, match="cannot store 'ref'"):
        recipes.parse({"steps": [{"tool": "browser_click", "args": {"ref": "e12"}}]})


def test_parse_rejects_an_unknown_target_key() -> None:
    # 'role' and 'placeholder' are in the snapshot an author reads, so reaching
    # for one is natural — and it used to be DROPPED, leaving a target that
    # matched every element on the page.
    with pytest.raises(recipes.RecipeError, match="unknown target key"):
        recipes.parse({"steps": [{"tool": "browser_click", "target": {"role": "button"}}]})


def test_parse_rejects_a_target_that_matches_on_nothing() -> None:
    with pytest.raises(recipes.RecipeError, match="at least one of"):
        recipes.parse({"steps": [{"tool": "browser_click", "target": {"nth": 0}}]})


def test_parse_rejects_a_boolean_nth() -> None:
    # isinstance(True, int) is True in Python, so this read as index 1 and
    # silently picked the SECOND match.
    with pytest.raises(recipes.RecipeError, match="non-negative integer"):
        recipes.parse({"steps": [{"tool": "browser_click", "target": {"tag": "a", "nth": True}}]})


def test_parse_rejects_a_negative_nth() -> None:
    with pytest.raises(recipes.RecipeError, match="non-negative integer"):
        recipes.parse({"steps": [{"tool": "browser_click", "target": {"tag": "a", "nth": -1}}]})


def test_parse_rejects_a_fill_form_with_no_fields() -> None:
    # Used to fill nothing, then click Submit, and report ok — a blank form
    # posted to a live site.
    with pytest.raises(recipes.RecipeError, match="non-empty 'fields'"):
        recipes.parse({"steps": [{"tool": "browser_fill_form", "args": {}}]})


def test_parse_rejects_fields_hidden_inside_args() -> None:
    # The shape you get by transcribing the MCP tool's own signature. The
    # executor overwrites args["fields"], so this silently filled nothing.
    with pytest.raises(recipes.RecipeError, match="not inside 'args'"):
        recipes.parse(
            {
                "steps": [
                    {
                        "tool": "browser_fill_form",
                        "args": {"fields": [{"ref": "e0", "value": "x"}]},
                    }
                ]
            }
        )


def test_parse_rejects_fields_on_a_tool_that_cannot_use_them() -> None:
    with pytest.raises(recipes.RecipeError, match="only browser_fill_form"):
        recipes.parse(
            {"steps": [{"tool": "browser_click", "fields": [{"target": {"tag": "input"}}]}]}
        )


def test_parse_validates_targets_inside_fill_form_fields() -> None:
    with pytest.raises(recipes.RecipeError, match="unknown target key"):
        recipes.parse(
            {
                "steps": [
                    {
                        "tool": "browser_fill_form",
                        "fields": [{"target": {"placeholder": "Name"}, "value": "x"}],
                    }
                ]
            }
        )


def test_resolve_target_refuses_a_boolean_nth_directly() -> None:
    # resolve_target is public and pure; parse is not its only caller.
    with pytest.raises(recipes.RecipeError, match="non-negative integer"):
        recipes.resolve_target(_snapshot(), {"tag": "a", "nth": True})


def test_extraction_tools_are_a_subset_of_allowed() -> None:
    assert recipes.EXTRACTION_TOOLS <= recipes.ALLOWED_TOOLS


# --- structure ---------------------------------------------------------------


def test_parse_rejects_an_empty_or_missing_step_list() -> None:
    for bad in ({}, {"steps": []}, {"steps": "open the page"}):
        with pytest.raises(recipes.RecipeError):
            recipes.parse(bad)


def test_parse_keeps_declared_params() -> None:
    recipe = recipes.parse(
        {"name": "search", "params": ["keyword"], "steps": [{"tool": "browser_snapshot"}]}
    )
    assert recipe["params"] == ["keyword"]
    assert recipe["name"] == "search"


def test_missing_params_is_reported_before_anything_runs() -> None:
    recipe = recipes.parse({"params": ["keyword"], "steps": [{"tool": "browser_snapshot"}]})
    assert recipes.missing_params(recipe, {}) == ["keyword"]
    assert recipes.missing_params(recipe, {"keyword": "CCTV"}) == []


# --- values ------------------------------------------------------------------


def test_substitute_resolves_a_param_and_passes_literals_through() -> None:
    assert recipes.substitute("param:keyword", {"keyword": "CCTV"}) == "CCTV"
    assert recipes.substitute("CCTV", {}) == "CCTV"
    assert recipes.substitute(20, {}) == 20


def test_an_unsupplied_param_raises_rather_than_becoming_empty() -> None:
    # Typing "" into a portal's search box returns EVERY row, which reads like a
    # successful run. Failing loudly is the only safe behaviour.
    with pytest.raises(recipes.RecipeError, match="keyword"):
        recipes.substitute("param:keyword", {})


def test_there_is_no_credential_value_source() -> None:
    # Recipes must never be able to hold or fetch a secret; logging in goes
    # through browser_login, which injects the password server-side. A
    # `credential:` string is therefore an inert literal, not a lookup.
    assert recipes.substitute("credential:portal.password", {}) == "credential:portal.password"
    assert recipes._PARAM_PREFIX == "param:"
    # `param:` is the ONLY dynamic source there is.
    assert not [n for n in dir(recipes) if n.endswith("_PREFIX") and n != "_PARAM_PREFIX"]


# --- targeting ---------------------------------------------------------------


def test_resolve_target_finds_the_current_ref() -> None:
    assert (
        recipes.resolve_target(_snapshot(), {"tag": "input", "type": "text", "name": "Username"})
        == "e0"
    )
    assert (
        recipes.resolve_target(_snapshot(), {"tag": "button", "type": "submit", "name": "Sign in"})
        == "e2"
    )


def test_resolve_target_raises_when_nothing_matches() -> None:
    # The page changed. That must surface, not fall back to something else.
    with pytest.raises(recipes.RecipeError, match="no element matches"):
        recipes.resolve_target(_snapshot(), {"tag": "button", "name": "Log in"})


def test_ambiguity_raises_unless_the_recipe_disambiguates() -> None:
    two = {"tag": "a", "name": "Next"}
    with pytest.raises(recipes.RecipeError, match="add 'nth'"):
        recipes.resolve_target(_snapshot(), two)
    assert recipes.resolve_target(_snapshot(), {**two, "nth": 0}) == "e4"
    assert recipes.resolve_target(_snapshot(), {**two, "nth": 1}) == "e5"


def test_out_of_range_nth_raises() -> None:
    with pytest.raises(recipes.RecipeError, match="out of range"):
        recipes.resolve_target(_snapshot(), {"tag": "a", "name": "Next", "nth": 9})


def test_a_partial_descriptor_still_matches() -> None:
    # Recipes should not have to spell out every attribute; name alone is a
    # legitimate descriptor when it is unique.
    assert recipes.resolve_target(_snapshot(), {"name": "Search"}) == "e3"

"""The browser_run_recipe MCP handler: what it refuses before it runs anything.

The handler is the only new tool that READS A FILE off the tenant volume, so its
path guard is a real boundary — and it had no test at all: deleting the guard
left the suite green.
"""

from __future__ import annotations

import json
import os

import pytest

from src import server


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point the handler's volume root at a temp dir, as a tenant PVC mount."""
    monkeypatch.setattr(server, "_AGENT_HOME", str(tmp_path))
    return tmp_path


async def test_a_path_outside_the_tenant_volume_is_refused(workspace) -> None:
    result = await server.browser_run_recipe("../../etc/passwd")
    assert result["status"] == "path_not_allowed"


async def test_an_absolute_path_outside_the_volume_is_refused(workspace) -> None:
    result = await server.browser_run_recipe("/etc/hosts")
    assert result["status"] == "path_not_allowed"


async def test_a_symlink_pointing_out_of_the_volume_is_refused(workspace) -> None:
    # realpath resolves the link before the prefix check, which is the whole
    # reason the guard uses realpath rather than normpath.
    outside = workspace.parent / "secret.json"
    outside.write_text('{"steps": [{"tool": "browser_read"}]}')
    link = workspace / "escape.recipe.json"
    os.symlink(outside, link)

    result = await server.browser_run_recipe("escape.recipe.json")
    assert result["status"] == "path_not_allowed"


async def test_a_missing_recipe_says_so(workspace) -> None:
    result = await server.browser_run_recipe("skills/nope.recipe.json")
    assert result["status"] == "not_found"


async def test_malformed_json_is_reported_not_raised(workspace) -> None:
    (workspace / "bad.recipe.json").write_text("{not json")
    result = await server.browser_run_recipe("bad.recipe.json")
    assert result["status"] == "invalid_recipe"
    assert "not valid JSON" in result["reason"]


async def test_a_recipe_calling_a_forbidden_tool_never_reaches_the_browser(workspace) -> None:
    # The allowlist has to bite at the HANDLER, before a session is opened —
    # this is the path a hostile file on the volume would actually take.
    (workspace / "evil.recipe.json").write_text(
        json.dumps({"steps": [{"tool": "browser_eval", "args": {"js": "fetch('//x')"}}]})
    )
    result = await server.browser_run_recipe("evil.recipe.json")
    assert result["status"] == "invalid_recipe"
    assert "not allowed" in result["reason"]


async def test_a_stored_ref_is_refused_at_the_handler(workspace) -> None:
    (workspace / "ref.recipe.json").write_text(
        json.dumps({"steps": [{"tool": "browser_click", "args": {"ref": "e12"}}]})
    )
    result = await server.browser_run_recipe("ref.recipe.json")
    assert result["status"] == "invalid_recipe"
    assert "ref" in result["reason"]

"""The MCP surface itself: the extension's tool names verbatim, the path
guards on every file-touching tool, and the handlers' refusals."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.fastmcp import Image
from mcp.server.fastmcp.exceptions import ToolError

from src import agent_ops, server
from tests.conftest import DEFAULT_TREE, FakeDriver, make_manager

EXTENSION_TOOLS = {
    "computer",
    "read_page",
    "get_page_text",
    "find",
    "form_input",
    "navigate",
    "javascript_tool",
    "read_console_messages",
    "read_network_requests",
    "browser_batch",
    "file_upload",
    "resize_window",
    "tabs_context_mcp",
    "tabs_create_mcp",
    "tabs_close_mcp",
}
EXTRAS = {
    "get_network_request",
    "login",
    "wait_for",
    "download",
    "list_frames",
    "switch_frame",
    "set_dialog_mode",
    "last_dialog",
    "run_recipe",
}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeDriver:
    driver = FakeDriver(DEFAULT_TREE)
    manager, _ = make_manager(driver)
    monkeypatch.setattr(server, "manager", manager)
    return driver


@pytest.fixture
def workspace(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the handlers' data root at a temp dir, as the mounted volume."""
    monkeypatch.setattr(server, "_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(server, "_ARTIFACT_DIR", str(tmp_path / ".cobrowse" / "artifacts"))
    return tmp_path


async def test_the_tool_list_is_the_extensions_plus_the_documented_extras() -> None:
    names = {t.name for t in await server.mcp.list_tools()}
    assert names == EXTENSION_TOOLS | EXTRAS
    assert not any(n.startswith("browser_") for n in names - {"browser_batch"})


async def test_every_extra_says_it_is_not_in_the_extension() -> None:
    for tool in await server.mcp.list_tools():
        if tool.name in EXTRAS - {"get_network_request"}:
            assert "Not in the extension" in " ".join((tool.description or "").split()), tool.name


async def test_navigate_tells_the_model_to_prefer_native_web_tools_and_that_sites_can_block() -> (
    None
):
    desc = next(t.description or "" for t in await server.mcp.list_tools() if t.name == "navigate")
    assert "WebSearch" in desc and "WebFetch" in desc
    assert "cannot reach every site" in desc
    assert "reason" in desc


async def test_batch_items_dispatch_by_bare_name_and_every_tool_but_batch_is_reachable() -> None:
    assert set(server._DISPATCH) == (EXTENSION_TOOLS | EXTRAS) - {"browser_batch"}


# --- path guards ---------------------------------------------------------------------------


def test_resolve_data_path_keeps_the_agent_inside_the_volume(workspace: Any) -> None:
    assert server._resolve_data_path("a/b.pdf") == str((workspace / "a" / "b.pdf").resolve())
    assert server._resolve_data_path("../../etc/passwd") is None
    assert server._resolve_data_path("/etc/hosts") is None


def test_the_credentials_mount_is_never_a_data_path(
    workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`login` keeps portal passwords server-side; a layout that mounts them
    under the data volume must not let `file_upload` attach them to a page."""
    creds = workspace / "creds"
    creds.mkdir()
    (creds / "PORTAL_CREDENTIALS_JSON").write_text("[]")
    monkeypatch.setattr(server, "_CREDS_ROOT", str(creds.resolve()))
    assert server._resolve_data_path("creds/PORTAL_CREDENTIALS_JSON") is None
    assert server._resolve_data_path(str(creds)) is None
    assert server._resolve_data_path("creds/../a.pdf") == str((workspace / "a.pdf").resolve())
    # A sibling whose name merely starts the same is data.
    assert server._resolve_data_path("creds-archive/x") is not None


async def test_file_upload_refuses_outside_missing_and_oversized(
    workspace: Any, fake: FakeDriver
) -> None:
    assert (await server.file_upload(1, "e5", ["/etc/hosts"]))["status"] == "path_not_allowed"
    assert (await server.file_upload(1, "e5", ["nope.pdf"]))["status"] == "not_found"
    big = workspace / "big.bin"
    big.write_bytes(b"x" * (server._UPLOAD_CAP + 1))
    assert (await server.file_upload(1, "e5", ["big.bin"]))["status"] == "too_large"
    ok = workspace / "doc.pdf"
    ok.write_bytes(b"pdf")
    out = await server.file_upload(1, "e5", ["doc.pdf"])
    assert out["status"] == "attached" and fake.uploads == [("e5", [str(ok.resolve())])]


async def test_download_refuses_a_path_outside_the_volume(workspace: Any, fake: FakeDriver) -> None:
    assert (await server.download(1, "e6", "/etc/x"))["status"] == "path_not_allowed"
    out = await server.download(1, "e6", "downloads/t.pdf")
    assert out["status"] == "downloaded" and out["path"].endswith("downloads/t.pdf")
    assert (workspace / "downloads").is_dir()


# --- a gone browser -----------------------------------------------------------------------


async def test_a_gone_browser_is_a_tool_error_the_agent_reads_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Leaves(FakeDriver):
        reason: str | None = None

        def gone(self) -> str | None:
            return self.reason

    driver = _Leaves(DEFAULT_TREE)
    manager, _ = make_manager(driver)
    monkeypatch.setattr(server, "manager", manager)
    await server.tabs_context_mcp()
    driver.reason = "User disconnected"
    with pytest.raises(ToolError, match="User disconnected"):
        await server.tabs_context_mcp()
    assert driver.closed is True


# --- get_network_request ------------------------------------------------------------------


async def test_get_network_request_needs_a_reason_and_can_write_to_a_named_path(
    workspace: Any, fake: FakeDriver
) -> None:
    idx = fake.record_network("GET", "https://x/api", 200, response_body="y" * 20_000)
    with pytest.raises(ToolError, match="reason"):
        await server.get_network_request(1, idx, "  ", part="response_body")
    assert (await server.get_network_request(1, idx, "r", path="/etc/x"))[
        "status"
    ] == "path_not_allowed"
    out = await server.get_network_request(
        1, idx, "the price", part="response_body", path="net/body.txt"
    )
    assert out["body"] is None and out["path"] == str((workspace / "net" / "body.txt").resolve())
    assert (workspace / "net" / "body.txt").read_text() == "y" * 20_000


# --- run_recipe -------------------------------------------------------------------------------


async def test_run_recipe_refuses_before_touching_the_browser(
    workspace: Any, fake: FakeDriver
) -> None:
    assert (await server.run_recipe("../../etc/passwd"))["status"] == "path_not_allowed"
    assert (await server.run_recipe("skills/nope.recipe.json"))["status"] == "not_found"
    (workspace / "bad.recipe.json").write_text("{not json")
    assert "not valid JSON" in (await server.run_recipe("bad.recipe.json"))["reason"]
    (workspace / "evil.recipe.json").write_text(
        json.dumps({"steps": [{"name": "javascript_tool", "input": {"text": "fetch('//x')"}}]})
    )
    evil = await server.run_recipe("evil.recipe.json")
    assert evil["status"] == "invalid_recipe" and "not allowed" in evil["reason"]
    (workspace / "ref.recipe.json").write_text(
        json.dumps(
            {"steps": [{"name": "computer", "input": {"action": "left_click", "ref": "e12"}}]}
        )
    )
    assert "ref" in (await server.run_recipe("ref.recipe.json"))["reason"]
    assert fake.clicks == [] and fake.evals == []


async def test_run_recipe_runs_a_stored_batch_on_the_active_tab(
    workspace: Any, fake: FakeDriver
) -> None:
    (workspace / "ok.recipe.json").write_text(
        json.dumps(
            {
                "params": ["q"],
                "steps": [
                    {"name": "navigate", "input": {"url": "https://x"}},
                    {"name": "read_page", "input": {}},
                    {
                        "name": "form_input",
                        "input": {"value": "param:q"},
                        "target": {"role": "searchbox"},
                    },
                    {"name": "get_page_text", "input": {}},
                ],
            }
        )
    )
    out = await server.run_recipe("ok.recipe.json", {"q": "pumps"})
    assert out["status"] == "ok" and out["steps_run"] == 4
    assert fake.opened == ["https://x"] and fake.form_inputs == [("e2", "pumps")]
    assert [e["tool"] for e in out["extracted"]] == ["read_page", "get_page_text"]


# --- browser_batch ------------------------------------------------------------------------------


async def test_browser_batch_flattens_text_and_images_in_order(fake: FakeDriver) -> None:
    content = await server.browser_batch(
        [
            {"name": "computer", "input": {"tabId": 1, "action": "left_click", "ref": "e4"}},
            {"name": "computer", "input": {"tabId": 1, "action": "screenshot"}},
            {"name": "tabs_context_mcp", "input": {}},
        ]
    )
    assert content[0] == "#1 computer:\nClicked e4"
    assert content[1].startswith("#2 computer: Screenshot:")
    assert isinstance(content[2], Image)
    assert content[3].startswith("#3 tabs_context_mcp: {")


async def test_browser_batch_reports_the_failing_item_and_stops(fake: FakeDriver) -> None:
    content = await server.browser_batch(
        [
            {"name": "read_page", "input": {"tabId": 9}},
            {"name": "read_page", "input": {"tabId": 1}},
        ]
    )
    assert len(content) == 1
    assert content[0].startswith("#1 read_page: ERROR — No tab 9 is open")
    with pytest.raises(ToolError):
        await server.browser_batch([])


async def test_browser_batch_waits_for_a_navigation_after_an_acting_item(fake: FakeDriver) -> None:
    """Between items, an item that could have started a navigation (a click)
    gets the driver's navigation wait before the next item runs; a read or a
    view-only action does not, and nothing waits after the last item."""
    await server.browser_batch(
        [
            {"name": "computer", "input": {"tabId": 1, "action": "left_click", "ref": "e4"}},
            {"name": "read_page", "input": {"tabId": 1}},
            {"name": "computer", "input": {"tabId": 1, "action": "screenshot"}},
            {"name": "read_page", "input": {"tabId": 1}},
            {"name": "computer", "input": {"tabId": 1, "action": "key", "text": "Return"}},
        ]
    )
    assert fake.nav_waits == [(agent_ops.BATCH_NAV_WINDOW_S, agent_ops.BATCH_NAV_LOAD_TIMEOUT_MS)]


async def test_text_tools_return_plain_text_not_a_structured_result(fake: FakeDriver) -> None:
    """A tool that returns text returns a TEXT block. With FastMCP's default a
    ``str`` return also becomes ``structuredContent {"result": …}`` and the
    client hands the model that JSON — the whole tree quoted and escaped on
    every read (the parity cut's acceptance run). The dict-returning tools keep
    their structured shape; a tool that MAY carry a page (``read``) is a text
    tool for the same reason."""
    tools = {t.name: t for t in await server.mcp.list_tools()}
    for name in ("read_page", "get_page_text", "find", "form_input", "navigate", "wait_for"):
        assert tools[name].outputSchema is None, name
    assert tools["tabs_context_mcp"].outputSchema is not None
    result = await server.mcp.call_tool("get_page_text", {"tabId": 1})
    assert isinstance(result, list)  # content blocks only — no (content, structured) pair
    assert result[0].text.startswith("Title: ")  # type: ignore[union-attr]
    assert not result[0].text.startswith("{")  # type: ignore[union-attr]


async def test_domain_errors_become_tool_errors(fake: FakeDriver) -> None:
    with pytest.raises(ToolError, match="No tab 4"):
        await server.read_page(4)
    with pytest.raises(ToolError, match="unknown computer action"):
        await server.computer("teleport", 1)
    with pytest.raises(ToolError, match="filter must be"):
        await server.read_page(1, filter="some")


async def test_computer_returns_text_or_text_plus_image(fake: FakeDriver) -> None:
    assert await server.computer("left_click", 1, ref="e1") == "Clicked e1"
    shot = await server.computer("screenshot", 1)
    assert isinstance(shot, list) and isinstance(shot[1], Image)


# --- read: the page in the same reply ---------------------------------------------------

READ_TOOLS = {"navigate", "computer", "form_input", "wait_for", "switch_frame"}


async def test_exactly_the_acting_tools_take_read_and_every_one_documents_it() -> None:
    tools = {t.name: t for t in await server.mcp.list_tools()}
    takes_read = {n for n, t in tools.items() if "read" in t.inputSchema.get("properties", {})}
    assert takes_read == READ_TOOLS
    for name in READ_TOOLS:
        desc = " ".join((tools[name].description or "").split())
        assert "SAME reply" in desc, name  # the decorator ran BEFORE registration
        assert '"interactive"' in desc and '"text"' in desc, name
    assert not tools["navigate"].inputSchema.get("required", []) or "tabId" not in tools[
        "navigate"
    ].inputSchema.get("required", [])


async def test_navigate_read_renders_the_json_line_then_the_page_as_text(
    fake: FakeDriver,
) -> None:
    bare = await server.navigate("https://x.com/a")
    assert bare == {
        "tabId": 1,
        "url": "https://x.com/a",
        "title": "Fake",
        "loaded": True,
        "page_state": "ok",
        "changes": [],
    }
    result = await server.mcp.call_tool("navigate", {"url": "https://x.com/a", "read": "text"})
    assert isinstance(result, list)  # content only — never a structured pair
    text = result[0].text  # type: ignore[union-attr]
    head, page = text.split("\n\n", 1)
    assert json.loads(head)["page_state"] == "ok" and "page" not in json.loads(head)
    assert page.startswith("Title: Fake\nURL: https://x.com/a\n")


async def test_read_rides_a_batch_item_and_is_refused_by_name(fake: FakeDriver) -> None:
    out = await server.browser_batch(
        [
            {"name": "mcp__browser__form_input", "input": {"tabId": 1, "ref": "e2", "value": "x"}},
            {
                "name": "computer",
                "input": {"tabId": 1, "action": "key", "text": "Return", "read": "interactive"},
            },
        ]
    )
    assert out[0] == "#1 form_input:\nSet e2 to 'x'"
    assert out[1].startswith("#2 computer:\nPressed Return\n\n  - link")
    with pytest.raises(ToolError, match="read must be one of"):
        await server.computer("left_click", 1, ref="e1", read="tree")

"""The agent surface, tool by tool, against the FakeDriver.

The contract is the extension's: every per-tab tool names its tab and
activates it; a mutating action replies with what it did plus only what
changed; reads are capped at a line boundary; every result is scrubbed of
the secrets `login` injected.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src import agent_ops
from src.agent_ops import AgentPaused, ToolInputError
from src.browser_driver import UnknownTabError
from src.find_model import FindConfig
from src.portal_creds import PortalCred
from src.refs_tree import REDACTED
from tests.conftest import DEFAULT_TREE, FakeDriver, make_manager

_NO_MODEL = FindConfig(url="", key="", model="m")


def _setup(tree: str = DEFAULT_TREE) -> tuple[FakeDriver, Any]:
    driver = FakeDriver(tree)
    manager, _ = make_manager(driver)
    return driver, manager


# --- tabs -------------------------------------------------------------------------


async def test_tabs_context_lists_numeric_ids() -> None:
    _driver, manager = _setup()
    out = await agent_ops.tabs_context(manager, "c1")
    assert out == {
        "tabs": [
            {"tabId": 1, "url": "about:blank", "title": "Fake", "active": True, "loaded": True}
        ]
    }


async def test_tabs_create_and_close_broadcast_the_strip() -> None:
    driver, manager = _setup()
    session = await manager.get_or_create("c1")
    sent: list[dict[str, Any]] = []

    async def sink(f: dict[str, Any]) -> None:
        sent.append(f)

    session.add_viewer_sink(sink)
    created = await agent_ops.tabs_create(manager, "c1")
    assert created == {"tabId": 2, "url": "about:blank"}
    assert driver.active_num() == 2
    closed = await agent_ops.tabs_close(manager, "c1", 2)
    assert closed["status"] == "closed" and [t["tabId"] for t in closed["tabs"]] == [1]
    assert (await agent_ops.tabs_close(manager, "c1", 9))["status"] == "unknown_tab"
    assert sum(1 for f in sent if f["type"] == "browser_tabs") >= 2


async def test_every_per_tab_tool_activates_the_tab_it_names() -> None:
    driver, manager = _setup()
    await driver.new_tab()  # tab 2 active
    await agent_ops.read_page(manager, "c1", 1)
    assert driver.active_num() == 1
    assert driver.activations[-1] == 1


async def test_an_unknown_tab_is_refused_with_the_corrective_call() -> None:
    _driver, manager = _setup()
    with pytest.raises(UnknownTabError, match=r"No tab 7 is open.*tabs_context_mcp"):
        await agent_ops.read_page(manager, "c1", 7)


# --- navigate ---------------------------------------------------------------------


async def test_navigate_returns_the_landed_state_and_names_a_wall() -> None:
    driver, manager = _setup()
    out = await agent_ops.navigate(manager, "c1", "https://x.com/a", 1)
    assert driver.opened == ["https://x.com/a"]
    assert out["url"] == "https://x.com/a" and out["page_state"] == "ok" and out["tabId"] == 1
    driver.page_state = "blocked_challenge"
    assert (await agent_ops.navigate(manager, "c1", "https://x.com/b", 1))[
        "page_state"
    ] == "blocked_challenge"


async def test_navigate_back_forward_walk_history() -> None:
    driver, manager = _setup()
    await agent_ops.navigate(manager, "c1", "back", 1)
    await agent_ops.navigate(manager, "c1", "forward", 1)
    assert driver.nav_ops == ["back", "forward"]
    with pytest.raises(ToolInputError):
        await agent_ops.navigate(manager, "c1", "  ", 1)


async def test_navigate_resumes_a_paused_agent() -> None:
    _driver, manager = _setup()
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    sent: list[dict[str, Any]] = []

    async def sink(f: dict[str, Any]) -> None:
        sent.append(f)

    session.add_viewer_sink(sink)
    await agent_ops.navigate(manager, "c1", "https://x.com", 1)
    assert session.agent_paused is False
    assert any(f["type"] == "browser_agent_state" and f["state"] == "idle" for f in sent)


async def test_navigate_carries_dialogs_and_new_tabs_but_not_its_own_navigation() -> None:
    driver, manager = _setup()
    driver.changes = ['Page navigated to https://x.com — "X"', 'Dialog alert "hi" was dismissed']
    out = await agent_ops.navigate(manager, "c1", "https://x.com", 1)
    assert out["changes"] == ['Dialog alert "hi" was dismissed']


# --- read_page / get_page_text -------------------------------------------------------


async def test_read_page_defaults_to_interactive_nodes_with_a_page_trailer() -> None:
    driver, manager = _setup()
    await driver.open("https://x.com/p")
    out = await agent_ops.read_page(manager, "c1", 1)
    assert out.startswith('  - link "Home" [ref=e1]')
    assert "text: Some prose" not in out
    assert out.endswith('Page: https://x.com/p — "Fake" (tab 1)')


async def test_read_page_all_keeps_text_and_passes_options_to_the_driver() -> None:
    driver, manager = _setup()
    out = await agent_ops.read_page(
        manager, "c1", 1, filter="all", depth=2, ref_id="e1", boxes=True
    )
    assert "text: Some prose" in out
    assert driver.read_pages == [{"depth": 2, "boxes": True, "ref_id": "e1"}]


async def test_read_page_cuts_at_a_line_boundary_and_states_the_full_size() -> None:
    _driver, manager = _setup()
    out = await agent_ops.read_page(manager, "c1", 1, max_chars=60)
    body = out.split("\n\nPage:")[0]
    assert "[truncated at a line boundary" in out
    assert body.startswith('  - link "Home" [ref=e1]')
    assert "of 1" in out  # "showing N of 1xx chars"


async def test_get_page_text_has_the_header_and_cap() -> None:
    driver, manager = _setup()
    driver.page_text_value = "hello\nworld"
    await driver.open("https://x.com/p")
    out = await agent_ops.get_page_text(manager, "c1", 1)
    assert out.startswith("Title: Fake\nURL: https://x.com/p\nSource element: main\n\nhello\nworld")
    driver.page_text_value = "\n".join("line" for _ in range(20000))
    assert "[truncated at a line boundary" in await agent_ops.get_page_text(manager, "c1", 1)


# --- find ---------------------------------------------------------------------------


async def test_find_literal_tier_answers_without_the_model() -> None:
    _driver, manager = _setup()
    out = await agent_ops.find(manager, "c1", 1, "sign in", config=_NO_MODEL)
    assert out.startswith("1 match(es), source: literal")
    assert "\ne4: " in out


async def test_find_without_a_model_tier_says_so_on_a_miss() -> None:
    _driver, manager = _setup()
    out = await agent_ops.find(manager, "c1", 1, "checkout", config=_NO_MODEL)
    assert "no model tier is configured" in out
    with pytest.raises(ToolInputError):
        await agent_ops.find(manager, "c1", 1, "  ", config=_NO_MODEL)


async def test_find_falls_to_the_model_tier_on_a_miss() -> None:
    _driver, manager = _setup()

    class _Resp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {
                "content": [{"type": "text", "text": "e4: the sign-in control\ne77: not real"}],
                "usage": {},
            }

    class _Client:
        async def post(self, url: str, *, json: Any, headers: Any) -> _Resp:
            return _Resp()

    config = FindConfig(url="https://gw", key="k", model="m")
    out = await agent_ops.find(manager, "c1", 1, "log me in", config=config, client=_Client())  # type: ignore[arg-type]
    assert "source: model" in out
    assert 'e4: - button "Sign in"' in out and "the sign-in control" in out
    assert "e77" not in out


async def test_find_explains_a_failed_model_tier_instead_of_no_match() -> None:
    _driver, manager = _setup()

    class _Resp:
        status_code = 429

        def json(self) -> dict[str, Any]:
            return {"error": {"message": "temporarily rate-limited upstream"}}

    class _Client:
        async def post(self, url: str, *, json: Any, headers: Any) -> _Resp:
            return _Resp()

    config = FindConfig(url="https://gw", key="k", model="m")
    out = await agent_ops.find(manager, "c1", 1, "log me in", config=config, client=_Client())  # type: ignore[arg-type]
    assert out.startswith("No literal match, and the model tier failed: m is rate-limited")
    assert "temporarily rate-limited upstream" in out
    assert "read_page" in out


# --- computer -----------------------------------------------------------------------


async def test_computer_click_by_ref_reports_what_changed() -> None:
    driver, manager = _setup()
    driver.changes = ['Page navigated to https://x.com/next — "Next"']
    out = await agent_ops.computer(manager, "c1", 1, "left_click", ref="e4")
    assert out["text"] == 'Clicked e4\nPage navigated to https://x.com/next — "Next"'
    assert driver.clicks == [
        {"ref": "e4", "coordinate": None, "button": "left", "count": 1, "modifiers": []}
    ]
    assert driver.settled == 1


async def test_computer_click_variants_and_modifiers() -> None:
    driver, manager = _setup()
    await agent_ops.computer(manager, "c1", 1, "right_click", coordinate=[10, 20])
    await agent_ops.computer(manager, "c1", 1, "double_click", ref="e1", modifiers=["ctrl"])
    await agent_ops.computer(manager, "c1", 1, "triple_click", ref="e2")
    assert driver.clicks[0]["button"] == "right" and driver.clicks[0]["coordinate"] == (10, 20)
    assert driver.clicks[1]["count"] == 2 and driver.clicks[1]["modifiers"] == ["Control"]
    assert driver.clicks[2]["count"] == 3
    with pytest.raises(ValueError, match="unknown modifier"):
        await agent_ops.computer(manager, "c1", 1, "left_click", ref="e1", modifiers=["hyper"])


async def test_computer_type_key_scroll_hover_drag_wait() -> None:
    driver, manager = _setup()
    driver.type_note = "ok — note: the field now contains '09/15', not the text you typed"
    typed = await agent_ops.computer(manager, "c1", 1, "type", text="hello", ref="e2")
    assert typed["text"].startswith("Typed 5 chars into e2 — note: the field now contains")
    assert driver.typed == [("hello", "e2")]
    keyed = await agent_ops.computer(manager, "c1", 1, "key", text="ctrl+a", repeat=2)
    assert keyed["text"] == "Pressed ctrl+a x2" and driver.keys == [("ctrl+a", 2)]
    await agent_ops.computer(
        manager, "c1", 1, "scroll", scroll_direction="up", scroll_amount=3, coordinate=[5, 5]
    )
    assert driver.scrolls == [{"direction": "up", "amount": 3, "coordinate": (5, 5), "ref": None}]
    assert (await agent_ops.computer(manager, "c1", 1, "scroll_to", ref="e4"))[
        "text"
    ] == "Scrolled e4 into view"
    await agent_ops.computer(manager, "c1", 1, "hover", ref="e1")
    assert driver.hovers == [{"ref": "e1", "coordinate": None}]
    await agent_ops.computer(
        manager, "c1", 1, "left_click_drag", start_coordinate=[1, 2], coordinate=[3, 4]
    )
    assert driver.drags == [((1, 2), (3, 4))]
    waited = await agent_ops.computer(manager, "c1", 1, "wait", duration=2)
    assert waited["text"] == "Waited 2s" and driver.waits == [2.0]


async def test_computer_screenshot_and_zoom_return_the_png_and_its_frame() -> None:
    driver, manager = _setup()
    shot = await agent_ops.computer(manager, "c1", 1, "screenshot")
    assert shot["png"] == driver.screenshot_png
    assert shot["text"].startswith(
        "Screenshot: image 1280x800 px covers page region x=0..1280, y=0..800"
    )
    zoom = await agent_ops.computer(manager, "c1", 1, "zoom", region=[10, 20, 110, 70])
    assert driver.screenshots[-1] == {"scale": 2.0, "region": (10, 20, 110, 70)}
    assert "image 200x100 px covers page region x=10..110, y=20..70" in zoom["text"]
    assert "2.00 image px per CSS px" in zoom["text"]
    small = await agent_ops.computer(manager, "c1", 1, "screenshot", scale=0.5)
    assert "image 640x400" in small["text"]


@pytest.mark.parametrize(
    ("action", "kw", "message"),
    [
        ("teleport", {}, "unknown computer action"),
        ("zoom", {}, "zoom needs a region"),
        ("zoom", {"region": [5, 5, 1, 1]}, "x1 > x0"),
        ("screenshot", {"scale": 9}, "between 0.1 and 2.0"),
        ("left_click", {}, "needs a ref or a coordinate"),
        ("type", {}, "type needs text"),
        ("key", {"text": " "}, "key needs text"),
        ("scroll_to", {}, "scroll_to needs a ref"),
        ("left_click_drag", {"coordinate": [1, 1]}, "needs start_coordinate"),
        ("left_click", {"coordinate": "x"}, "must be \\[x, y\\]"),
    ],
)
async def test_computer_rejects_bad_input_by_name(
    action: str, kw: dict[str, Any], message: str
) -> None:
    _driver, manager = _setup()
    with pytest.raises((ToolInputError, ValueError), match=message):
        await agent_ops.computer(manager, "c1", 1, action, **kw)


async def test_computer_honours_the_humans_pause_for_acting_not_viewing() -> None:
    driver, manager = _setup()
    (await manager.get_or_create("c1")).agent_paused = True
    await agent_ops.computer(manager, "c1", 1, "screenshot")  # viewing is fine
    await agent_ops.read_page(manager, "c1", 1)
    with pytest.raises(AgentPaused):
        await agent_ops.computer(manager, "c1", 1, "left_click", ref="e4")
    with pytest.raises(AgentPaused):
        await agent_ops.computer(manager, "c1", 1, "scroll", scroll_direction="down")
    assert driver.clicks == [] and driver.scrolls == []


# --- forms / scripts / files ------------------------------------------------------------


async def test_form_input_reports_the_value_and_a_readback_note() -> None:
    driver, manager = _setup()
    assert await agent_ops.form_input(manager, "c1", 1, "e2", "pumps") == "Set e2 to 'pumps'"
    assert await agent_ops.form_input(manager, "c1", 1, "e3", True) == "Set e3 to True"
    driver.form_note = "ok — note: the field now contains '1', not the text you typed"
    assert "note: the field now contains" in await agent_ops.form_input(manager, "c1", 1, "e2", 12)
    assert driver.form_inputs == [("e2", "pumps"), ("e3", True), ("e2", 12)]
    with pytest.raises(ToolInputError):
        await agent_ops.form_input(manager, "c1", 1, "", "x")


async def test_javascript_is_pause_gated_and_returns_result_or_error() -> None:
    driver, manager = _setup()
    driver.eval_result = {"result": "42"}
    assert await agent_ops.javascript(manager, "c1", 1, "6*7") == {"result": "42"}
    driver.eval_result = {"error": "boom"}
    assert await agent_ops.javascript(manager, "c1", 1, "x") == {"error": "boom"}
    (await manager.get_or_create("c1")).agent_paused = True
    with pytest.raises(AgentPaused):
        await agent_ops.javascript(manager, "c1", 1, "1")


async def test_upload_download_resize_reach_the_driver() -> None:
    driver, manager = _setup()
    assert await agent_ops.upload(manager, "c1", 1, "e5", ["/w/a.pdf"]) == {
        "status": "attached",
        "paths": ["/w/a.pdf"],
    }
    assert (await agent_ops.download(manager, "c1", 1, "e6", "/w/out.pdf"))[
        "filename"
    ] == "report.pdf"
    assert await agent_ops.resize(manager, "c1", 1, 900, 600) == {
        "status": "resized",
        "width": 900,
        "height": 600,
    }
    assert driver.uploads == [("e5", ["/w/a.pdf"])] and driver.downloads == [("e6", "/w/out.pdf")]
    assert driver.viewports == [(900, 600)]


# --- observability --------------------------------------------------------------------


async def test_console_and_network_lists() -> None:
    driver, manager = _setup()
    driver.record_console("log", "hello")
    driver.record_console("error", "TypeError: boom")
    errors = await agent_ops.console_messages(
        manager, "c1", 1, pattern=None, only_errors=True, limit=10, clear=False
    )
    assert errors == [{"type": "error", "text": "TypeError: boom"}]
    driver.record_network("GET", "https://x.com/api/items", 200, response_body='{"a": 1}')
    driver.record_network("POST", "https://x.com/api/save", 0)
    listed = await agent_ops.network_requests(
        manager, "c1", 1, url_pattern="save", limit=10, clear=False
    )
    assert listed == [
        {
            "index": 2,
            "method": "POST",
            "url": "https://x.com/api/save",
            "status": 0,
            "resource_type": "xhr",
            "size": 0,
        }
    ]
    assert "response_body" not in listed[0]


async def test_network_request_headers_are_redacted_unless_raw_and_bodies_are_tagged() -> None:
    driver, manager = _setup()
    idx = driver.record_network(
        "GET",
        "https://x.com/api/price",
        200,
        response_body='{"price": 12.5}',
        request_headers={"Cookie": "sid=1", "Accept": "*/*"},
    )
    head = await agent_ops.network_request(
        manager, "c1", 1, idx, part=None, raw_headers=False, reason="price"
    )
    assert head["request_headers"] == {"Cookie": "<redacted>", "Accept": "*/*"}
    assert head["source"] == "network" and "body" not in head
    raw = await agent_ops.network_request(
        manager, "c1", 1, idx, part=None, raw_headers=True, reason="price"
    )
    assert raw["request_headers"]["Cookie"] == "sid=1"
    body = await agent_ops.network_request(
        manager, "c1", 1, idx, part="response_body", raw_headers=False, reason="price"
    )
    assert body["body"] == '{"price": 12.5}' and body["source"] == "network"
    assert (
        await agent_ops.network_request(
            manager, "c1", 1, 99, part=None, raw_headers=False, reason="x"
        )
    )["status"] == "unknown_index"
    with pytest.raises(ToolInputError):
        await agent_ops.network_request(
            manager, "c1", 1, idx, part="cookies", raw_headers=False, reason="x"
        )


async def test_network_request_big_body_goes_to_a_file() -> None:
    driver, manager = _setup()
    idx = driver.record_network("GET", "https://x.com/big", 200, response_body="x" * 20_000)
    written: list[str] = []

    async def write_file(text: str) -> str:
        written.append(text)
        return "/w/.cobrowse/artifacts/network.txt"

    out = await agent_ops.network_request(
        manager,
        "c1",
        1,
        idx,
        part="response_body",
        raw_headers=False,
        reason="r",
        write_file=write_file,
    )
    assert out["body"] is None and out["path"] == "/w/.cobrowse/artifacts/network.txt"
    assert len(written[0]) == 20_000


async def test_network_request_without_a_captured_body_says_why() -> None:
    driver, manager = _setup()
    idx = driver.record_network("GET", "https://x.com/img.png", 200)
    out = await agent_ops.network_request(
        manager, "c1", 1, idx, part="response_body", raw_headers=False, reason="r"
    )
    assert out["body"] is None and "no response body was captured" in out["note"]


# --- extras -----------------------------------------------------------------------------


async def test_wait_for_passes_every_condition_through() -> None:
    driver, manager = _setup()
    assert await agent_ops.wait_for(manager, "c1", 1, text="Done", timeout_ms=500) == {
        "ready": True
    }
    driver.wait_for_result = False
    assert await agent_ops.wait_for(
        manager, "c1", 1, url="/thanks", response="/api/save", timeout_ms=100
    ) == {"ready": False}
    assert driver.waited[-1] == {
        "text": None,
        "selector": None,
        "url": "/thanks",
        "response": "/api/save",
        "timeout_ms": 100,
    }


async def test_frames_and_dialogs() -> None:
    driver, manager = _setup()
    driver._frames["t1"].append({"index": 1, "name": "login", "url": "https://idp/x"})
    assert len(await agent_ops.list_frames(manager, "c1", 1)) == 2
    assert (await agent_ops.switch_frame(manager, "c1", 1, "login"))["status"] == "switched"
    assert (await agent_ops.switch_frame(manager, "c1", 1, "nope"))["status"] == "unknown_frame"
    assert (await agent_ops.switch_frame(manager, "c1", 1, ""))["status"] == "reset"
    assert await agent_ops.last_dialog(manager, "c1", 1) == {"status": "none"}
    assert (await agent_ops.set_dialog_mode(manager, "c1", "accept"))["mode"] == "accept"
    assert (await agent_ops.set_dialog_mode(manager, "c1", "typo"))["mode"] == "dismiss"
    await driver.fire_dialog("confirm", "Delete?")
    handled = await agent_ops.last_dialog(manager, "c1", 1)
    assert handled["status"] == "handled" and handled["type"] == "confirm" and "seq" not in handled


# --- login + outbound redaction ------------------------------------------------------


async def test_login_scrubs_the_injected_secret_from_every_later_result() -> None:
    driver, manager = _setup()
    portals = {"acme": PortalCred("acme", "https://acme/login", "user1", "s3cret!!")}
    result = await agent_ops.login(manager, "c1", "acme", portals)
    assert result["status"] == "submitted" and "s3cret!!" not in json.dumps(result)
    # The page later prints the password (a tree line, the page text, a
    # request body): every one is blanked before it reaches the agent.
    driver.tree = DEFAULT_TREE + '  - textbox "Password" [ref=e3]: s3cret!!\n'
    assert REDACTED in await agent_ops.read_page(manager, "c1", 1, filter="all")
    driver.page_text_value = "your password is s3cret!!"
    assert "s3cret!!" not in await agent_ops.get_page_text(manager, "c1", 1)
    idx = driver.record_network(
        "POST", "https://acme/login", 200, request_body="user=user1&pw=s3cret!!"
    )
    body = await agent_ops.network_request(
        manager, "c1", 1, idx, part="request_body", raw_headers=False, reason="r"
    )
    assert body["body"] == f"user=user1&pw={REDACTED}"
    driver.eval_result = {"result": "s3cret!!"}
    assert (await agent_ops.javascript(manager, "c1", 1, "x"))["result"] == REDACTED


async def test_login_on_a_named_tab_activates_it_first() -> None:
    driver, manager = _setup()
    await driver.new_tab()
    portals = {"acme": PortalCred("acme", "https://acme/", "u", "p4ssword")}
    await agent_ops.login(manager, "c1", "acme", portals, ref="e2", tab_id=1)
    assert driver.active_num() == 1 and driver.logins_at == [("e2", "u", "p4ssword")]


# --- read: act and look in ONE reply ---------------------------------------------------
#
# Under a large harness prompt every extra round trip re-reads the prompt, so
# "click, then read" costing two turns was the single biggest line in the
# measured runs (a saved turn was worth more than the page it fetched). An
# acting call that hands the page back collapses them.


async def test_navigate_without_a_tab_uses_the_active_one_and_says_which() -> None:
    driver, manager = _setup()
    await driver.new_tab()  # tab 2 is now active
    out = await agent_ops.navigate(manager, "c1", "https://x.com/a", None)
    assert out["tabId"] == 2 and driver.opened == ["https://x.com/a"]
    assert "page" not in out  # nothing asked for, nothing added


async def test_navigate_read_carries_the_page_in_the_asked_shape() -> None:
    driver, manager = _setup()
    driver.page_text_value = "Some prose on the page"
    tree = await agent_ops.navigate(manager, "c1", "https://x.com/a", 1, read="interactive")
    assert tree["page_state"] == "ok"
    assert tree["page"].startswith('  - link "Home" [ref=e1]')
    assert "text: Some prose" not in tree["page"]
    assert tree["page"].endswith('Page: https://x.com/a — "Fake" (tab 1)')
    whole = await agent_ops.navigate(manager, "c1", "https://x.com/a", 1, read="all")
    assert "text: Some prose" in whole["page"]
    text = await agent_ops.navigate(manager, "c1", "https://x.com/a", 1, read="text")
    assert text["page"].startswith("Title: Fake\nURL: https://x.com/a\n")
    assert text["page"].endswith("Some prose on the page")


async def test_read_is_checked_by_name_before_anything_acts() -> None:
    driver, manager = _setup()
    with pytest.raises(ToolInputError, match="read must be one of interactive, all, text"):
        await agent_ops.navigate(manager, "c1", "https://x.com/a", 1, read="html")
    assert driver.opened == []
    with pytest.raises(ToolInputError, match="read must be"):
        await agent_ops.computer(manager, "c1", 1, "left_click", ref="e4", read="page")
    assert driver.clicks == []


async def test_computer_read_appends_the_page_after_what_changed() -> None:
    driver, manager = _setup()
    driver.changes = ['Page navigated to https://x.com/next — "Next"']
    out = await agent_ops.computer(manager, "c1", 1, "left_click", ref="e4", read="interactive")
    did, page = out["text"].split("\n\n", 1)
    assert did == 'Clicked e4\nPage navigated to https://x.com/next — "Next"'
    assert page.startswith('  - link "Home" [ref=e1]')
    # The page is read AFTER the action settled, never before.
    assert driver.settled == 1 and len(driver.read_pages) == 1
    shot = await agent_ops.computer(manager, "c1", 1, "screenshot", read="text")
    assert shot["png"] is not None and "\n\nTitle: Fake" in shot["text"]


async def test_form_input_wait_for_and_switch_frame_take_read_too() -> None:
    _driver, manager = _setup()
    filled = await agent_ops.form_input(manager, "c1", 1, "e2", "pumps", read="interactive")
    assert filled.startswith("Set e2 to 'pumps'\n\n  - link")
    waited = await agent_ops.wait_for(manager, "c1", 1, text="Done", read="text")
    assert waited["ready"] is True and waited["page"].startswith("Title: ")
    switched = await agent_ops.switch_frame(manager, "c1", 1, "main", read="interactive")
    assert switched["status"] == "reset" and switched["page"].startswith("  - link")
    assert "page" not in await agent_ops.wait_for(manager, "c1", 1, text="Done")


async def test_the_page_an_action_hands_back_is_redacted_like_any_read() -> None:
    driver, manager = _setup()
    portals = {"acme": PortalCred("acme", "https://acme/", "user1", "s3cret!!")}
    await agent_ops.login(manager, "c1", "acme", portals)
    driver.tree = DEFAULT_TREE + '  - textbox "Password" [ref=e3]: s3cret!!\n'
    driver.page_text_value = "your password is s3cret!!"
    clicked = await agent_ops.computer(manager, "c1", 1, "left_click", ref="e4", read="all")
    assert "s3cret!!" not in clicked["text"] and REDACTED in clicked["text"]
    landed = await agent_ops.navigate(manager, "c1", "https://acme/home", 1, read="text")
    assert "s3cret!!" not in landed["page"] and REDACTED in landed["page"]

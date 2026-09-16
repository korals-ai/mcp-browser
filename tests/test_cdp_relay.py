"""The CDP relay: the Playwright Extension's protocol v2, translated for our driver.

Two layers, two harnesses. :class:`ExtensionBridge` is driven with a scripted
fake extension (no sockets): the handshake, the tab model, session routing,
the client-root aliasing that ``context.new_cdp_session`` needs, and the
disconnect paths. :class:`CdpRelay` runs on real loopback WebSockets so the
two-endpoint server, its refusals and the connect URL are exercised as the
extension will see them.

Every message shape here is copied from upstream's ``relayConnection.ts`` /
``cdpRelay.ts`` — the extension is unmodified upstream code, so the fake must
speak exactly what it speaks.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from src.cdp_relay import PROTOCOL_VERSION, CdpRelay, ExtensionBridge, RelayError


class FakeExtension:
    """Answers the five chrome.* commands the way the extension does and
    records everything the relay asked it."""

    def __init__(self, bridge_getter: Any) -> None:
        self.commands: list[dict[str, Any]] = []
        self.cdp_out: list[dict[str, Any]] = []
        self._bridge_getter = bridge_getter
        self.fail_next: str | None = None

    async def send_to_extension(self, msg: dict[str, Any]) -> None:
        self.commands.append(msg)
        bridge = self._bridge_getter()
        method, params = msg["method"], msg["params"]
        if self.fail_next:
            err, self.fail_next = self.fail_next, None
            await bridge.on_extension_message({"id": msg["id"], "error": err})
            return
        result: Any = {}
        if method == "chrome.debugger.sendCommand" and params[1] == "Target.getTargetInfo":
            tab_id = params[0]["tabId"]
            result = {"targetInfo": {"targetId": f"T{tab_id}", "type": "page", "url": "u"}}
        elif method == "chrome.debugger.sendCommand":
            result = {"echo": params[1], "debuggee": params[0]}
        elif method == "chrome.tabs.create":
            result = {"id": 99, "url": params[0]["url"]}
        # The extension answers asynchronously; so does this.
        asyncio.get_running_loop().call_soon(
            lambda: asyncio.ensure_future(
                bridge.on_extension_message({"id": msg["id"], "result": result})
            )
        )

    async def send_to_cdp(self, msg: dict[str, Any]) -> None:
        self.cdp_out.append(msg)


def _make() -> tuple[ExtensionBridge, FakeExtension]:
    holder: dict[str, ExtensionBridge] = {}
    ext = FakeExtension(lambda: holder["b"])
    bridge = ExtensionBridge(ext.send_to_extension, ext.send_to_cdp)
    holder["b"] = bridge
    return bridge, ext


async def _handshake(bridge: ExtensionBridge, *tab_ids: int) -> None:
    for tid in tab_ids:
        await bridge.on_extension_message(
            {"method": "chrome.tabs.onCreated", "params": [{"id": tid, "windowId": 1}]}
        )
    await bridge.on_extension_message({"method": "extension.initialized", "params": []})


async def _cdp(bridge: ExtensionBridge, ext: FakeExtension, **msg: Any) -> dict[str, Any]:
    await bridge.on_cdp_message(msg)
    return next(m for m in ext.cdp_out if m.get("id") == msg["id"])


# --- handshake + auto-attach ---------------------------------------------------


async def test_initialized_event_sets_ready_and_tabs_are_not_attached_yet() -> None:
    bridge, ext = _make()
    assert not bridge.initialized.is_set()
    await _handshake(bridge, 7)
    assert bridge.initialized.is_set()
    assert ext.commands == []  # observation only until Playwright asks


async def test_set_auto_attach_attaches_every_known_tab_and_announces_it() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7, 8)
    reply = await _cdp(
        bridge, ext, id=1, method="Target.setAutoAttach", params={"autoAttach": True}
    )
    assert reply == {"id": 1, "result": {}}
    attaches = [c for c in ext.commands if c["method"] == "chrome.debugger.attach"]
    assert [c["params"] for c in attaches] == [[{"tabId": 7}, "1.3"], [{"tabId": 8}, "1.3"]]
    events = [m for m in ext.cdp_out if m.get("method") == "Target.attachedToTarget"]
    assert [e["params"]["sessionId"] for e in events] == ["pw-tab-1", "pw-tab-2"]
    assert events[0]["params"]["targetInfo"] == {
        "targetId": "T7",
        "type": "page",
        "url": "u",
        "attached": True,
    }
    assert events[0]["params"]["waitingForDebugger"] is False


async def test_tab_created_after_auto_attach_is_attached_on_its_own() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    await bridge.on_extension_message(
        {"method": "chrome.tabs.onCreated", "params": [{"id": 9, "openerTabId": 7}]}
    )
    await asyncio.sleep(0.05)
    events = [m for m in ext.cdp_out if m.get("method") == "Target.attachedToTarget"]
    assert [e["params"]["sessionId"] for e in events] == ["pw-tab-1", "pw-tab-2"]


# --- routing ------------------------------------------------------------------------


async def test_session_command_routes_to_the_tab_and_echoes_session_id() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    reply = await _cdp(bridge, ext, id=2, sessionId="pw-tab-1", method="Page.enable", params={})
    assert reply == {
        "id": 2,
        "sessionId": "pw-tab-1",
        "result": {"echo": "Page.enable", "debuggee": {"tabId": 7}},
    }
    cmd = ext.commands[-1]
    assert cmd["method"] == "chrome.debugger.sendCommand"
    assert cmd["params"] == [{"tabId": 7}, "Page.enable", {}]


async def test_browser_level_command_rides_any_attached_tab() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    reply = await _cdp(bridge, ext, id=2, method="Storage.getCookies", params={})
    assert reply["result"]["debuggee"] == {"tabId": 7}
    assert "sessionId" not in reply


async def test_browser_level_command_with_no_tab_is_an_error_reply() -> None:
    bridge, ext = _make()
    await _handshake(bridge)
    reply = await _cdp(bridge, ext, id=2, method="Storage.getCookies", params={})
    assert "No attached tab" in reply["error"]["message"]


async def test_unknown_session_is_an_error_reply_not_a_dead_command() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    reply = await _cdp(bridge, ext, id=3, sessionId="pw-tab-42", method="Page.enable", params={})
    assert reply["error"]["message"] == "No tab found for sessionId: pw-tab-42"


async def test_extension_error_becomes_a_cdp_error_reply() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    ext.fail_next = "Debugger is not attached to the tab with id: 7."
    reply = await _cdp(bridge, ext, id=2, sessionId="pw-tab-1", method="Page.enable", params={})
    assert reply["error"]["message"] == "Debugger is not attached to the tab with id: 7."


async def test_debugger_events_are_forwarded_under_the_tab_session() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    await bridge.on_extension_message(
        {
            "method": "chrome.debugger.onEvent",
            "params": [{"tabId": 7}, "Page.frameNavigated", {"frame": {"id": "f"}}],
        }
    )
    assert ext.cdp_out[-1] == {
        "sessionId": "pw-tab-1",
        "method": "Page.frameNavigated",
        "params": {"frame": {"id": "f"}},
    }


async def test_events_for_unknown_tabs_are_dropped() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    n = len(ext.cdp_out)
    await bridge.on_extension_message(
        {"method": "chrome.debugger.onEvent", "params": [{"tabId": 55}, "Page.x", {}]}
    )
    assert len(ext.cdp_out) == n


async def test_child_session_events_keep_their_own_id_and_commands_route_back() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    # Chrome attaches an OOPIF under the tab: the event carries the child id.
    await bridge.on_extension_message(
        {
            "method": "chrome.debugger.onEvent",
            "params": [
                {"tabId": 7},
                "Target.attachedToTarget",
                {"sessionId": "child-1", "targetInfo": {"type": "iframe"}},
            ],
        }
    )
    await bridge.on_extension_message(
        {
            "method": "chrome.debugger.onEvent",
            "params": [{"tabId": 7, "sessionId": "child-1"}, "Runtime.executionContextCreated", {}],
        }
    )
    assert ext.cdp_out[-1]["sessionId"] == "child-1"
    reply = await _cdp(bridge, ext, id=5, sessionId="child-1", method="DOM.getDocument", params={})
    assert reply["result"]["debuggee"] == {"tabId": 7, "sessionId": "child-1"}


# --- the client-root aliasing our driver needs ------------------------------------


async def test_attach_to_browser_target_mints_a_root_alias_playwright_can_use() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    reply = await _cdp(bridge, ext, id=2, method="Target.attachToBrowserTarget", params={})
    sid = reply["result"]["sessionId"]
    # Announced on the root BEFORE the reply, as Chrome does — the client
    # creates its session object from the event, not the reply.
    idx_event = next(
        i
        for i, m in enumerate(ext.cdp_out)
        if m.get("method") == "Target.attachedToTarget" and m["params"]["sessionId"] == sid
    )
    idx_reply = next(i for i, m in enumerate(ext.cdp_out) if m.get("id") == 2)
    assert idx_event < idx_reply
    assert "sessionId" not in ext.cdp_out[idx_event]
    # Commands on the alias are browser-level, answered under the alias.
    r2 = await _cdp(bridge, ext, id=3, sessionId=sid, method="Browser.getVersion", params={})
    assert r2["sessionId"] == sid and r2["result"]["product"] == "Chrome/Extension-Bridge"


async def test_attach_to_target_aliases_the_tab_and_fans_events_out() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    root = (await _cdp(bridge, ext, id=2, method="Target.attachToBrowserTarget", params={}))[
        "result"
    ]["sessionId"]
    reply = await _cdp(
        bridge,
        ext,
        id=3,
        sessionId=root,
        method="Target.attachToTarget",
        params={"targetId": "T7", "flatten": True},
    )
    alias = reply["result"]["sessionId"]
    assert alias != "pw-tab-1"
    ann = next(
        m
        for m in ext.cdp_out
        if m.get("method") == "Target.attachedToTarget" and m["params"]["sessionId"] == alias
    )
    assert ann["sessionId"] == root  # parented under the client root it was asked on
    # A command on the alias reaches the SAME tab.
    r = await _cdp(bridge, ext, id=4, sessionId=alias, method="Page.startScreencast", params={})
    assert r["result"]["debuggee"] == {"tabId": 7}
    # A tab event reaches both the main session and the alias.
    await bridge.on_extension_message(
        {"method": "chrome.debugger.onEvent", "params": [{"tabId": 7}, "Page.screencastFrame", {}]}
    )
    frames = [m for m in ext.cdp_out if m.get("method") == "Page.screencastFrame"]
    assert sorted(f["sessionId"] for f in frames) == sorted(["pw-tab-1", alias])


async def test_detach_from_target_drops_the_alias() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    alias = (
        await _cdp(bridge, ext, id=2, method="Target.attachToTarget", params={"targetId": "T7"})
    )["result"]["sessionId"]
    await _cdp(bridge, ext, id=3, method="Target.detachFromTarget", params={"sessionId": alias})
    assert ext.cdp_out[-2]["method"] == "Target.detachedFromTarget"
    reply = await _cdp(bridge, ext, id=4, sessionId=alias, method="Page.enable", params={})
    assert "No tab found" in reply["error"]["message"]


async def test_alias_ids_are_never_reused_after_a_detach() -> None:
    """Playwright keys its CRSessions by id and a re-minted id would overwrite
    a live one; the relay's one counter mints every id it hands out."""
    bridge, ext = _make()
    await _handshake(bridge, 7)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})

    async def attach(i: int) -> str:
        r = await _cdp(bridge, ext, id=i, method="Target.attachToTarget", params={"targetId": "T7"})
        return str(r["result"]["sessionId"])

    a1, a2 = await attach(2), await attach(3)
    await _cdp(bridge, ext, id=4, method="Target.detachFromTarget", params={"sessionId": a1})
    a3 = await attach(5)
    assert len({a1, a2, a3}) == 3


async def test_alias_detach_is_announced_on_the_root_it_was_attached_under() -> None:
    """Playwright disposes a CDPSession from Target.detachedFromTarget seen on
    its PARENT session — the client root the attach was asked on — so both
    detach paths (explicit, and the tab going away) announce it there."""
    bridge, ext = _make()
    await _handshake(bridge, 7, 8)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    root = (await _cdp(bridge, ext, id=2, method="Target.attachToBrowserTarget", params={}))[
        "result"
    ]["sessionId"]

    async def attach(i: int, target: str) -> str:
        r = await _cdp(
            bridge,
            ext,
            id=i,
            sessionId=root,
            method="Target.attachToTarget",
            params={"targetId": target},
        )
        return str(r["result"]["sessionId"])

    a7, a8 = await attach(3, "T7"), await attach(4, "T8")
    await _cdp(bridge, ext, id=5, method="Target.detachFromTarget", params={"sessionId": a7})
    await bridge.on_extension_message({"method": "chrome.tabs.onRemoved", "params": [8, {}]})
    detached = {
        m["params"]["sessionId"]: m.get("sessionId")
        for m in ext.cdp_out
        if m.get("method") == "Target.detachedFromTarget"
    }
    assert detached == {a7: root, a8: root, "pw-tab-2": None}


async def test_attach_to_unknown_target_is_an_error() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    reply = await _cdp(
        bridge, ext, id=2, method="Target.attachToTarget", params={"targetId": "nope"}
    )
    assert "No target with given id" in reply["error"]["message"]


# --- Target create/close, the faked browser commands --------------------------------


async def test_create_target_makes_a_tab_and_attaches_it() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    reply = await _cdp(bridge, ext, id=2, method="Target.createTarget", params={"url": "https://x"})
    assert reply["result"] == {"targetId": "T99"}
    assert {"id": 1, "method": "chrome.tabs.create", "params": [{"url": "https://x"}]} in [
        {k: v for k, v in c.items() if k != "id"} | {"id": 1} for c in ext.commands
    ]
    r = await _cdp(bridge, ext, id=3, method="Target.closeTarget", params={"targetId": "T99"})
    assert r["result"] == {"success": True}
    assert ext.commands[-1]["method"] == "chrome.tabs.remove"
    assert ext.commands[-1]["params"] == [99]


async def test_close_unknown_target_reports_failure_not_error() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    r = await _cdp(bridge, ext, id=3, method="Target.closeTarget", params={"targetId": "T1"})
    assert r["result"] == {"success": False}


async def test_browser_get_version_is_faked_and_download_behavior_is_a_noop() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    v = await _cdp(bridge, ext, id=1, method="Browser.getVersion", params={})
    assert v["result"]["protocolVersion"] == "1.3"
    d = await _cdp(
        bridge, ext, id=2, method="Browser.setDownloadBehavior", params={"behavior": "allow"}
    )
    assert d["result"] == {}
    assert ext.commands == []  # neither reached the extension


async def test_browser_close_is_refused_never_forwarded() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    r = await _cdp(bridge, ext, id=2, method="Browser.close", params={})
    assert "not forwarded" in r["error"]["message"]
    assert all(
        c["method"] != "chrome.debugger.sendCommand" or c["params"][1] != "Browser.close"
        for c in ext.commands
    )


async def test_get_target_info_on_a_session_answers_from_the_model() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    r = await _cdp(
        bridge, ext, id=2, sessionId="pw-tab-1", method="Target.getTargetInfo", params={}
    )
    assert r["result"]["targetInfo"]["targetId"] == "T7"


async def test_root_get_target_info_is_answered_even_with_no_attached_tab() -> None:
    """Playwright sends the root form right after Target.setAutoAttach. A
    seed tab whose attach failed (DevTools open on it — swallowed with a
    warning, like upstream) leaves no tab to forward through; upstream
    answers it from the model, so the connect survives with zero pages."""
    bridge, ext = _make()
    await _handshake(bridge, 7)
    ext.fail_next = "Another debugger is already attached to the tab with id: 7."
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    r = await _cdp(bridge, ext, id=2, method="Target.getTargetInfo", params={})
    assert r == {"id": 2, "result": {}}
    assert not any(
        c["method"] == "chrome.debugger.sendCommand" and c["params"][1] == "Target.getTargetInfo"
        for c in ext.commands
    )


# --- detach / disconnect ------------------------------------------------------------


async def test_tab_removed_emits_detached_and_forgets_the_session() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    await bridge.on_extension_message({"method": "chrome.tabs.onRemoved", "params": [7, {}]})
    assert ext.cdp_out[-1] == {
        "method": "Target.detachedFromTarget",
        "params": {"sessionId": "pw-tab-1", "targetId": "T7"},
    }
    r = await _cdp(bridge, ext, id=2, sessionId="pw-tab-1", method="Page.enable", params={})
    assert "No tab found" in r["error"]["message"]


async def test_debugger_detach_event_detaches_too() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)
    await _cdp(bridge, ext, id=1, method="Target.setAutoAttach", params={})
    await bridge.on_extension_message(
        {"method": "chrome.debugger.onDetach", "params": [{"tabId": 7}, "target_closed"]}
    )
    assert ext.cdp_out[-1]["method"] == "Target.detachedFromTarget"


async def test_close_fails_every_in_flight_extension_call() -> None:
    bridge, ext = _make()
    await _handshake(bridge, 7)

    async def never_answer(msg: dict[str, Any]) -> None:
        ext.commands.append(msg)

    bridge._send_ext = never_answer  # the extension goes quiet
    task = asyncio.create_task(
        bridge.on_cdp_message({"id": 1, "method": "Target.createTarget", "params": {"url": "u"}})
    )
    await asyncio.sleep(0.02)
    bridge.close("User disconnected")
    await task
    reply = next(m for m in ext.cdp_out if m.get("id") == 1)
    assert reply["error"]["message"] == "Extension disconnected: User disconnected"
    with pytest.raises(RelayError, match="Extension not connected"):
        await bridge._call_extension("chrome.tabs.create", [{}])


# --- CdpRelay over real loopback sockets ---------------------------------------------


def test_connect_url_carries_the_upstream_query_shape() -> None:
    relay = CdpRelay(client_name="MCP browser tool", token="t0k/en")
    relay._port = 4567
    url = relay.connect_url("abcdefghijklmnopabcdefghijklmnop")
    parsed = urlparse(url)
    assert parsed.scheme == "chrome-extension"
    assert parsed.netloc == "abcdefghijklmnopabcdefghijklmnop"
    assert parsed.path == "/connect.html"
    q = parse_qs(parsed.query)
    assert q["mcpRelayUrl"] == [f"ws://127.0.0.1:4567{relay._ext_path}"]
    assert json.loads(q["client"][0]) == {"name": "MCP browser tool"}
    assert q["protocolVersion"] == [str(PROTOCOL_VERSION)]
    assert q["token"] == ["t0k/en"]
    # No token → no token param: the extension shows its tab picker.
    assert "token=" not in CdpRelay(client_name="x", token="").connect_url("a" * 32)


async def _connect(url: str) -> Any:
    from websockets.asyncio.client import connect

    return await connect(url)


async def test_relay_end_to_end_over_sockets() -> None:
    relay = CdpRelay(client_name="test", token="")
    await relay.start()
    try:
        # CDP before the extension: refused with upstream's reason.
        early = await _connect(relay.cdp_url)
        with pytest.raises(Exception, match="Extension not connected"):
            await early.recv()
        ext = await _connect(relay.extension_url)
        await ext.send(json.dumps({"method": "chrome.tabs.onCreated", "params": [{"id": 3}]}))
        await ext.send(json.dumps({"method": "extension.initialized", "params": []}))
        await relay.wait_for_extension(5)
        # A second extension is refused.
        second = await _connect(relay.extension_url)
        with pytest.raises(Exception, match="Another extension connection"):
            await second.recv()

        cdp = await _connect(relay.cdp_url)
        await cdp.send(json.dumps({"id": 1, "method": "Target.setAutoAttach", "params": {}}))
        attach = json.loads(await ext.recv())
        assert attach["method"] == "chrome.debugger.attach" and attach["params"] == [
            {"tabId": 3},
            "1.3",
        ]
        await ext.send(json.dumps({"id": attach["id"], "result": {}}))
        info = json.loads(await ext.recv())
        assert info["params"][1] == "Target.getTargetInfo"
        await ext.send(json.dumps({"id": info["id"], "result": {"targetInfo": {"targetId": "T3"}}}))
        first = json.loads(await cdp.recv())
        assert first["method"] == "Target.attachedToTarget"
        assert first["params"]["sessionId"] == "pw-tab-1"
        reply = json.loads(await cdp.recv())
        assert reply == {"id": 1, "result": {}}
        # Playwright leaving closes the extension link (upstream's rule).
        await cdp.close()
        with pytest.raises(Exception, match="Playwright client disconnected"):
            while True:
                await ext.recv()
    finally:
        await relay.stop()


async def test_the_extensions_own_close_reason_reaches_the_driver_and_the_cdp_client() -> None:
    """The extension closes with a reason ('User disconnected', 'All
    controlled tabs detached'); it is what the driver reports, not a
    generic 'closed the socket'."""
    relay = CdpRelay(client_name="test", token="")
    seen: list[str] = []
    relay.on_extension_closed = seen.append
    await relay.start()
    try:
        ext = await _connect(relay.extension_url)
        await ext.send(json.dumps({"method": "extension.initialized", "params": []}))
        await relay.wait_for_extension(5)
        cdp = await _connect(relay.cdp_url)
        await ext.close(1000, "All controlled tabs detached")
        with pytest.raises(Exception, match="Extension disconnected: All controlled tabs detached"):
            while True:
                await cdp.recv()
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.02)
        assert seen == ["All controlled tabs detached"]
        assert relay.disconnect_reason == "All controlled tabs detached"
    finally:
        await relay.stop()


async def test_wait_for_extension_times_out_loudly() -> None:
    relay = CdpRelay(client_name="test", token="abc")
    await relay.start()
    try:
        with pytest.raises(RelayError, match="did not connect within 0s"):
            await relay.wait_for_extension(0.05)
    finally:
        await relay.stop()


async def test_unknown_path_is_404_not_a_socket() -> None:
    relay = CdpRelay(client_name="test", token="")
    await relay.start()
    try:
        with pytest.raises(Exception, match="404"):
            await _connect(f"ws://127.0.0.1:{relay._port}/nope")
    finally:
        await relay.stop()

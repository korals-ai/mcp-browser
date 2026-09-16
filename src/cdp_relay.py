"""CDP relay: the bridge between :class:`PlaywrightDriver` and a browser extension.

The third runtime of the browser tool is the user's OWN browser, reached through
an extension that holds the ``debugger`` permission. The extension cannot speak
CDP to Playwright directly — an extension may only issue five ``chrome.*``
calls (``debugger.attach/detach/sendCommand``, ``tabs.create/remove``) and
forward four ``chrome.*`` events — so this relay sits between the two and
translates. It is the Playwright Extension's protocol v2, implemented verbatim
(``packages/extension`` + ``tools/mcp/cdpRelay.ts`` in microsoft/playwright);
the extension side is unmodified upstream code, so a divergence here is a bug
here.

Two WebSocket endpoints on a loopback-only ephemeral port:

- ``/extension/<uuid>`` — the extension dials in after the user approves the
  connect page. It pushes ``chrome.tabs.onCreated`` for every tab it hands us,
  then ``extension.initialized``; every later ``chrome.debugger.onEvent`` is a
  CDP event for one of those tabs.
- ``/cdp/<uuid>`` — Playwright dials in via ``connect_over_cdp``. Every CDP
  command with a ``sessionId`` is forwarded to that tab's debugger session;
  the handful of browser-level commands Playwright needs are answered here:
  ``Browser.getVersion`` is faked, ``Browser.setDownloadBehavior`` is a no-op
  (the extension has no browser-level download control), ``Target.*`` are
  synthesised from the tab model (``pw-tab-N`` session ids), and
  ``Browser.close`` disconnects the relay instead of closing the user's browser.

Nothing here touches a tab the user did not put in the extension's tab group:
the tab model only ever contains tabs the extension announced.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

log = logging.getLogger("workspace-tool-browser")

# The Web Store id of the upstream Playwright Extension (Apache-2.0), the
# bridge used until our own fork ships. A fork carries a different id.
PLAYWRIGHT_EXTENSION_ID = "mmlmfjhmonkocbjadbfplnigmagldckm"
PROTOCOL_VERSION = 2

# CDP frames carry base64 screenshots / screencast frames; the default 1 MiB
# would drop them mid-session with a 1009 and no message.
_MAX_FRAME_BYTES = 64 * 1024 * 1024

SendJson = Callable[[dict[str, Any]], Awaitable[None]]


class RelayError(RuntimeError):
    """A relay-level failure surfaced to the driver (extension gone, timeout)."""


@dataclass
class _TabSession:
    tab_id: int
    session_id: str
    target_info: dict[str, Any]
    # Child CDP sessions (OOPIFs, workers) Chrome attached under this tab, seen
    # via Target.attachedToTarget; commands for them route to the same tab
    # with the child sessionId kept.
    child_sessions: set[str] = field(default_factory=set)
    # Extra session ids Playwright asked for on this SAME tab
    # (``Target.attachToTarget`` from a client root session — what
    # ``context.new_cdp_session(page)`` does). chrome.debugger gives an
    # extension one session per tab, so these are aliases the relay
    # multiplexes: commands on any of them reach the tab, every tab event is
    # fanned out to all of them. Keyed alias id → the client root session it
    # was announced under (None = the real root): Playwright disposes a
    # CDPSession from ``Target.detachedFromTarget`` on its PARENT session, so
    # the detach must be announced where the attach was.
    aliases: dict[str, str | None] = field(default_factory=dict)


class ExtensionBridge:
    """The translation core: one extension link ⇄ one CDP client, transport-free.

    The owner feeds it decoded frames (:meth:`on_extension_message`,
    :meth:`on_cdp_message`) and hands it two senders. Keeping the core off the
    sockets is what lets the unit tests drive a scripted fake extension.
    """

    def __init__(self, send_to_extension: SendJson, send_to_cdp: SendJson) -> None:
        self._send_ext = send_to_extension
        self._send_cdp = send_to_cdp
        self._known_tabs: dict[int, dict[str, Any]] = {}
        self._sessions: dict[int, _TabSession] = {}
        self._auto_attach = False
        self._next_session = 1
        # Client root sessions minted for ``Target.attachToBrowserTarget``
        # (Playwright's ``_clientRootSession``) — browser-level in routing,
        # answered under their own id. chrome.debugger refuses the real one.
        self._browser_aliases: set[str] = set()
        self._ext_pending: dict[int, asyncio.Future[Any]] = {}
        self._ext_last_id = 0
        # Set by `extension.initialized`; CDP commands are not processed before.
        self.initialized = asyncio.Event()
        self.closed = False

    # --- extension → bridge -------------------------------------------------

    async def on_extension_message(self, msg: dict[str, Any]) -> None:
        msg_id = msg.get("id")
        if msg_id is not None and msg_id in self._ext_pending:
            fut = self._ext_pending.pop(msg_id)
            if fut.done():
                return
            if msg.get("error"):
                fut.set_exception(RelayError(str(msg["error"])))
            else:
                fut.set_result(msg.get("result"))
            return
        if msg_id is not None:
            log.debug("cdp-relay: unexpected extension response id=%s", msg_id)
            return
        method = msg.get("method")
        params = msg.get("params") or []
        if method == "chrome.tabs.onCreated":
            self._on_tab_created(params[0])
        elif method == "chrome.tabs.onRemoved":
            await self._on_tab_removed(int(params[0]))
        elif method == "chrome.debugger.onEvent":
            await self._on_debugger_event(
                params[0], params[1], params[2] if len(params) > 2 else {}
            )
        elif method == "chrome.debugger.onDetach":
            await self._detach_tab(int(params[0].get("tabId", -1)))
        elif method == "extension.initialized":
            self.initialized.set()
        elif method is None and "error" in msg:
            log.warning("cdp-relay: extension protocol error %s", msg["error"])

    def _on_tab_created(self, tab: dict[str, Any]) -> None:
        tab_id = tab.get("id")
        if tab_id is None:
            return
        self._known_tabs[int(tab_id)] = tab
        if self._auto_attach:
            self._spawn(self._attach_tab(int(tab_id)))

    async def _on_tab_removed(self, tab_id: int) -> None:
        self._known_tabs.pop(tab_id, None)
        await self._detach_tab(tab_id)

    async def _on_debugger_event(
        self, source: dict[str, Any], method: str, params: dict[str, Any]
    ) -> None:
        tab_id = source.get("tabId")
        if tab_id is None:
            return
        sess = self._sessions.get(int(tab_id))
        if sess is None:
            return
        child = params.get("sessionId") if isinstance(params, dict) else None
        if method == "Target.attachedToTarget" and child:
            sess.child_sessions.add(str(child))
        elif method == "Target.detachedFromTarget" and child:
            sess.child_sessions.discard(str(child))
        # A child session's event keeps its own Chrome sessionId; a top-level
        # tab event carries the relay's pw-tab-N id and is repeated for every
        # alias session on the tab (each is a separate CDPSession to Playwright).
        if source.get("sessionId"):
            await self._send_cdp(
                {"sessionId": source["sessionId"], "method": method, "params": params}
            )
            return
        for sid in (sess.session_id, *sorted(sess.aliases)):
            await self._send_cdp({"sessionId": sid, "method": method, "params": params})

    # --- CDP client → bridge ------------------------------------------------

    async def on_cdp_message(self, msg: dict[str, Any]) -> None:
        """Handle one CDP command. Concurrent by design — Playwright pipelines
        commands and a slow one must not block the rest — so the owner
        schedules this per frame rather than awaiting it in the read loop."""
        msg_id = msg.get("id")
        session_id = msg.get("sessionId")
        method = str(msg.get("method", ""))
        params = msg.get("params") or {}
        try:
            result = await self._handle_cdp_command(method, params, session_id)
            reply: dict[str, Any] = {"id": msg_id, "result": result}
        except Exception as exc:  # the CDP contract: an error reply, never a dead command
            reply = {"id": msg_id, "error": {"message": str(exc)}}
        if session_id is not None:
            reply["sessionId"] = session_id
        await self._send_cdp(reply)

    async def _handle_cdp_command(
        self, method: str, params: dict[str, Any], session_id: str | None
    ) -> Any:
        if method == "Target.getTargetInfo":
            # Answered from the tab model for every session, never forwarded
            # (upstream's rule): the root form arrives right after
            # Target.setAutoAttach, when there may be no attached tab to
            # carry a forwarded command.
            sess = self._session_by_id(session_id) if session_id else None
            return {"targetInfo": sess.target_info} if sess else {}
        if session_id in self._browser_aliases:
            return await self._handle_root_command(method, params, session_id)
        if session_id is None:
            return await self._handle_root_command(method, params, None)
        return await self._send_session_command(session_id, method, params)

    async def _handle_root_command(
        self, method: str, params: dict[str, Any], root: str | None
    ) -> Any:
        """A browser-level command, on the real root session (``root`` None)
        or on one of the client root sessions the relay minted."""
        if method == "Browser.getVersion":
            return {
                "protocolVersion": "1.3",
                "product": "Chrome/Extension-Bridge",
                "userAgent": "CDP-Bridge-Server/1.0.0",
            }
        if method == "Browser.setDownloadBehavior":
            return {}
        if method == "Browser.close":
            # Playwright's browser.close() on a connected browser. The user's
            # Chrome is theirs: drop the links, never forward a Browser.close.
            raise RelayError("Browser.close is not forwarded to the user's browser")
        if method == "Target.setAutoAttach":
            if root is None:
                await self._enable_auto_attach()
            return {}
        if method == "Target.createTarget":
            return await self._create_target(params.get("url"))
        if method == "Target.closeTarget":
            return await self._close_target(params.get("targetId"))
        if method == "Target.attachToBrowserTarget":
            return await self._attach_to_browser_target()
        if method == "Target.attachToTarget":
            return await self._attach_alias(str(params.get("targetId")), root)
        if method == "Target.detachFromTarget":
            return await self._detach_alias(str(params.get("sessionId")))
        if method == "Target.getTargets":
            return {"targetInfos": [dict(s.target_info) for s in self._sessions.values()]}
        return await self._send_browser_command(method, params)

    async def _attach_to_browser_target(self) -> dict[str, Any]:
        sid = f"pw-browser-{self._next_session}"
        self._next_session += 1
        self._browser_aliases.add(sid)
        # Chrome announces the new session on the root before answering; the
        # client creates its CDPSession object from that event.
        await self._send_cdp(
            {
                "method": "Target.attachedToTarget",
                "params": {
                    "sessionId": sid,
                    "targetInfo": {
                        "targetId": "browser",
                        "type": "browser",
                        "title": "",
                        "url": "",
                        "attached": True,
                        "canAccessOpener": False,
                    },
                    "waitingForDebugger": False,
                },
            }
        )
        return {"sessionId": sid}

    async def _attach_alias(self, target_id: str, root: str | None) -> dict[str, Any]:
        sess = next(
            (s for s in self._sessions.values() if s.target_info.get("targetId") == target_id),
            None,
        )
        if sess is None:
            raise RelayError(f"No target with given id found: {target_id}")
        sid = f"{sess.session_id}-a{self._next_session}"
        self._next_session += 1
        sess.aliases[sid] = root
        event: dict[str, Any] = {
            "method": "Target.attachedToTarget",
            "params": {
                "sessionId": sid,
                "targetInfo": {**sess.target_info, "attached": True},
                "waitingForDebugger": False,
            },
        }
        if root is not None:
            event["sessionId"] = root  # parented under the client root it was asked on
        await self._send_cdp(event)
        return {"sessionId": sid}

    async def _detach_alias(self, sid: str) -> dict[str, Any]:
        for sess in self._sessions.values():
            if sid in sess.aliases:
                parent = sess.aliases.pop(sid)
                await self._send_detached(sess, sid, parent)
                return {}
        if sid in self._browser_aliases:
            self._browser_aliases.discard(sid)
            return {}
        raise RelayError(f"No session with given id found: {sid}")

    async def _enable_auto_attach(self) -> None:
        self._auto_attach = True
        results = await asyncio.gather(
            *(self._attach_tab(tid) for tid in list(self._known_tabs)), return_exceptions=True
        )
        for tid, res in zip(list(self._known_tabs), results, strict=False):
            if isinstance(res, Exception):
                log.warning("cdp-relay: attach to tab %s failed: %s", tid, res)

    async def _create_target(self, url: str | None) -> dict[str, Any]:
        tab = await self._call_extension("chrome.tabs.create", [{"url": url or "about:blank"}])
        if not isinstance(tab, dict) or tab.get("id") is None:
            raise RelayError("Failed to create tab")
        self._known_tabs[int(tab["id"])] = tab
        sess = await self._attach_tab(int(tab["id"]))
        return {"targetId": sess.target_info.get("targetId")}

    async def _close_target(self, target_id: str | None) -> dict[str, Any]:
        sess = next(
            (s for s in self._sessions.values() if s.target_info.get("targetId") == target_id),
            None,
        )
        if sess is None:
            return {"success": False}
        await self._call_extension("chrome.tabs.remove", [sess.tab_id])
        return {"success": True}

    async def _send_browser_command(self, method: str, params: dict[str, Any]) -> Any:
        # chrome.debugger needs a target; a browser-scoped command answers the
        # same through any attached tab.
        sess = next(iter(self._sessions.values()), None)
        if sess is None:
            raise RelayError(f"No attached tab to forward browser-level command: {method}")
        return await self._call_extension(
            "chrome.debugger.sendCommand", [{"tabId": sess.tab_id}, method, params]
        )

    async def _send_session_command(
        self, session_id: str, method: str, params: dict[str, Any]
    ) -> Any:
        sess = self._session_by_id(session_id)
        child: str | None = None
        if sess is None:
            sess = next(
                (s for s in self._sessions.values() if session_id in s.child_sessions), None
            )
            child = session_id
        if sess is None:
            raise RelayError(f"No tab found for sessionId: {session_id}")
        debuggee: dict[str, Any] = {"tabId": sess.tab_id}
        if child is not None:
            debuggee["sessionId"] = child
        return await self._call_extension("chrome.debugger.sendCommand", [debuggee, method, params])

    # --- tab model ----------------------------------------------------------

    def _session_by_id(self, session_id: str) -> _TabSession | None:
        return next(
            (
                s
                for s in self._sessions.values()
                if s.session_id == session_id or session_id in s.aliases
            ),
            None,
        )

    async def _attach_tab(self, tab_id: int) -> _TabSession:
        existing = self._sessions.get(tab_id)
        if existing is not None:
            return existing
        await self._call_extension("chrome.debugger.attach", [{"tabId": tab_id}, "1.3"])
        info = await self._call_extension(
            "chrome.debugger.sendCommand", [{"tabId": tab_id}, "Target.getTargetInfo"]
        )
        target_info = dict((info or {}).get("targetInfo") or {})
        sess = _TabSession(tab_id, f"pw-tab-{self._next_session}", target_info)
        self._next_session += 1
        self._sessions[tab_id] = sess
        await self._send_cdp(
            {
                "method": "Target.attachedToTarget",
                "params": {
                    "sessionId": sess.session_id,
                    "targetInfo": {**target_info, "attached": True},
                    "waitingForDebugger": False,
                },
            }
        )
        return sess

    async def _detach_tab(self, tab_id: int) -> None:
        sess = self._sessions.pop(tab_id, None)
        if sess is None:
            return
        for sid, parent in sorted(sess.aliases.items()):
            await self._send_detached(sess, sid, parent)
        await self._send_detached(sess, sess.session_id, None)

    async def _send_detached(self, sess: _TabSession, sid: str, parent: str | None) -> None:
        event: dict[str, Any] = {
            "method": "Target.detachedFromTarget",
            "params": {"sessionId": sid, "targetId": sess.target_info.get("targetId")},
        }
        if parent is not None:
            event["sessionId"] = parent
        await self._send_cdp(event)

    # --- extension RPC ------------------------------------------------------

    async def _call_extension(self, method: str, params: list[Any]) -> Any:
        if self.closed:
            raise RelayError("Extension not connected")
        self._ext_last_id += 1
        msg_id = self._ext_last_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._ext_pending[msg_id] = fut
        await self._send_ext({"id": msg_id, "method": method, "params": params})
        return await fut

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        task.add_done_callback(_log_task_error)

    def close(self, reason: str) -> None:
        """The extension link is gone: fail every in-flight extension call."""
        self.closed = True
        for fut in self._ext_pending.values():
            if not fut.done():
                fut.set_exception(RelayError(f"Extension disconnected: {reason}"))
        self._ext_pending.clear()


def _log_task_error(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("cdp-relay: background attach failed: %s", exc)


class CdpRelay:
    """The two-endpoint WebSocket server around one :class:`ExtensionBridge`.

    Lifecycle: :meth:`start` binds ``127.0.0.1:0``; the driver opens
    :meth:`connect_url` in the user's browser; :meth:`wait_for_extension`
    returns once the extension has completed its handshake; then Playwright
    connects to :attr:`cdp_url`. A second extension or CDP connection is
    refused (upstream's rule), and either side dropping closes the other.
    """

    def __init__(self, *, client_name: str, token: str) -> None:
        self._client_name = client_name
        self._token = token
        rid = str(uuid.uuid4())
        self._ext_path = f"/extension/{rid}"
        self._cdp_path = f"/cdp/{rid}"
        self._server: Any = None
        self._port = 0
        self._ext_ws: Any = None
        self._cdp_ws: Any = None
        self._bridge: ExtensionBridge | None = None
        self._ext_connected = asyncio.Event()
        # Why the extension side went away, for the driver's error message.
        self.disconnect_reason: str | None = None
        self.on_extension_closed: Callable[[str], None] | None = None

    async def start(self) -> None:
        from websockets.asyncio.server import serve

        def _process_request(connection: Any, request: Any) -> Any:
            if request.path in (self._ext_path, self._cdp_path):
                return None
            return connection.respond(HTTPStatus.NOT_FOUND, "Not Found\n")

        self._server = await serve(
            self._handle,
            "127.0.0.1",
            0,
            process_request=_process_request,
            max_size=_MAX_FRAME_BYTES,
        )
        self._port = self._server.sockets[0].getsockname()[1]
        log.info("cdp-relay listening on 127.0.0.1:%d", self._port)

    @property
    def cdp_url(self) -> str:
        return f"ws://127.0.0.1:{self._port}{self._cdp_path}"

    @property
    def extension_url(self) -> str:
        return f"ws://127.0.0.1:{self._port}{self._ext_path}"

    def connect_url(self, extension_id: str = PLAYWRIGHT_EXTENSION_ID) -> str:
        """The extension's connect page, parameterised the way upstream's
        relay does it. With a matching token the extension auto-approves
        (no tab picker); without one the user picks a tab."""
        client = json.dumps({"name": self._client_name})
        url = (
            f"chrome-extension://{extension_id}/connect.html"
            f"?mcpRelayUrl={quote(self.extension_url, safe='')}"
            f"&client={quote(client, safe='')}"
            f"&protocolVersion={PROTOCOL_VERSION}"
        )
        if self._token:
            url += f"&token={quote(self._token, safe='')}"
        return url

    async def wait_for_extension(self, timeout_s: float) -> None:
        """Block until the extension connected AND finished its handshake."""
        try:
            await asyncio.wait_for(self._ext_connected.wait(), timeout_s)
            assert self._bridge is not None
            await asyncio.wait_for(self._bridge.initialized.wait(), timeout_s)
        except TimeoutError as exc:
            raise RelayError(
                f"browser extension did not connect within {timeout_s:.0f}s after the "
                "connect page opened — is the extension installed, and does the token match?"
            ) from exc
        if self._bridge is None or self._bridge.closed:
            raise RelayError(f"Extension disconnected: {self.disconnect_reason}")

    async def stop(self) -> None:
        await self._close_links("Server stopped")
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    # --- connections --------------------------------------------------------

    async def _handle(self, ws: Any) -> None:
        path = ws.request.path
        if path == self._ext_path:
            await self._serve_extension(ws)
        else:
            await self._serve_cdp(ws)

    async def _serve_extension(self, ws: Any) -> None:
        if self._ext_ws is not None:
            await ws.close(1000, "Another extension connection already established")
            return
        self._ext_ws = ws
        self._bridge = ExtensionBridge(self._send_to_extension, self._send_to_cdp)
        self._ext_connected.set()
        log.info("cdp-relay: extension connected")
        reason = "extension closed the socket"
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as exc:
                    log.warning("cdp-relay: malformed extension frame: %s", exc)
                    continue
                await self._bridge.on_extension_message(msg)
        except Exception as exc:  # a dropped socket is the normal end here
            reason = str(exc) or type(exc).__name__
        finally:
            close_reason = (getattr(ws, "close_reason", None) or "") or reason
            self.disconnect_reason = close_reason
            self._bridge.close(close_reason)
            self._ext_ws = None
            log.info("cdp-relay: extension disconnected: %s", close_reason)
            if self._cdp_ws is not None:
                with contextlib.suppress(Exception):
                    await self._cdp_ws.close(1000, f"Extension disconnected: {close_reason}")
            if self.on_extension_closed is not None:
                self.on_extension_closed(close_reason)

    async def _serve_cdp(self, ws: Any) -> None:
        if self._bridge is None or self._ext_ws is None:
            await ws.close(1000, "Extension not connected")
            return
        if self._cdp_ws is not None:
            await ws.close(1000, "Another CDP client already connected")
            return
        self._cdp_ws = ws
        bridge = self._bridge
        await bridge.initialized.wait()
        log.info("cdp-relay: CDP client connected")
        tasks: set[asyncio.Task[None]] = set()
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as exc:
                    log.warning("cdp-relay: malformed CDP frame: %s", exc)
                    continue
                task = asyncio.create_task(bridge.on_cdp_message(msg))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except Exception as exc:  # a dropped socket is the normal end here
            log.debug("cdp-relay: CDP socket ended: %s", exc)
        finally:
            self._cdp_ws = None
            for t in tasks:
                t.cancel()
            log.info("cdp-relay: CDP client disconnected")
            # Upstream closes the extension link when Playwright leaves: the
            # tab group is released and the user gets their tabs back.
            if self._ext_ws is not None:
                with contextlib.suppress(Exception):
                    await self._ext_ws.close(1000, "Playwright client disconnected")

    async def _send_to_extension(self, msg: dict[str, Any]) -> None:
        if self._ext_ws is None:
            raise RelayError("Extension not connected")
        await self._ext_ws.send(json.dumps(msg))

    async def _send_to_cdp(self, msg: dict[str, Any]) -> None:
        if self._cdp_ws is None:
            return  # observation-only before Playwright connects (upstream's rule)
        with contextlib.suppress(Exception):
            await self._cdp_ws.send(json.dumps(msg))

    async def _close_links(self, reason: str) -> None:
        for ws in (self._cdp_ws, self._ext_ws):
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close(1000, reason)

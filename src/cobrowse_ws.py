"""The ``/cobrowse`` WebSocket: the human view+input plane for one session.

On connect it resolves (or launches) the session, starts the screencast, and
pumps CDP frames to the viewer as :class:`BrowserFrame`. Inbound it accepts
frame-acks (backpressure), input events (v1), and control signals (pause/resume
the agent, take over). When the last viewer for a session disconnects the
screencast stops — the session itself lives on (the agent may still be driving)
until the idle reaper closes it.

This handler is transport glue over :class:`SessionManager` + the driver; it
holds no browser logic of its own (per the repo's handler/service split).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from src import metrics
from src.protocol import (
    BrowserAgentState,
    BrowserCloseTab,
    BrowserControl,
    BrowserCursor,
    BrowserEndSession,
    BrowserFrame,
    BrowserFrameAck,
    BrowserInput,
    BrowserNav,
    BrowserNavigate,
    BrowserNewTab,
    BrowserResize,
    BrowserSwitchTab,
    BrowserTabs,
    normalize_cursor,
    parse_client_message,
)
from src.sessions import SessionManager

log = logging.getLogger("workspace-tool-browser")

# Cursor-mirroring is a cosmetic, best-effort side-channel (see BrowserCursor).
# It runs OUT OF BAND of input dispatch so it can never stall a viewer's clicks
# or the screencast: at most one probe in flight per connection, throttled, and
# hard-bounded by a timeout — every failure mode collapses to "no update".
_CURSOR_PROBE_MIN_INTERVAL_S = 0.08  # ≤ ~12 probes/sec per viewer
_CURSOR_PROBE_TIMEOUT_S = 0.3  # a slow page eval is abandoned, not awaited


class CoBrowseConnection:
    """Drives one viewer WebSocket against one session. Framework-agnostic: the
    ``ws`` only needs ``send_json`` / ``receive_json`` so tests use a fake and
    the Starlette endpoint adapts the real socket."""

    def __init__(
        self, manager: SessionManager, session_id: str, ws: Any, *, can_drive: bool = True
    ) -> None:
        self._manager = manager
        self._session_id = session_id
        self._ws = ws
        # Whether this viewer may DRIVE (send input / nav / tab / control / end) or
        # only WATCH. The dispatcher decides from the caller's chat grant (edit/owner
        # → drive; view → watch-only) and passes it as ?control= on the tunnel URL.
        # Defaults True so an older dispatcher (no flag) behaves as before.
        self._can_drive = can_drive
        self._ended = False  # set when the human ends the session; stops the loop
        # This viewer's screencast frame sink, registered on the shared driver so
        # frames fan out to every attached viewer. Kept so we can deregister
        # exactly this sink on disconnect. None until the stream starts.
        self._frame_sink: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
        # Per-connection egress tally, logged at teardown (the picture-tuning
        # forensic: how many frames/bytes did THIS viewer actually pull, at what fps).
        self._frames_sent = 0
        self._bytes_sent = 0
        # Cursor-mirroring side-channel state (all best-effort, see the module
        # constants). At most one probe task in flight; a monotonic gate throttles
        # scheduling; the last keyword sent is remembered so we only push changes.
        self._cursor_task: asyncio.Task[None] | None = None
        self._cursor_gate_at = 0.0
        self._cursor_last_sent: str | None = None

    async def run(self) -> None:
        """Full connection lifecycle: attach, stream, drain input, detach."""
        session = await self._manager.get_or_create(self._session_id)
        session.viewers += 1
        session.touch()
        metrics.viewer_attached()
        # Register this socket so an MCP tool (e.g. a take-over request) can push
        # an unsolicited frame to it.
        session.add_viewer_sink(self._ws.send_json)
        started = time.monotonic()
        try:
            await self._push_nav(session)
            await self._push_tabs(session)  # populate the viewer's tab strip
            # Send the CURRENT agent state on connect. Without this, a viewer that
            # attaches AFTER the agent paused (a reconnect, or a takeover it
            # missed) shows "idle" and never renders the Resume button, leaving
            # the agent locked out with no way to hand control back.
            await self._send_agent_state(session)
            await self._start_stream(session)
            await self._recv_loop(session)
        finally:
            # Cancel any in-flight cursor probe — it's cosmetic and must not
            # outlive the connection or delay teardown.
            if self._cursor_task is not None and not self._cursor_task.done():
                self._cursor_task.cancel()
            session.remove_viewer_sink(self._ws.send_json)
            session.viewers -= 1
            metrics.viewer_detached()
            # Deregister THIS viewer's frame sink. The driver stops the CDP
            # screencast only when the last sink goes (start-once / stop-when-empty),
            # so a surviving viewer keeps streaming and the session lives on (the
            # agent may still be acting; the reaper closes it later). When the human
            # ended it, manager.close already tore the driver down — skip.
            if self._frame_sink is not None and not self._ended:
                try:
                    await session.driver.remove_frame_sink(self._frame_sink)
                except Exception:
                    log.exception("cobrowse session=%s remove_frame_sink", self._session_id)
            self._log_stream_summary(time.monotonic() - started)

    def _log_stream_summary(self, dur_s: float) -> None:
        """One line per viewer at teardown — the per-session egress forensic that
        backs the picture-tuning knobs (avg fps actually delivered, avg frame size),
        without paying for a per-session metric series."""
        avg_fps = self._frames_sent / dur_s if dur_s > 0 else 0.0
        avg_kb = (self._bytes_sent / self._frames_sent / 1024) if self._frames_sent else 0.0
        log.info(
            "cobrowse stream ended session=%s frames=%d bytes=%d dur_s=%.1f "
            "avg_fps=%.1f avg_frame_kb=%.1f",
            self._session_id,
            self._frames_sent,
            self._bytes_sent,
            dur_s,
            avg_fps,
            avg_kb,
        )

    async def _push_nav(self, session: Any) -> None:
        nav = await session.driver.nav_state()
        await self._ws.send_json(
            BrowserNav(
                url=nav.get("url", ""),
                title=nav.get("title", ""),
                can_go_back=bool(nav.get("can_go_back")),
                can_go_forward=bool(nav.get("can_go_forward")),
            ).to_json()
        )

    async def _push_tabs(self, session: Any) -> None:
        tabs = await session.driver.list_tabs()
        await self._ws.send_json(BrowserTabs(tabs=tabs).to_json())

    async def _start_stream(self, session: Any) -> None:
        async def sink(data: str, meta: dict[str, Any]) -> None:
            await self._ws.send_json(
                BrowserFrame(
                    frame_id=int(meta.get("session_frame_id") or 0),
                    data=data,
                    width=int(meta.get("width") or 0),
                    height=int(meta.get("height") or 0),
                    device_scale=float(meta.get("device_scale") or 1.0),
                ).to_json()
            )
            # Count egress only AFTER the send lands — a frame dropped to a
            # disconnecting viewer (send_json raises, swallowed by the pump) is not
            # wire cost and must not inflate the budget signal.
            self._frames_sent += 1
            self._bytes_sent += len(data)
            metrics.add_screencast_bytes(len(data))
            metrics.inc_screencast_frame()

        self._frame_sink = sink
        await session.driver.add_frame_sink(sink)

    async def _recv_loop(self, session: Any) -> None:
        """Handle viewer->pod messages until the socket closes."""
        while True:
            raw = await self._ws.receive_json()
            if raw is None:  # closed
                return
            if self._manager.get(self._session_id) is not session:
                # Another viewer ended the session (Chromium closed + session
                # dropped/replaced). Stop cleanly instead of dispatching this
                # message onto a torn-down driver, which would raise and be
                # miscounted as a session error. (Co-browse is multi-viewer;
                # "End" closes it for everyone.)
                return
            try:
                msg = parse_client_message(raw)
            except ValueError as exc:
                log.warning("cobrowse session=%s bad frame: %s", self._session_id, exc)
                continue
            await self._dispatch(session, msg)
            if self._ended:  # human ended the session — the driver is closed
                return

    async def _dispatch(self, session: Any, msg: Any) -> None:
        if isinstance(msg, BrowserFrameAck):
            # Frame-acks are stream flow-control, not "driving" — always allowed so a
            # watch-only viewer's stream stays healthy (backpressure keeps working).
            await session.driver.ack_frame(msg.frame_id)
            return
        if not self._can_drive:
            # Watch-only viewer: drop every drive frame (input / nav / tab / control /
            # resize / end). It still receives the screencast; it just can't act.
            metrics.inc_input_denied()
            log.debug(
                "cobrowse session=%s dropped drive frame from watch-only viewer", self._session_id
            )
            return
        if isinstance(msg, BrowserInput):
            session.touch(actor="human")
            await session.driver.send_input(msg.event, msg.fields)
            # A mouse move may have changed what's under the pointer — refresh the
            # mirrored cursor out of band. Non-blocking: never delays the next input.
            if msg.event == "mouse" and msg.fields.get("kind") == "move":
                self._schedule_cursor_probe(session, msg.fields)
            return
        if isinstance(msg, BrowserResize):
            # A panel resize keeps the session alive (the human is watching) but is
            # not "acting" — don't claim last_actor for a passive viewport change.
            session.touch()
            await session.driver.set_viewport(msg.width, msg.height)
            return
        if isinstance(msg, BrowserControl):
            await self._apply_control(session, msg)
            return
        if isinstance(msg, BrowserSwitchTab):
            session.touch(actor="human")
            await session.driver.switch_tab(msg.tab_id)
            await self._broadcast_tabs_and_nav(session)
            return
        if isinstance(msg, BrowserCloseTab):
            session.touch(actor="human")
            await session.driver.close_tab(msg.tab_id)
            await self._broadcast_tabs_and_nav(session)
            return
        if isinstance(msg, BrowserNewTab):
            session.touch(actor="human")
            await session.driver.open("about:blank", new_tab=True)
            await self._broadcast_tabs_and_nav(session)
            return
        if isinstance(msg, BrowserNavigate):
            # Human typed a URL in the address bar — navigate the active tab.
            session.touch(actor="human")
            await session.driver.open(msg.url, new_tab=False)
            await self._broadcast_tabs_and_nav(session)
            return
        if isinstance(msg, BrowserEndSession):
            # Human ended co-browse — close Chromium + drop the session so the
            # agent's next browser tool call (or a viewer reconnect) starts fresh.
            # Flag the loop to stop: the session object is now closed, so we must
            # not dispatch anything else against it.
            metrics.inc_session_ended()
            self._ended = True
            await self._manager.close(self._session_id)

    async def _broadcast_tabs_and_nav(self, session: Any) -> None:
        """After a human tab action, push the new tab list + active URL to ALL
        attached viewers so every tab strip stays in sync."""
        tabs = await session.driver.list_tabs()
        await session.broadcast(BrowserTabs(tabs=tabs).to_json())
        nav = await session.driver.nav_state()
        await session.broadcast(
            BrowserNav(
                url=nav.get("url", ""),
                title=nav.get("title", ""),
                can_go_back=bool(nav.get("can_go_back")),
                can_go_forward=bool(nav.get("can_go_forward")),
            ).to_json()
        )

    async def _apply_control(self, session: Any, msg: BrowserControl) -> None:
        # Pause/takeover just flip the shared flag the MCP tools consult; no
        # driver call needed. Resume/release clear it. Echo the resulting state
        # so the viewer's pause indicator reflects it.
        if msg.action in ("pause_agent", "takeover"):
            session.agent_paused = True
            session.touch(actor="human")
        elif msg.action in ("resume_agent", "release"):
            session.agent_paused = False
            session.touch(actor="human")
        await self._send_agent_state(session)

    async def _send_agent_state(self, session: Any) -> None:
        state: Literal["acting", "paused", "idle"] = "paused" if session.agent_paused else "idle"
        await self._ws.send_json(
            BrowserAgentState(state=state, last_actor=session.last_actor).to_json()
        )

    def _schedule_cursor_probe(self, session: Any, fields: dict[str, Any]) -> None:
        """Kick a best-effort cursor probe for this move, if allowed. Cheap and
        synchronous: throttles by wall-clock and refuses to stack a second probe,
        so the input loop returns immediately regardless of probe latency."""
        if self._cursor_task is not None and not self._cursor_task.done():
            return  # one probe at a time — drop this move's probe, not the input
        now = asyncio.get_running_loop().time()
        if now - self._cursor_gate_at < _CURSOR_PROBE_MIN_INTERVAL_S:
            return
        x, y = fields.get("x"), fields.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return
        self._cursor_gate_at = now
        self._cursor_task = asyncio.create_task(
            self._probe_and_send_cursor(session, float(x), float(y))
        )

    async def _probe_and_send_cursor(self, session: Any, x: float, y: float) -> None:
        """Read the remote cursor at (x, y) and push it to THIS viewer if it
        changed. Fully self-contained: any failure (timeout, torn-down driver,
        closing socket) is swallowed so the cosmetic side-channel can never take
        down the connection or be reported as a session error."""
        try:
            raw = await asyncio.wait_for(session.driver.cursor_at(x, y), _CURSOR_PROBE_TIMEOUT_S)
        except (TimeoutError, asyncio.CancelledError):
            return
        except Exception:
            return  # driver closed mid-probe, evaluate raised, … — cosmetic, ignore
        cursor = normalize_cursor(raw if isinstance(raw, str) else "")
        if cursor == self._cursor_last_sent:
            return  # unchanged — don't spend a frame on it
        self._cursor_last_sent = cursor
        with contextlib.suppress(Exception):  # a closing socket must not raise here
            await self._ws.send_json(BrowserCursor(cursor=cursor).to_json())

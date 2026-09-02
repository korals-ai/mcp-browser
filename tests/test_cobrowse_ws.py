"""The co-browse WS: screencast out, acks/input/control in, viewer refcount."""

from __future__ import annotations

import asyncio
from typing import Any

from src.cobrowse_ws import CoBrowseConnection
from src.protocol import BrowserInput, parse_client_message
from tests.conftest import FakeDriver, make_manager


def _move(x: float, y: float) -> BrowserInput:
    return BrowserInput(event="mouse", fields={"kind": "move", "x": x, "y": y})


class FakeWs:
    """A scripted viewer socket: replays ``inbound`` then reports close, and
    records everything sent back."""

    def __init__(self, inbound: list[dict[str, Any]]) -> None:
        self._inbound = list(inbound)
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)

    async def receive_json(self) -> dict[str, Any] | None:
        if self._inbound:
            return self._inbound.pop(0)
        return None  # closed


async def test_connect_pushes_nav_and_starts_screencast() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    ws = FakeWs(inbound=[])
    await CoBrowseConnection(manager, "c1", ws).run()

    assert driver.screencast_started is True
    assert ws.sent[0]["type"] == "browser_nav"
    # last viewer left -> screencast stopped, session kept
    assert driver.screencast_stopped is True
    assert manager.get("c1") is not None


async def test_connect_to_paused_session_shows_paused_state() -> None:
    # The lock-out fix: a viewer attaching to an already-paused session must be
    # told it's paused on connect, so the Resume button renders.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    ws = FakeWs(inbound=[])
    await CoBrowseConnection(manager, "c1", ws).run()

    states = [m for m in ws.sent if m["type"] == "browser_agent_state"]
    assert states and states[0]["state"] == "paused"


async def test_frames_reach_the_viewer() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)

    # Hold the socket open until we've emitted a frame.
    gate = asyncio.Event()

    class GatedWs(FakeWs):
        async def receive_json(self) -> dict[str, Any] | None:
            await gate.wait()
            return None

    ws = GatedWs(inbound=[])
    task = asyncio.create_task(CoBrowseConnection(manager, "c1", ws).run())
    await asyncio.sleep(0)  # let run() start the screencast
    await driver.emit_frame("BASE64DATA", {"session_frame_id": 7, "width": 800, "height": 600})
    gate.set()
    await task

    frames = [m for m in ws.sent if m["type"] == "browser_frame"]
    assert frames and frames[0]["data"] == "BASE64DATA"
    assert frames[0]["frame_id"] == 7


async def test_screencast_egress_is_counted_after_send() -> None:
    # A delivered frame moves both the wire-egress counters (budget signal for the
    # quality/fps knobs). Counted per byte of the base64 payload.
    from src import metrics

    driver = FakeDriver()
    manager, _ = make_manager(driver)

    gate = asyncio.Event()

    class GatedWs(FakeWs):
        async def receive_json(self) -> dict[str, Any] | None:
            await gate.wait()
            return None

    ws = GatedWs(inbound=[])
    bytes_before = metrics._registry.get_sample_value("cobrowse_screencast_bytes_total") or 0.0
    frames_before = metrics._registry.get_sample_value("cobrowse_screencast_frames_total") or 0.0

    task = asyncio.create_task(CoBrowseConnection(manager, "c1", ws).run())
    await asyncio.sleep(0)
    await driver.emit_frame("BASE64DATA", {"session_frame_id": 1, "width": 800, "height": 600})
    gate.set()
    await task

    bytes_after = metrics._registry.get_sample_value("cobrowse_screencast_bytes_total") or 0.0
    frames_after = metrics._registry.get_sample_value("cobrowse_screencast_frames_total") or 0.0
    assert bytes_after == bytes_before + len("BASE64DATA")
    assert frames_after == frames_before + 1


async def test_frame_ack_reaches_driver() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    ws = FakeWs(inbound=[{"type": "browser_frame_ack", "frame_id": 42}])
    await CoBrowseConnection(manager, "c1", ws).run()
    assert driver.acked == [42]


async def test_input_forwarded_and_marks_human_actor() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    ws = FakeWs(inbound=[{"type": "browser_input", "event": "mouse", "fields": {"x": 10, "y": 20}}])
    await CoBrowseConnection(manager, "c1", ws).run()
    assert driver.inputs == [("mouse", {"x": 10, "y": 20})]
    assert manager.get("c1").last_actor == "human"


async def test_watch_only_viewer_input_is_dropped() -> None:
    # A view-grant viewer (can_drive=False) may WATCH but not act — its input
    # frames never reach the driver.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    ws = FakeWs(inbound=[{"type": "browser_input", "event": "mouse", "fields": {"x": 1, "y": 2}}])
    await CoBrowseConnection(manager, "c1", ws, can_drive=False).run()
    assert driver.inputs == []  # dropped
    assert driver.screencast_started is True  # but it still receives the stream


async def test_paste_forwarded_and_marks_human_actor() -> None:
    # A paste is an ordinary drive frame: same actor bookkeeping, same ACL gate as
    # a click (the next test), so the audit trail keeps naming the human.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    ws = FakeWs(inbound=[{"type": "browser_input", "event": "paste", "fields": {"text": "RFP-1"}}])
    await CoBrowseConnection(manager, "c1", ws).run()
    assert driver.inputs == [("paste", {"text": "RFP-1"})]
    assert manager.get("c1").last_actor == "human"


async def test_watch_only_viewer_paste_is_dropped() -> None:
    # Pasting is acting: a view-grant viewer must not be able to type into the
    # tenant's portal session through the clipboard either.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    ws = FakeWs(inbound=[{"type": "browser_input", "event": "paste", "fields": {"text": "RFP-1"}}])
    await CoBrowseConnection(manager, "c1", ws, can_drive=False).run()
    assert driver.inputs == []


async def test_watch_only_viewer_still_acks_frames() -> None:
    # Frame-acks are stream flow-control, not driving — they must still work for a
    # watch-only viewer or its screencast would stall.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    ws = FakeWs(inbound=[{"type": "browser_frame_ack", "frame_id": 42}])
    await CoBrowseConnection(manager, "c1", ws, can_drive=False).run()
    assert driver.acked == [42]


async def test_watch_only_viewer_cannot_end_the_session() -> None:
    # browser_end_session closes Chromium for everyone — a watch-only viewer must
    # not be able to trigger it.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    ws = FakeWs(inbound=[{"type": "browser_end_session"}])
    await CoBrowseConnection(manager, "c1", ws, can_drive=False).run()
    assert manager.get("c1") is not None  # session survived


async def test_pause_control_sets_flag_and_emits_state() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    ws = FakeWs(inbound=[{"type": "browser_control", "action": "pause_agent"}])
    await CoBrowseConnection(manager, "c1", ws).run()
    assert manager.get("c1").agent_paused is True
    states = [m for m in ws.sent if m["type"] == "browser_agent_state"]
    assert states and states[-1]["state"] == "paused"


async def test_resume_control_clears_flag_and_emits_idle() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    ws = FakeWs(inbound=[{"type": "browser_control", "action": "resume_agent"}])
    await CoBrowseConnection(manager, "c1", ws).run()
    assert session.agent_paused is False
    states = [m for m in ws.sent if m["type"] == "browser_agent_state"]
    assert states and states[-1]["state"] == "idle"


async def test_bad_frame_is_skipped_not_fatal() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    ws = FakeWs(inbound=[{"type": "garbage"}, {"type": "browser_frame_ack", "frame_id": 1}])
    await CoBrowseConnection(manager, "c1", ws).run()
    assert driver.acked == [1]  # good frame after the bad one still processed


async def test_two_viewers_both_get_frames_and_stop_on_last_leave() -> None:
    # Co-browse is multi-viewer (a 2nd tab or a 2nd authorized user): a single
    # frame emitted while both are attached must reach BOTH viewers (fan-out), and
    # the shared screencast must keep running until the LAST viewer leaves.
    driver = FakeDriver()
    manager, _ = make_manager(driver)

    class GatedWs(FakeWs):
        def __init__(self, gate: asyncio.Event) -> None:
            super().__init__(inbound=[])
            self._gate = gate

        async def receive_json(self) -> dict[str, Any] | None:
            await self._gate.wait()
            return None  # closed once the gate opens

    gate1, gate2 = asyncio.Event(), asyncio.Event()
    ws1, ws2 = GatedWs(gate1), GatedWs(gate2)
    t1 = asyncio.create_task(CoBrowseConnection(manager, "c1", ws1).run())
    await asyncio.sleep(0)  # let viewer 1 attach its sink
    t2 = asyncio.create_task(CoBrowseConnection(manager, "c1", ws2).run())
    await asyncio.sleep(0)  # let viewer 2 attach its sink

    # one frame while BOTH are attached -> both receive it
    await driver.emit_frame("FRAME1", {"session_frame_id": 3, "width": 800, "height": 600})

    # viewer 1 leaves; viewer 2 remains -> screencast NOT stopped
    gate1.set()
    await t1
    assert driver.screencast_stopped is False
    assert manager.get("c1").viewers == 1

    # last viewer leaves -> screencast stops, session kept
    gate2.set()
    await t2
    assert driver.screencast_stopped is True
    assert manager.get("c1") is not None

    for ws in (ws1, ws2):
        frames = [m for m in ws.sent if m["type"] == "browser_frame"]
        assert frames and frames[0]["data"] == "FRAME1", "each viewer must get the frame"


async def test_viewer_stops_cleanly_when_another_ends_the_session() -> None:
    # Multi-viewer: when viewer A ends co-browse (closes Chromium for everyone),
    # viewer B's next message must NOT be dispatched onto the torn-down driver —
    # that would raise and be miscounted as a session error. B's loop exits clean.
    driver = FakeDriver()
    manager, _ = make_manager(driver)

    b_gate = asyncio.Event()

    class BWs(FakeWs):
        def __init__(self) -> None:
            super().__init__(inbound=[])
            self._step = 0

        async def receive_json(self) -> dict[str, Any] | None:
            self._step += 1
            if self._step == 1:
                await b_gate.wait()  # hold until viewer A has ended the session
                return {"type": "browser_input", "event": "mouse", "fields": {"x": 1, "y": 2}}
            return None  # then close

    b_ws = BWs()
    b_task = asyncio.create_task(CoBrowseConnection(manager, "c1", b_ws).run())
    await asyncio.sleep(0)  # let B attach to the shared session

    # Viewer A ends the session (closes the driver + drops the session).
    a_ws = FakeWs(inbound=[{"type": "browser_end_session"}])
    await CoBrowseConnection(manager, "c1", a_ws).run()
    assert manager.get("c1") is None

    # Release B's queued input: it must be dropped, not sent to the dead driver.
    b_gate.set()
    await b_task  # must NOT raise

    assert driver.inputs == [], "B's input must not reach the torn-down driver"


def test_parse_accepts_a_paste_input_event() -> None:
    msg = parse_client_message(
        {"type": "browser_input", "event": "paste", "fields": {"text": "hi"}}
    )
    assert msg.event == "paste"
    assert msg.fields == {"text": "hi"}


def test_parse_rejects_unknown_input_event() -> None:
    try:
        parse_client_message({"type": "browser_input", "event": "telepathy", "fields": {}})
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_parse_rejects_unknown_type() -> None:
    try:
        parse_client_message({"type": "nope"})
    except ValueError:
        return
    raise AssertionError("expected ValueError")


# --- cursor mirroring (cosmetic, best-effort, per-viewer) -------------------


async def test_mouse_move_dispatches_input_then_mirrors_cursor() -> None:
    driver = FakeDriver()
    driver.cursor_value = "pointer"
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    conn = CoBrowseConnection(manager, "c1", FakeWs(inbound=[]))

    await conn._dispatch(session, _move(10, 20))

    # The input is dispatched FIRST and unconditionally — the cursor probe is a
    # separate out-of-band task, so input never waits on it.
    assert driver.inputs == [("mouse", {"kind": "move", "x": 10, "y": 20})]
    assert conn._cursor_task is not None
    await conn._cursor_task  # let the probe run

    cursors = [m for m in conn._ws.sent if m["type"] == "browser_cursor"]
    assert cursors and cursors[0]["cursor"] == "pointer"
    assert driver.cursor_calls == [(10.0, 20.0)]


async def test_cursor_probe_failure_never_breaks_dispatch() -> None:
    driver = FakeDriver()

    async def boom(_x: float, _y: float) -> str:
        raise RuntimeError("driver torn down mid-probe")

    driver.cursor_at = boom  # type: ignore[method-assign]
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    conn = CoBrowseConnection(manager, "c1", FakeWs(inbound=[]))

    await conn._dispatch(session, _move(1, 2))  # must not raise
    assert conn._cursor_task is not None
    await conn._cursor_task  # the swallowed failure completes cleanly

    assert [m for m in conn._ws.sent if m["type"] == "browser_cursor"] == []


async def test_cursor_only_sent_on_change() -> None:
    driver = FakeDriver()
    driver.cursor_value = "pointer"
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    conn = CoBrowseConnection(manager, "c1", FakeWs(inbound=[]))

    await conn._probe_and_send_cursor(session, 1, 1)  # pointer -> sent
    await conn._probe_and_send_cursor(session, 2, 2)  # still pointer -> deduped
    driver.cursor_value = "text"
    await conn._probe_and_send_cursor(session, 3, 3)  # changed -> sent

    sent = [m["cursor"] for m in conn._ws.sent if m["type"] == "browser_cursor"]
    assert sent == ["pointer", "text"]


async def test_watch_only_viewer_gets_no_cursor_probe() -> None:
    # A watch-only viewer's drive frames (incl. mouse move) are dropped before
    # send_input — so no cursor probe leaks for a viewer that can't drive.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    conn = CoBrowseConnection(manager, "c1", FakeWs(inbound=[]), can_drive=False)

    await conn._dispatch(session, _move(5, 5))

    assert driver.inputs == []
    assert driver.cursor_calls == []
    assert conn._cursor_task is None


async def test_cursor_probe_is_single_in_flight() -> None:
    # While one probe is running, a second move must NOT stack another probe (it
    # drops the probe, never the input) — bounding probe concurrency to 1/viewer.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    conn = CoBrowseConnection(manager, "c1", FakeWs(inbound=[]))

    async def never() -> None:
        await asyncio.Event().wait()

    conn._cursor_task = asyncio.create_task(never())
    try:
        conn._schedule_cursor_probe(session, {"kind": "move", "x": 1, "y": 1})
        assert driver.cursor_calls == []  # no new probe while one is in flight
    finally:
        conn._cursor_task.cancel()

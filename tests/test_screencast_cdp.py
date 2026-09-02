"""CDP-level screencast/tab tests for :class:`PlaywrightDriver`.

``FakeDriver`` (conftest) models the driver *Protocol* but NOT the CDP session
layer beneath it, so the tab-switch / close-tab screencast paths — which each
target a *specific tab's* CDP session — were never unit-covered (the driver's
screencast code sat at ~40% coverage, exercised only against real Chromium).

``FakeCDP`` records ``on`` / ``remove_listener`` / ``send`` so a test can assert
exactly **which** session a screencast start/stop/frame-ack hit. It's the harness
the two latent tab-switch bugs are driven from:

* **F3** — closing the streaming tab must stop the screencast on the *closing*
  tab's own CDP session (and drop its frame listener), not on the replacement.
* **F4** — a screencast frame still in flight from the *old* tab, arriving after
  the active tab switched, must be acked to the session it *came from*, not to
  whatever tab is active at callback time.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from src import metrics
from src.browser_driver import (
    _MAX_VIEWPORT_H,
    _MAX_VIEWPORT_W,
    _MIN_FRAME_INTERVAL_S,
    _MIN_VIEWPORT_H,
    _MIN_VIEWPORT_W,
    PlaywrightDriver,
    _Tab,
)


def _counter(name: str) -> float:
    return metrics._registry.get_sample_value(name) or 0.0


class FakeCDP:
    """Records CDP calls for one tab's session. ``send`` is async (like the real
    ``CDPSession.send``); ``on`` / ``remove_listener`` are sync (pyee emitter)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.acks: list[str | None] = []
        self.listeners: dict[str, list[Any]] = {}
        self.removed: list[Any] = []

    def on(self, event: str, handler: Any) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        self.removed.append(handler)
        lst = self.listeners.get(event, [])
        if handler in lst:
            lst.remove(handler)

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        self.sent.append((method, params))
        if method == "Page.screencastFrameAck":
            self.acks.append(params.get("sessionId"))
        return {}

    @property
    def started(self) -> bool:
        return any(m == "Page.startScreencast" for m, _ in self.sent)

    @property
    def stopped(self) -> bool:
        return any(m == "Page.stopScreencast" for m, _ in self.sent)

    @property
    def frame_listeners(self) -> list[Any]:
        return list(self.listeners.get("Page.screencastFrame", []))


class FakePage:
    """The handful of page methods the tab/screencast paths touch."""

    def __init__(self) -> None:
        self.url = "about:blank"
        self.closed = False
        self.screenshot_kwargs: dict[str, Any] | None = None
        self.viewport_sizes: list[dict[str, int]] = []

    async def set_viewport_size(self, size: dict[str, int]) -> None:
        self.viewport_sizes.append(size)

    async def bring_to_front(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def title(self) -> str:
        return "Fake"

    async def screenshot(self, **kwargs: Any) -> bytes:
        self.screenshot_kwargs = kwargs
        return b"png-bytes"


async def _noop_sink(_data: str, _meta: dict[str, Any]) -> None:
    return None


def _two_tab_driver() -> tuple[PlaywrightDriver, FakeCDP, FakeCDP]:
    """A driver with two tabs (t1 active), each backed by a FakeCDP — no Chromium."""
    drv = PlaywrightDriver()
    t1_cdp, t2_cdp = FakeCDP("t1"), FakeCDP("t2")
    drv._tabs = [_Tab("t1", FakePage(), t1_cdp), _Tab("t2", FakePage(), t2_cdp)]
    drv._active_id = "t1"
    return drv, t1_cdp, t2_cdp


async def _cancel_pump(drv: PlaywrightDriver) -> None:
    """Stop the background screencast pump add_frame_sink starts (no real browser
    to close, so tear the task down directly)."""
    if drv._pump_task is not None:
        drv._pump_task.cancel()
        with contextlib.suppress(BaseException):
            await drv._pump_task


async def test_closing_the_active_tab_stops_screencast_on_that_tabs_own_cdp() -> None:
    """F3: closing the streaming tab stops the screencast on the CLOSING tab's own
    CDP session (and drops its frame listener) — not on the replacement tab we're
    about to activate."""
    drv, t1_cdp, t2_cdp = _two_tab_driver()
    await drv.add_frame_sink(_noop_sink)  # start streaming the active tab (t1)
    try:
        assert t1_cdp.started and not t2_cdp.started

        await drv.close_tab("t1")

        assert t1_cdp.stopped, "closing tab's screencast was never stopped (F3)"
        assert t1_cdp.frame_listeners == [], "closing tab kept a leaked frame listener (F3)"
        assert t2_cdp.started, "replacement tab's screencast was not started"
    finally:
        await _cancel_pump(drv)


async def test_late_frame_after_switch_is_acked_to_its_origin_session() -> None:
    """F4: a screencast frame from the OLD tab, arriving after the active tab has
    switched, is acked to the session it CAME FROM — not to whatever tab is active
    at callback time."""
    drv, t1_cdp, t2_cdp = _two_tab_driver()
    await drv.add_frame_sink(_noop_sink)  # streaming t1
    try:
        late_handler = t1_cdp.frame_listeners[0]  # the listener bound to t1's stream

        await drv._activate("t2")  # user switches tabs; capture moves to t2

        # A t1 frame was already in flight and now invokes the old listener:
        await late_handler({"sessionId": "sess-t1", "data": "AAAA", "metadata": {}})

        assert t1_cdp.acks == ["sess-t1"], "late frame not acked to its origin session (F4)"
        assert t2_cdp.acks == [], "late frame wrongly acked to the now-active session (F4)"
    finally:
        await _cancel_pump(drv)


async def test_healthy_tab_switch_and_close_never_flag_a_teardown_mismatch() -> None:
    """The teardown-mismatch counter is a pure regression signal: an ordinary
    switch (both ways) plus a close of the streaming tab must leave it untouched."""
    drv, _t1_cdp, _t2_cdp = _two_tab_driver()
    await drv.add_frame_sink(_noop_sink)
    try:
        before = _counter("cobrowse_screencast_teardown_mismatch_total")
        await drv.switch_tab("t2")  # move the capture t1 -> t2
        await drv.switch_tab("t1")  # and back
        await drv.close_tab("t1")  # close the streaming tab
        assert _counter("cobrowse_screencast_teardown_mismatch_total") == before
    finally:
        await _cancel_pump(drv)


async def test_screencast_starts_with_the_pinned_picture_params() -> None:
    """Pin the CDP capture params as literals: quality 85 was chosen WITH the 2x
    device scale and the pump's rate cap (see the _DEVICE_SCALE comment block) —
    a drive-by tweak back to a low quality (or a format change) should have to
    edit this test and read that reasoning."""
    drv, t1_cdp, _t2_cdp = _two_tab_driver()
    await drv.add_frame_sink(_noop_sink)
    try:
        params = next(p for m, p in t1_cdp.sent if m == "Page.startScreencast")
        assert params == {"format": "jpeg", "quality": 85, "everyNthFrame": 1}
    finally:
        await _cancel_pump(drv)


def test_fan_out_cap_is_pinned_at_15_fps() -> None:
    """Pin the fan-out rate cap as a literal: 15 fps (0.066 s) was chosen to keep
    scrolling/cursor motion smooth while bounding per-viewer egress, and the
    CoBrowseEgressHigh alert's threshold is derived from exactly this rate. A
    drive-by change to the interval must edit this test and re-derive that ceiling
    (infra/grafana_cloud/cobrowse_alerts.tf)."""
    assert _MIN_FRAME_INTERVAL_S == 0.066
    fps = 1.0 / _MIN_FRAME_INTERVAL_S
    assert 14.5 <= fps <= 15.5  # ~15 fps, not the old ~10


async def test_frame_sinks_gauge_tracks_attach_and_detach() -> None:
    """The sink-leak guard: the gauge follows the driver's live sink count, so a
    positive reading while no viewer is attached (cobrowse_active_viewers == 0)
    exposes the attach/teardown race."""
    # A fresh driver starts with an empty sink set, so set_frame_sinks writes the
    # ABSOLUTE count (1 after attach, 0 after detach) — deterministic regardless of
    # a stale gauge value left by an earlier test on the shared module registry.
    drv, _t1_cdp, _t2_cdp = _two_tab_driver()
    await drv.add_frame_sink(_noop_sink)
    try:
        assert _counter("cobrowse_frame_sinks") == 1
    finally:
        await drv.remove_frame_sink(_noop_sink)
    assert _counter("cobrowse_frame_sinks") == 0


async def test_pump_rate_cap_coalesces_bursts_to_the_freshest_frame() -> None:
    """A burst of CDP frames faster than the fan-out cap yields ONE delivery —
    of the freshest frame, not the first queued — and no duplicate re-send of an
    already-delivered frame afterwards."""
    drv, t1_cdp, _t2_cdp = _two_tab_driver()
    drv._min_frame_interval = 0.05
    received: list[str] = []

    async def sink(data: str, _meta: dict[str, Any]) -> None:
        received.append(data)

    await drv.add_frame_sink(sink)
    try:
        handler = t1_cdp.frame_listeners[0]
        await handler({"sessionId": "s", "data": "f1", "metadata": {}})
        await asyncio.sleep(0.01)  # within the cap: first frame goes out at once
        assert received == ["f1"]

        for data in ("f2", "f3", "f4", "f5"):  # burst well inside one interval
            await handler({"sessionId": "s", "data": data, "metadata": {}})
        # Wait past several intervals: the burst must collapse to exactly one
        # more delivery (the freshest), with no trailing duplicates.
        await asyncio.sleep(0.2)
        assert received == ["f1", "f5"]
    finally:
        await _cancel_pump(drv)


async def test_set_viewport_resizes_every_tab_and_updates_frame_fallback() -> None:
    """A human panel resize is applied to ALL tabs (the viewport is otherwise a
    context default new tabs inherit) and updates the frame-metadata fallback so a
    later frame with no CDP deviceWidth still reports the resized size."""
    drv, _t1_cdp, _t2_cdp = _two_tab_driver()

    await drv.set_viewport(900, 600)

    for tab in drv._tabs:
        page = tab.page
        assert isinstance(page, FakePage)
        assert page.viewport_sizes == [{"width": 900, "height": 600}]
    assert drv._viewport == (900, 600)


async def test_set_viewport_clamps_out_of_range_and_skips_no_op() -> None:
    """Sizes are clamped to sane bounds, and an unchanged size is a no-op so a
    debounced resize storm doesn't thrash CDP."""
    drv, _t1_cdp, _t2_cdp = _two_tab_driver()

    await drv.set_viewport(20, 20)  # below the floor
    assert drv._viewport == (_MIN_VIEWPORT_W, _MIN_VIEWPORT_H)
    page = drv._tabs[0].page
    assert isinstance(page, FakePage)
    assert page.viewport_sizes == [{"width": _MIN_VIEWPORT_W, "height": _MIN_VIEWPORT_H}]

    await drv.set_viewport(_MIN_VIEWPORT_W, _MIN_VIEWPORT_H)  # same size → no CDP call
    assert page.viewport_sizes == [{"width": _MIN_VIEWPORT_W, "height": _MIN_VIEWPORT_H}]

    await drv.set_viewport(99999, 99999)  # above the ceiling
    assert drv._viewport == (_MAX_VIEWPORT_W, _MAX_VIEWPORT_H)


async def test_agent_screenshot_stays_css_scaled() -> None:
    """The agent's screenshot must request scale="css": the context renders at
    2x device scale for the human screencast, and without this cap every agent
    screenshot would carry 4x the pixels into model context — a silent
    token-cost multiplier on every browsing step (human watching or not)."""
    drv, _t1_cdp, _t2_cdp = _two_tab_driver()
    await drv.screenshot()
    page = drv._tabs[0].page
    assert isinstance(page, FakePage)
    assert page.screenshot_kwargs == {"type": "png", "scale": "css"}


async def test_late_frame_is_counted_for_observability() -> None:
    """The F4 late frame (arriving from a switched-away session) is measured, so
    the tab-switch race is visible in prod instead of silently swallowed."""
    drv, t1_cdp, _t2_cdp = _two_tab_driver()
    await drv.add_frame_sink(_noop_sink)
    try:
        late_handler = t1_cdp.frame_listeners[0]
        await drv._activate("t2")
        before = _counter("cobrowse_late_frames_total")
        await late_handler({"sessionId": "sess-t1", "data": "AAAA", "metadata": {}})
        assert _counter("cobrowse_late_frames_total") == before + 1
    finally:
        await _cancel_pump(drv)

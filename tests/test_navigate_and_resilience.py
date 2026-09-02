"""Human address-bar navigation + viewer-send resilience.

Two fixes, both from a real prod session:
  1. A human can type a URL in the address bar to navigate the active tab
     (``browser_navigate``) — the "+" new-tab was useless without it.
  2. A frame pushed to a viewer that is mid-disconnect must NOT tear down the
     whole co-browse handler (which silently dropped the human's next click,
     e.g. a tab-close, until they reconnected).
"""

from __future__ import annotations

from typing import Any

import pytest

from src import metrics
from src.cobrowse_ws import CoBrowseConnection
from src.protocol import BrowserNavigate, BrowserResize, normalize_url, parse_client_message
from tests.conftest import FakeDriver, make_manager


class _FakeWs:
    def __init__(self, inbound: list[dict[str, Any]]) -> None:
        self._inbound = list(inbound)
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)

    async def receive_json(self) -> dict[str, Any] | None:
        return self._inbound.pop(0) if self._inbound else None


# ---- URL normalization -----------------------------------------------------


def test_normalize_url_adds_scheme_to_bare_host() -> None:
    assert normalize_url("example.com") == "https://example.com"
    assert normalize_url("  example.com/path  ") == "https://example.com/path"


def test_normalize_url_leaves_schemed_inputs() -> None:
    assert normalize_url("http://x.com") == "http://x.com"
    assert normalize_url("https://x.com") == "https://x.com"
    assert normalize_url("about:blank") == "about:blank"


def test_parse_navigate_frame() -> None:
    assert parse_client_message({"type": "browser_navigate", "url": "x.com"}) == BrowserNavigate(
        "https://x.com"
    )


def test_parse_navigate_rejects_empty_url() -> None:
    for bad in ({"type": "browser_navigate"}, {"type": "browser_navigate", "url": "  "}):
        with pytest.raises(ValueError, match="navigate"):
            parse_client_message(bad)


# ---- human navigation reaches the driver -----------------------------------


async def test_human_navigate_opens_active_tab() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    ws = _FakeWs(inbound=[{"type": "browser_navigate", "url": "example.com"}])
    await CoBrowseConnection(manager, "c1", ws).run()
    # navigated the ACTIVE tab (no new tab), and re-broadcast nav + tabs
    assert driver.opened == ["https://example.com"]
    assert any(m["type"] == "browser_nav" for m in ws.sent)


# ---- viewport resize (responsive co-browse panel) --------------------------


def test_parse_resize_frame() -> None:
    assert parse_client_message(
        {"type": "browser_resize", "width": 900, "height": 600}
    ) == BrowserResize(width=900, height=600)


def test_parse_resize_rejects_bad_dims() -> None:
    for bad in (
        {"type": "browser_resize", "width": 900},  # missing height
        {"type": "browser_resize", "width": 0, "height": 600},  # non-positive
        {"type": "browser_resize", "width": -5, "height": 600},
        {"type": "browser_resize", "width": 900.5, "height": 600},  # non-int
        {"type": "browser_resize", "width": True, "height": 600},  # bool is not a size
    ):
        with pytest.raises(ValueError, match="resize"):
            parse_client_message(bad)


async def test_human_resize_reaches_driver_without_claiming_actor() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    ws = _FakeWs(inbound=[{"type": "browser_resize", "width": 1024, "height": 720}])
    await CoBrowseConnection(manager, "c1", ws).run()
    assert driver.viewports == [(1024, 720)]
    # A passive panel resize keeps the session alive but does not flip last_actor.
    assert manager.get("c1") is None or manager.get("c1").last_actor is None


# ---- send resilience: a disconnecting viewer must not kill the session -----


class _RaisingWs:
    """Starlette-ish ws whose send raises the first time (client mid-disconnect),
    like the prod traceback (WebSocketDisconnect / RuntimeError on a closing ws)."""

    def __init__(self, exc: Exception) -> None:
        self._exc: Exception | None = exc
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        if self._exc is not None:
            exc, self._exc = self._exc, None
            raise exc
        self.sent.append(data)


@pytest.mark.parametrize("exc", [RuntimeError("connection is closing"), ValueError("boom")])
async def test_adapter_swallows_disconnect_but_not_bugs(exc: Exception) -> None:
    from starlette.websockets import WebSocketDisconnect

    from src.server import _StarletteWsAdapter

    # A disconnect-class error is swallowed; a real bug still surfaces.
    disconnecting = _StarletteWsAdapter(_RaisingWs(WebSocketDisconnect(code=1006)))
    await disconnecting.send_json({"type": "browser_nav"})  # must NOT raise

    runtime = _StarletteWsAdapter(_RaisingWs(RuntimeError("once a close message has been sent")))
    await runtime.send_json({"type": "browser_nav"})  # must NOT raise

    unexpected = _StarletteWsAdapter(_RaisingWs(exc))
    if isinstance(exc, RuntimeError):
        await unexpected.send_json({"type": "x"})  # RuntimeError is swallowed
    else:
        with pytest.raises(ValueError):
            await unexpected.send_json({"type": "x"})  # a real bug is NOT hidden


# ---- recv resilience: a disconnect discovered on the SEND side (the screencast
# ---- pump) flips Starlette's application_state, so the recv guard raises a BARE
# ---- RuntimeError, not WebSocketDisconnect. Catching only the latter let it
# ---- escape and kill the handler, inflating cobrowse_session_errors_total on
# ---- ordinary session-ends. The recv adapter must treat it as a clean close.


class _RecvRaisingWs:
    """A ws whose receive_json raises the EXACT error Starlette 1.3.1 raises when
    application_state != CONNECTED (a peer send already saw the disconnect)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.recv_calls = 0

    async def send_json(self, data: dict[str, Any]) -> None:  # pragma: no cover
        pass

    async def receive_json(self) -> dict[str, Any] | None:
        self.recv_calls += 1
        raise self._exc


async def test_recv_side_runtimeerror_is_clean_close_not_session_error() -> None:
    from src.server import _StarletteWsAdapter

    # The literal message Starlette 1.3.1 raises (reproduced against the real lib).
    ws = _RecvRaisingWs(RuntimeError('WebSocket is not connected. Need to call "accept" first.'))
    adapter = _StarletteWsAdapter(ws)
    before = metrics._registry.get_sample_value("cobrowse_recv_closes_total") or 0.0

    result = await adapter.receive_json()  # must NOT raise (that would kill the handler)

    assert result is None  # recv loop reads this as "closed" and exits cleanly
    after = metrics._registry.get_sample_value("cobrowse_recv_closes_total") or 0.0
    assert after == before + 1  # counted as a benign close, NOT a session error


async def test_adapter_short_circuits_both_directions_once_closed() -> None:
    from starlette.websockets import WebSocketDisconnect

    from src.server import _StarletteWsAdapter

    class _CountingWs:
        def __init__(self) -> None:
            self.send_calls = 0
            self.recv_calls = 0
            self._first_send = True

        async def send_json(self, data: dict[str, Any]) -> None:
            self.send_calls += 1
            if self._first_send:
                self._first_send = False
                raise WebSocketDisconnect(code=1006)  # first send discovers the death

        async def receive_json(self) -> dict[str, Any] | None:
            self.recv_calls += 1
            return {"type": "x"}

    ws = _CountingWs()
    adapter = _StarletteWsAdapter(ws)

    await adapter.send_json({"a": 1})  # first send fails -> marks closed
    assert ws.send_calls == 1

    # once closed, BOTH directions short-circuit without touching the real socket
    await adapter.send_json({"a": 2})
    assert ws.send_calls == 1, "a closed adapter must not send again"
    assert await adapter.receive_json() is None
    assert ws.recv_calls == 0, "a closed adapter must not receive again"

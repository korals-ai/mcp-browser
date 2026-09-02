"""Human "End" — closes the Chromium session on the pod.

Co-browse can't be turned off from the timeline (the tool call is permanent
history), so the human needs an explicit end that frees the browser. After it,
the session is gone and the next get_or_create mints a fresh one.
"""

from __future__ import annotations

from typing import Any

from src import metrics
from src.cobrowse_ws import CoBrowseConnection
from src.protocol import BrowserEndSession, parse_client_message
from tests.conftest import FakeDriver, make_manager


class _FakeWs:
    def __init__(self, inbound: list[dict[str, Any]]) -> None:
        self._inbound = list(inbound)
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)

    async def receive_json(self) -> dict[str, Any] | None:
        return self._inbound.pop(0) if self._inbound else None


def test_parse_end_session_frame() -> None:
    assert parse_client_message({"type": "browser_end_session"}) == BrowserEndSession()


async def test_end_session_closes_the_session() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    before = metrics._registry.get_sample_value("cobrowse_sessions_ended_total") or 0.0

    ws = _FakeWs(inbound=[{"type": "browser_end_session"}])
    await CoBrowseConnection(manager, "c1", ws).run()

    # Session dropped from the manager + Chromium torn down.
    assert manager.get("c1") is None
    assert driver.closed is True
    assert (
        metrics._registry.get_sample_value("cobrowse_sessions_ended_total") or 0.0
    ) == before + 1


async def test_next_session_after_end_is_fresh() -> None:
    # No fixed driver → the factory mints a fresh FakeDriver per session.
    manager, created = make_manager()
    first = await manager.get_or_create("c1")
    ws = _FakeWs(inbound=[{"type": "browser_end_session"}])
    await CoBrowseConnection(manager, "c1", ws).run()
    assert manager.get("c1") is None

    # A reconnect mints a brand-new session (a different driver instance).
    reopened = await manager.get_or_create("c1")
    assert reopened.driver is not first.driver
    assert len(created) == 2

"""Co-browse pod metrics: the counters/gauge move on the real code paths.

Reads values straight off the module's dedicated registry (not the global
default) so assertions are exact and don't depend on scrape formatting.
"""

from __future__ import annotations

from typing import Any

from src import metrics


def _val(name: str) -> float:
    return metrics._registry.get_sample_value(name) or 0.0


def test_render_exposes_all_cobrowse_series() -> None:
    body, content_type = metrics.render()
    text = body.decode()
    assert "text/plain" in content_type
    for name in (
        "cobrowse_viewer_connections_total",
        "cobrowse_active_viewers",
        "cobrowse_session_errors_total",
        "cobrowse_send_drops_total",
        "cobrowse_recv_closes_total",
        "cobrowse_chromium_launches_total",
        "cobrowse_chromium_launch_failures_total",
        "cobrowse_screencast_teardown_mismatch_total",
        "cobrowse_late_frames_total",
        "cobrowse_screencast_bytes_total",
        "cobrowse_screencast_frames_total",
        "cobrowse_frame_sinks",
    ):
        assert name in text, name


def test_counters_increment() -> None:
    before = {
        n: _val(n)
        for n in (
            "cobrowse_viewer_connections_total",
            "cobrowse_session_errors_total",
            "cobrowse_send_drops_total",
            "cobrowse_recv_closes_total",
            "cobrowse_chromium_launches_total",
            "cobrowse_chromium_launch_failures_total",
            "cobrowse_screencast_teardown_mismatch_total",
            "cobrowse_late_frames_total",
        )
    }
    metrics.inc_viewer_connection()
    metrics.inc_session_error()
    metrics.inc_send_drop()
    metrics.inc_recv_close()
    metrics.inc_chromium_launch()
    metrics.inc_chromium_launch_failure()
    metrics.inc_screencast_teardown_mismatch()
    metrics.inc_late_frame()
    for n, b in before.items():
        assert _val(n) == b + 1, n


def test_active_viewers_gauge_up_then_down() -> None:
    start = _val("cobrowse_active_viewers")
    metrics.viewer_attached()
    assert _val("cobrowse_active_viewers") == start + 1
    metrics.viewer_detached()
    assert _val("cobrowse_active_viewers") == start


def test_screencast_egress_counters_move() -> None:
    # The picture-budget signals: bytes counts wire egress, frames counts fan-out.
    bytes_before = _val("cobrowse_screencast_bytes_total")
    frames_before = _val("cobrowse_screencast_frames_total")
    metrics.add_screencast_bytes(1500)
    metrics.inc_screencast_frame()
    assert _val("cobrowse_screencast_bytes_total") == bytes_before + 1500
    assert _val("cobrowse_screencast_frames_total") == frames_before + 1


def test_frame_sinks_gauge_is_set_absolute() -> None:
    # set() (not inc/dec) so the gauge can't drift from the driver's sink count.
    metrics.set_frame_sinks(2)
    assert _val("cobrowse_frame_sinks") == 2
    metrics.set_frame_sinks(0)
    assert _val("cobrowse_frame_sinks") == 0


async def test_adapter_drop_increments_send_drops() -> None:
    # A frame pushed to a disconnecting viewer is dropped AND counted.
    from starlette.websockets import WebSocketDisconnect

    from src.server import _StarletteWsAdapter

    class _DyingWs:
        async def send_json(self, data: dict[str, Any]) -> None:
            raise WebSocketDisconnect(code=1006)

    before = _val("cobrowse_send_drops_total")
    await _StarletteWsAdapter(_DyingWs()).send_json({"type": "browser_nav"})
    assert _val("cobrowse_send_drops_total") == before + 1

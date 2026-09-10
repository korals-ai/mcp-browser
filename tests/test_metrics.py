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
        "cobrowse_viewers_without_frame_total",
        "cobrowse_frame_send_failures_total",
        "cobrowse_slow_frame_sends_total",
        "cobrowse_replay_frame_age_seconds",
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


def test_viewer_attach_outcomes_are_labelled_not_summed() -> None:
    """The three attach outcomes must stay separable: "a late viewer attached"
    and "a late viewer attached and got nothing" are the same event to every
    other signal, and only the label tells them apart."""
    before = {
        o: metrics._registry.get_sample_value("cobrowse_viewer_attaches_total", {"outcome": o})
        or 0.0
        for o in ("first", "replayed", "no_cached_frame")
    }
    metrics.inc_viewer_attach("first")
    metrics.inc_viewer_attach("replayed")
    metrics.inc_viewer_attach("replayed")
    metrics.inc_viewer_attach("no_cached_frame")
    after = {
        o: metrics._registry.get_sample_value("cobrowse_viewer_attaches_total", {"outcome": o})
        or 0.0
        for o in ("first", "replayed", "no_cached_frame")
    }
    assert after["first"] == before["first"] + 1
    assert after["replayed"] == before["replayed"] + 2
    assert after["no_cached_frame"] == before["no_cached_frame"] + 1


def test_never_painted_and_send_health_counters_move() -> None:
    before = {
        n: _val(n)
        for n in (
            "cobrowse_viewers_without_frame_total",
            "cobrowse_frame_send_failures_total",
            "cobrowse_slow_frame_sends_total",
        )
    }
    metrics.inc_viewer_without_frame()
    metrics.inc_frame_send_failure()
    metrics.inc_slow_frame_send()
    for n, b in before.items():
        assert _val(n) == b + 1, n


def test_first_frame_histogram_separates_first_from_late_joins() -> None:
    """A cold first paint (seconds) and a cached late paint (milliseconds) must
    not share a distribution — averaged together, neither is readable."""
    late_before = (
        metrics._registry.get_sample_value(
            "cobrowse_viewer_first_frame_seconds_count", {"join": "late"}
        )
        or 0.0
    )
    metrics.observe_first_frame("late", 0.05)
    metrics.observe_first_frame("first", 8.0)
    assert (
        metrics._registry.get_sample_value(
            "cobrowse_viewer_first_frame_seconds_count", {"join": "late"}
        )
        == late_before + 1
    )
    # The late observation lands in the bottom bucket; that IS the regression test
    # for replay-on-attach — a late join drifting upward means it stopped working.
    assert (
        metrics._registry.get_sample_value(
            "cobrowse_viewer_first_frame_seconds_bucket", {"join": "late", "le": "0.25"}
        )
        == late_before + 1
    )


def test_replay_frame_age_is_observed() -> None:
    before = _val("cobrowse_replay_frame_age_seconds_count")
    metrics.observe_replay_frame_age(3.5)
    assert _val("cobrowse_replay_frame_age_seconds_count") == before + 1
    assert _val("cobrowse_replay_frame_age_seconds_sum") >= 3.5

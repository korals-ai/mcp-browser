"""Prometheus metrics for the co-browse browser pod (one series per pod).

The browser pod is per-tenant, fixed single replica, so each metric is a single
per-pod series; prometheus attaches the pod's ``platform_tenant_id`` label at
scrape time (the PodMonitor in the prometheus overlay). Scraped at ``/metrics``
on the pod's one port (8096), alongside ``/mcp`` and ``/cobrowse``.

Kept in its own module (dedicated registry, not the global default) so the gauges,
counters, and text render can be unit-tested without booting the ASGI app — the
same shape as ``apps/workspace/src/metrics.py``.

Signals worth alerting on:
  * ``cobrowse_session_errors_total`` — the ``/cobrowse`` handler died unexpectedly
    (the class of bug where a failed send tore down the session and silently
    dropped the human's next input). Any sustained rate is a regression.
  * ``cobrowse_chromium_launch_failures_total`` — Chromium wouldn't start, so
    co-browse is dead for that tenant.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)

_registry = CollectorRegistry()

_viewer_connections = Counter(
    "cobrowse_viewer_connections_total",
    "Total /cobrowse viewer WebSocket connections accepted by this pod.",
    registry=_registry,
)
_active_viewers = Gauge(
    "cobrowse_active_viewers",
    "Viewers currently attached to this pod's co-browse session.",
    registry=_registry,
)
_session_errors = Counter(
    "cobrowse_session_errors_total",
    "Times the /cobrowse handler ended with an unexpected error (not a clean "
    "disconnect). The regression signal for a torn-down session.",
    registry=_registry,
)
_send_drops = Counter(
    "cobrowse_send_drops_total",
    "Frames dropped because the viewer was mid-disconnect. Expected on a normal "
    "close; a high sustained rate means viewers are flapping.",
    registry=_registry,
)
_recv_closes = Counter(
    "cobrowse_recv_closes_total",
    "Recv loop ended because the viewer socket was already gone (a normal "
    "disconnect race, discovered on the receive side). NOT an error — this is the "
    "clean-close counterpart to cobrowse_session_errors_total, which stays a pure "
    "regression signal.",
    registry=_registry,
)
_chromium_launches = Counter(
    "cobrowse_chromium_launches_total",
    "Times Chromium was lazily launched (a new co-browse session started).",
    registry=_registry,
)
_chromium_launch_failures = Counter(
    "cobrowse_chromium_launch_failures_total",
    "Times Chromium failed to launch — co-browse is unavailable for this tenant.",
    registry=_registry,
)
_sessions_ended = Counter(
    "cobrowse_sessions_ended_total",
    "Times a human explicitly ended the co-browse session (closed Chromium).",
    registry=_registry,
)
_screencast_teardown_mismatch = Counter(
    "cobrowse_screencast_teardown_mismatch_total",
    "Times a screencast stop targeted a tab whose CDP session was NOT the active "
    "capture — the signature of the close/switch ordering bug (teardown aimed at "
    "the wrong session, leaking the real one). Should stay 0; any increase is a "
    "regression.",
    registry=_registry,
)
_late_frames = Counter(
    "cobrowse_late_frames_total",
    "Screencast frames that arrived from a CDP session which was no longer the "
    "active capture — an in-flight frame delivered mid tab-switch. Acked to their "
    "origin session (not the newly-active one). Diagnostic: measures how often the "
    "tab-switch race actually occurs, not an error.",
    registry=_registry,
)
_dialogs_handled = Counter(
    "cobrowse_dialogs_handled_total",
    "Native JS dialogs (alert/confirm/prompt/beforeunload) auto-handled, by type "
    "and action (accept/dismiss).",
    ["type", "action"],
    registry=_registry,
)
_screencast_bytes = Counter(
    "cobrowse_screencast_bytes_total",
    "Base64 JPEG screencast bytes fanned out to viewers (per-viewer egress; the "
    "same frame sent to two viewers counts twice, which IS the wire cost). "
    "rate() = egress bytes/s; divide by cobrowse_screencast_frames_total for the "
    "average frame size. The budget signal for the picture knobs — raising "
    "_SCREENCAST_QUALITY or _MIN_FRAME_INTERVAL_S (fps) moves this directly.",
    registry=_registry,
)
_screencast_frames = Counter(
    "cobrowse_screencast_frames_total",
    "Screencast frames fanned out to viewers. rate() = the delivered fps after "
    "the pump's _MIN_FRAME_INTERVAL_S fan-out cap — the direct read-back of a "
    "frame-rate change (does the wire actually carry the fps we set?).",
    registry=_registry,
)
_frame_sinks = Gauge(
    "cobrowse_frame_sinks",
    "Screencast frame sinks currently registered on the driver (one per streaming "
    "viewer). Should track cobrowse_active_viewers; a positive value while "
    "active_viewers is 0 is a sink leak (the attach/teardown race).",
    registry=_registry,
)
_input_denied = Counter(
    "cobrowse_input_denied_total",
    "Drive frames (input/nav/tab/control/end) dropped from a WATCH-ONLY viewer — "
    "one whose chat grant is view, not edit/owner. A sustained rate can mean the UI "
    "is offering drive controls it shouldn't, or an authz probe.",
    registry=_registry,
)
_gc_deleted = Counter(
    "cobrowse_gc_profiles_deleted_total",
    "Orphaned per-chat Chromium profiles reclaimed by the profile GC (their chat "
    "was deleted). rate() = the reclaim throughput draining a tenant's backlog.",
    registry=_registry,
)
_gc_denied_perm = Counter(
    "cobrowse_gc_denied_perm_total",
    "Profiles the GC could NOT delete because they were foreign-owned (EPERM/EACCES). "
    "MUST stay 0 today (every pod is uid 65532). A sustained non-zero is the R2 "
    "tripwire: per-user Linux isolation has landed and the tenant-global sweep now "
    "leaks other users' orphans — the GC needs its root/drop-to-uid helper. Alert on "
    "any increase.",
    registry=_registry,
)
_gc_skipped_live = Counter(
    "cobrowse_gc_skipped_live_total",
    "Orphan profiles the GC skipped because a live Chrome held the profile lock — "
    "expected during an active co-browse, not an error.",
    registry=_registry,
)


def record_gc_sweep(*, deleted: int, denied_perm: int, skipped_live: int) -> None:
    """Record one profile-GC sweep's outcome (called once per sweep). Counters only
    move on non-zero so an idle sweep is free; all three render at 0 from boot so a
    dashboard/alert can bind before the first reclaim."""
    if deleted:
        _gc_deleted.inc(deleted)
    if denied_perm:
        _gc_denied_perm.inc(denied_perm)
    if skipped_live:
        _gc_skipped_live.inc(skipped_live)


def inc_dialog(dtype: str, action: str) -> None:
    _dialogs_handled.labels(type=dtype, action=action).inc()


def inc_input_denied() -> None:
    _input_denied.inc()


def inc_viewer_connection() -> None:
    _viewer_connections.inc()


def viewer_attached() -> None:
    _active_viewers.inc()


def viewer_detached() -> None:
    _active_viewers.dec()


def inc_session_error() -> None:
    _session_errors.inc()


def inc_send_drop() -> None:
    _send_drops.inc()


def inc_recv_close() -> None:
    _recv_closes.inc()


def inc_chromium_launch() -> None:
    _chromium_launches.inc()


def inc_chromium_launch_failure() -> None:
    _chromium_launch_failures.inc()


def inc_session_ended() -> None:
    _sessions_ended.inc()


def inc_screencast_teardown_mismatch() -> None:
    _screencast_teardown_mismatch.inc()


def inc_late_frame() -> None:
    _late_frames.inc()


def add_screencast_bytes(n: int) -> None:
    _screencast_bytes.inc(n)


def inc_screencast_frame() -> None:
    _screencast_frames.inc()


def set_frame_sinks(n: int) -> None:
    _frame_sinks.set(n)


def render() -> tuple[bytes, str]:
    """Return the text-format metrics body + content type for the HTTP handler."""
    return generate_latest(_registry), CONTENT_TYPE_LATEST

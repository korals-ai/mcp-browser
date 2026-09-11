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
  * ``cobrowse_restore_guard_trips_total`` / ``cobrowse_tab_restores_total``
    ``{outcome="degraded"}`` — a session start found its own saved tab set
    marked with an unfinished attempt, i.e. the last restore died mid-flight.
    A container-level OOM alert says a container died; this says the container's
    own persisted state is what keeps killing it, which is the difference
    between "restart it" and "clear the state" — a restart cannot fix a crash
    whose cause is reloaded from disk on every start.
  * ``cobrowse_viewers_without_frame_total`` — a human watched the spinner for a
    whole connection and never saw the page. The only signal here that measures
    what the VIEWER received rather than what the pod did; every transport-side
    metric read green through the late-joiner bug.
  * ``cobrowse_repaints_total{outcome="no_frame"}`` — the page changed and the
    viewers were not shown it. The sibling of the above for a viewer that IS
    painting: it has a picture, so nothing reads as broken, but the picture is
    of the previous page and the human has no way to tell.
  * ``cobrowse_input_dispatch_failures_total`` — the human's click raised on the
    way to the page. The action did not happen and the viewer was told nothing.

A note on shape, because three of the signals here exist because of it: the bugs
this pod keeps producing are ones where the pod is healthy and the HUMAN is
stuck, so a signal that measures the pod cannot see them. Prefer the metric that
counts what the viewer received, and when a case can't be measured, count it
under its own outcome (``viewer_left``, ``aborted_connect``) rather than letting
it fall out of the numerator — an unmeasured case that silently reads as a clean
one is how the last three of these went unnoticed until a customer said so.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
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
_pages_blocked = Counter(
    "cobrowse_pages_blocked_total",
    "Agent navigations that landed on a wall instead of content, by page_state "
    "(blocked_challenge/blocked_denied/rate_limited/server_error — see "
    "src/page_state.py). The measure of how often anti-bot walls cost tenants "
    "a browsing task; in-cluster diagnostic, not alert-wired.",
    ["state"],
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
_viewer_attaches = Counter(
    "cobrowse_viewer_attaches_total",
    "Viewers registering a frame sink, by what the pod could give them at that "
    "instant: first (this viewer started the capture), replayed (a cached frame "
    "was handed over immediately), no_cached_frame (a LATE viewer with nothing "
    "cached — it must wait for the next repaint, which on a static page may never "
    "come). Splitting the outcome is the point: a late attach that paints and one "
    "that hangs are otherwise the same event. Expect no_cached_frame only in the "
    "moments right after a tab switch or before the first frame of a cold session; "
    "a sustained rate means late joiners are staring at the spinner again.",
    ["outcome"],
    registry=_registry,
)
_viewers_without_frame = Counter(
    "cobrowse_viewers_without_frame_total",
    "Viewers that disconnected having received ZERO frames AFTER staying attached "
    "long enough to have been painted (see cobrowse_viewer_aborted_connects_total "
    "for the ones that didn't) — the human saw the loading spinner and nothing "
    "else. This counts the USER's outcome, not the transport's health: every other "
    "co-browse signal (viewer_connections, frame_sinks, session_errors) stayed "
    "perfectly green through the 2026-09-10 late-joiner bug because the socket, "
    "the sink and the screencast were all fine — only the picture was missing. "
    "Alert on any sustained rate.",
    registry=_registry,
)
_viewer_aborted_connects = Counter(
    "cobrowse_viewer_aborted_connects_total",
    "Viewer connections that ended with zero frames in under "
    "_MIN_PAINTABLE_CONNECTION_S — too fast for any picture to have been possible, "
    "so NOT a stranded human. Split out of cobrowse_viewers_without_frame_total "
    "because lumping them together made an ordinary reconnect race read as a "
    "person watching a spinner, which is what misfired CoBrowseViewerNeverPainted "
    "on 2026-09-11. It is counted rather than dropped for the usual reason: a "
    "silent skip and a genuine zero look identical. A sustained rate is its own "
    "finding — the SPA is dialling and hanging up, so every reconnect flashes the "
    "spinner even though each individual attempt looks harmless.",
    registry=_registry,
)
_viewer_connection_seconds = Histogram(
    "cobrowse_viewer_connection_seconds",
    "How long a viewer's WebSocket lived. The distribution IS the churn signal: "
    "a healthy session is one long connection, and a pile in the sub-second bucket "
    "means viewers are flapping (each flap is a spinner flash for the human) while "
    "a hard cluster at one long duration means something upstream — a proxy idle "
    "timeout, a lease — is cutting sessions at a fixed age rather than the human "
    "leaving. Neither is visible in a connection COUNT, which is all "
    "cobrowse_viewer_connections_total can say.",
    buckets=(0.5, 5.0, 60.0, 600.0, 3600.0, float("inf")),
    registry=_registry,
)
_repaints = Counter(
    "cobrowse_repaints_total",
    "Page changes the pod KNOWS happened (navigate / tab switch / back / forward / "
    "reload) while at least one viewer was attached, by what the viewers then got: "
    "painted (a frame reached a viewer), no_frame (none did, within the deadline — "
    "every attached human is looking at the PREVIOUS page and has no way to know), "
    "viewer_left (the last viewer detached before the deadline, so the question "
    "became moot). This is the join no single-sided signal can make: the screencast "
    "can be running, the sinks registered and the navigation successful while the "
    "picture is silently stale. viewer_left is carried rather than dropped so an "
    "unmeasured case can never be mistaken for a clean one.",
    ["outcome"],
    registry=_registry,
)
_repaint_to_frame_seconds = Histogram(
    "cobrowse_repaint_to_frame_seconds",
    "Seconds from a known page change to the first frame that reached a viewer. "
    "The human-facing latency of 'I clicked a link / the agent navigated' — the "
    "one number that says how long the co-browse picture lags reality.",
    buckets=(0.5, 2.0, 5.0, float("inf")),
    registry=_registry,
)
_input_dispatch_failures = Counter(
    "cobrowse_input_dispatch_failures_total",
    "Drive frames from a viewer that RAISED on dispatch, by kind (input/navigate/"
    "tab/control/resize). The human clicked and the click went nowhere. Before "
    "this existed the exception tore the whole connection down, so one bad click "
    "took the picture with it and surfaced only as a generic "
    "cobrowse_session_errors_total tick; the connection now survives and the "
    "failure is named. Any sustained rate is a broken input path, not a flaky "
    "viewer.",
    ["kind"],
    registry=_registry,
)
_multi_driver_attaches = Counter(
    "cobrowse_multi_driver_attaches_total",
    "Times a viewer with DRIVE rights attached to a session that already had one. "
    "Two people sharing one mouse and keyboard with no arbitration and no UI "
    "telling either of them: clicks interleave, typing lands in whichever field "
    "the other person just moved focus to. Not an error — co-browse is deliberately "
    "multi-viewer — but it is the precondition for a confusing session, and today "
    "nothing anywhere records that it happened.",
    registry=_registry,
)
_first_frame_seconds = Histogram(
    "cobrowse_viewer_first_frame_seconds",
    "Seconds from a viewer attaching to the first frame IT received, split by "
    "whether it started the capture (join=first, pays Chromium launch + first "
    "paint) or joined one already running (join=late, should be immediate from "
    "the cache). A late-join distribution that drifts off the bottom bucket is "
    "the regression. Note the blind spot this shares with every latency metric: "
    "a viewer that NEVER paints is absent here, not slow — cobrowse_viewers_"
    "without_frame_total is its counterpart and must be read together.",
    ["join"],
    buckets=(0.25, 2.0, 10.0, float("inf")),
    registry=_registry,
)
_replay_frame_age_seconds = Histogram(
    "cobrowse_replay_frame_age_seconds",
    "Age of the cached frame replayed to a late viewer, in seconds. A large age "
    "is not itself wrong — a page nobody touched for an hour is genuinely still "
    "that picture — but it is the tell for a cache that outlived what it depicts "
    "(the driver clears it on tab switch and on capture stop, so a replay much "
    "older than the last navigation means one of those paths stopped clearing).",
    buckets=(1.0, 10.0, 60.0, float("inf")),
    registry=_registry,
)
_frame_send_failures = Counter(
    "cobrowse_frame_send_failures_total",
    "Fan-out sends that raised — one viewer's socket failed while the pump was "
    "writing to it. The pump deliberately swallows these so one dying viewer "
    "cannot stall the others, and swallowing without counting is how a silently "
    "starved viewer stays invisible. Sits alongside cobrowse_send_drops_total "
    "(a drop the WS layer saw coming); a rate here without disconnects means "
    "sends are failing on sockets we still believe are open.",
    registry=_registry,
)
_slow_frame_sends = Counter(
    "cobrowse_slow_frame_sends_total",
    "Single-sink sends that took longer than the pump's own frame interval "
    "(_MIN_FRAME_INTERVAL_S). The pump awaits sinks SERIALLY, so one slow viewer "
    "delays the picture for every other viewer on the session — this is the "
    "head-of-line signal that says the fan-out needs per-viewer pacing rather "
    "than a shared loop. Zero on a healthy pod.",
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
_tab_restores = Counter(
    "cobrowse_tab_restores_total",
    "Session starts that consulted the saved tab set, by outcome: nothing_saved "
    "(no file), clean (restored, active tab loaded), degraded (the restore guard "
    "saw an unfinished previous attempt, so NOTHING was loaded), exhausted (the "
    "guard kept firing with nothing loaded — the tabs are not the cause). Any "
    "degraded/exhausted is the crash-loop signature: alert on it, because the "
    "pod-level OOM alert only says a container died, not that its own saved "
    "state is what keeps killing it.",
    ["outcome"],
    registry=_registry,
)
_tabs_restored = Counter(
    "cobrowse_tabs_restored_total",
    "Tabs recreated by a restore (loaded or parked). Divided by "
    "cobrowse_tab_restores_total this is the average saved-tab-set size — the "
    "number that has to stay compatible with the pod's memory limit.",
    registry=_registry,
)
_tabs_loaded_on_restore = Counter(
    "cobrowse_tabs_loaded_on_restore_total",
    "Tabs a restore actually NAVIGATED. Restore is lazy, so this should be 1 per "
    "clean restore and 0 per degraded one. A value tracking "
    "cobrowse_tabs_restored_total means the lazy path regressed to eager and the "
    "2026-09-09 OOM loop is back.",
    registry=_registry,
)
_tabs_dropped_on_restore = Counter(
    "cobrowse_tabs_dropped_on_restore_total",
    "Saved tabs discarded for exceeding the per-restore cap — real data loss for "
    "the human, so it is counted rather than only logged.",
    registry=_registry,
)
_restore_guard_trips = Counter(
    "cobrowse_restore_guard_trips_total",
    "Restores that found an unfinished attempt marked on the saved set and so "
    "loaded nothing. Each increment is one crash the guard absorbed; a sustained "
    "rate means something is still killing session start.",
    registry=_registry,
)
_tabs_hydrated = Counter(
    "cobrowse_tabs_hydrated_total",
    "Parked (restored, unloaded) tabs navigated on first activation. EXCLUDES the "
    "restore's own load of the active tab, which is not a revisit — counting it "
    "there would put a 1 in this on every clean start. rate() = how often a human "
    "or the agent actually comes back to a restored tab, which is the measured "
    "justification for restoring them lazily.",
    registry=_registry,
)
_open_tabs = Gauge(
    "cobrowse_open_tabs",
    "Tabs currently open across this pod's co-browse sessions. The leading "
    "indicator for the per-renderer memory the pod's limit is sized against — "
    "on 2026-09-09 this reached 10 in one session against a limit sized for a "
    "couple, and nothing was watching it.",
    registry=_registry,
)
_memory_used = Gauge(
    "cobrowse_memory_used_bytes",
    "This container's cgroup memory usage, sampled at session start, restore and "
    "tab hydration. cAdvisor already scrapes this, but at 30s resolution it "
    "missed a container that lived 50 seconds; this one is sampled at the moments "
    "that allocate.",
    registry=_registry,
)
_memory_limit = Gauge(
    "cobrowse_memory_limit_bytes",
    "This container's cgroup memory limit (0 = unlimited). Paired with "
    "cobrowse_memory_used_bytes so a dashboard can show headroom without needing "
    "the pod spec — and so an alert can fire on the RATIO before the kernel acts.",
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


def record_tab_restore(*, outcome: str, tabs: int, loaded: int, dropped: int) -> None:
    """Record one restore. Called on EVERY session start that consults the saved
    set, including the empty case — an outcome series that only appears once
    something restored could not tell "no restores happened" from "the metric
    was never wired"."""
    _tab_restores.labels(outcome=outcome).inc()
    if tabs:
        _tabs_restored.inc(tabs)
    if loaded:
        _tabs_loaded_on_restore.inc(loaded)
    if dropped:
        _tabs_dropped_on_restore.inc(dropped)


def inc_restore_guard_trip() -> None:
    _restore_guard_trips.inc()


def inc_tab_hydrated() -> None:
    _tabs_hydrated.inc()


def add_open_tabs(delta: int) -> None:
    """Move the pod-wide open-tab gauge. A delta, not a set: several per-chat
    sessions share one pod, and each can only speak for its own tabs."""
    _open_tabs.inc(delta)


def set_memory(*, used: int, limit: int) -> None:
    _memory_used.set(used)
    _memory_limit.set(limit)


def inc_page_blocked(state: str) -> None:
    _pages_blocked.labels(state=state).inc()


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


def inc_viewer_attach(outcome: str) -> None:
    """outcome: first | replayed | no_cached_frame (see the metric docstring)."""
    _viewer_attaches.labels(outcome=outcome).inc()


def inc_viewer_without_frame() -> None:
    _viewers_without_frame.inc()


def inc_viewer_aborted_connect() -> None:
    _viewer_aborted_connects.inc()


def observe_viewer_connection(seconds: float) -> None:
    _viewer_connection_seconds.observe(seconds)


def inc_repaint(outcome: str) -> None:
    """outcome: painted | no_frame | viewer_left (see the metric docstring)."""
    _repaints.labels(outcome=outcome).inc()


def observe_repaint_to_frame(seconds: float) -> None:
    _repaint_to_frame_seconds.observe(seconds)


def inc_input_dispatch_failure(kind: str) -> None:
    _input_dispatch_failures.labels(kind=kind).inc()


def inc_multi_driver_attach() -> None:
    _multi_driver_attaches.inc()


def observe_first_frame(join: str, seconds: float) -> None:
    """join: first | late — a late join should land in the bottom bucket."""
    _first_frame_seconds.labels(join=join).observe(seconds)


def observe_replay_frame_age(seconds: float) -> None:
    _replay_frame_age_seconds.observe(seconds)


def inc_frame_send_failure() -> None:
    _frame_send_failures.inc()


def inc_slow_frame_send() -> None:
    _slow_frame_sends.inc()


def set_frame_sinks(n: int) -> None:
    _frame_sinks.set(n)


def render() -> tuple[bytes, str]:
    """Return the text-format metrics body + content type for the HTTP handler."""
    return generate_latest(_registry), CONTENT_TYPE_LATEST

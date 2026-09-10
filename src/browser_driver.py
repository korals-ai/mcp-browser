"""The browser control abstraction: one driver per shared Chromium session.

:class:`BrowserDriver` is the seam between the pod's logic (sessions, MCP tools,
co-browse WS) and the real browser. The logic depends only on this Protocol, so
tests substitute a fake and never launch Chromium — matching the repo's
"inject IO deps, keep logic testable" stance.

:class:`PlaywrightDriver` is the real implementation: it owns a Chromium context
with N **tabs** (pages), a CDP session per tab, and screencasts the ACTIVE tab.
Snapshot/click/type act on the active tab; switching a tab moves the screencast
to the newly-active tab's CDP session. Imported lazily so the test image doesn't
need Playwright's Chromium on the pre-build path.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import shutil
import subprocess
from collections import deque
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn, Protocol

from src import metrics
from src.input_map import to_cdp_command
from src.page_state import classify_page_state
from src.snapshot import Element

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = logging.getLogger("workspace-tool-browser")

# Ring-buffer + truncation caps for the console/network observability tools:
# bound memory so a chatty page can't grow the per-tab buffers without limit.
_CONSOLE_RING = 50
_NETWORK_RING = 100
_CONSOLE_TEXT_CAP = 2000
_URL_CAP = 512

# Screencast picture tuning — the three knobs balance each other, set TOGETHER;
# don't tune one in isolation:
# - _DEVICE_SCALE 2: JPEGs carry 2x the CSS viewport's pixels, which is what
#   keeps text crisp on a HiDPI display (1x is blurry at any JPEG quality).
#   CDP frame *metadata* stays in CSS px; the viewer sizes its canvas from the
#   JPEG's own pixels (apps/chat-ui/src/lib/cobrowsePaint.ts).
# - _SCREENCAST_QUALITY 85: below ~60, JPEG ringing on text is visible even at
#   native size.
# - _MIN_FRAME_INTERVAL_S: CDP emits up to ~60 fps mid-animation; capping
#   fan-out at ~15 fps stays visibly smooth (10 reads as steppy). The pump only
#   fans out the FRESHEST frame, so the cap bounds egress at ~15 x per-frame
#   bytes per viewer — the CoBrowseEgressHigh alert
#   (infra/grafana_cloud/cobrowse_alerts.tf) holds the per-pod ceiling.
_DEVICE_SCALE = 2
_SCREENCAST_QUALITY = 85
_MIN_FRAME_INTERVAL_S = 0.066

# Human-driven resize bounds (set_viewport). The floor keeps a sliver panel
# from rendering an unusable page; the ceiling caps the screencast JPEG
# (_DEVICE_SCALE x these pixels) so a 4K panel can't balloon per-frame egress.
_MIN_VIEWPORT_W, _MIN_VIEWPORT_H = 400, 300
_MAX_VIEWPORT_W, _MAX_VIEWPORT_H = 2560, 1600

# Lower the automation fingerprint: co-browse drives REAL user portals on the
# human's behalf, and a browser advertising automation gets CAPTCHA-walled.
# Verified: this flag flips `navigator.webdriver` to false; stripping the
# "Headless" UA token (`_clean_ua`) drops the other dominant signal. Residual
# signals remain — full stealth is an endless arms race, out of scope.
_STEALTH_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]

# Playwright INJECTS --enable-automation into Chromium's default switches — the
# flag behind the "controlled by automated test software" infobar and
# webdriver-adjacent behavior on some Chrome versions. A launch arg cannot
# retract a default switch; only ignore_default_args removes it, so it must be
# passed on BOTH launch paths (ephemeral and persistent).
_IGNORED_DEFAULT_ARGS = ["--enable-automation"]

# Chrome's setuid/namespace sandbox can't initialize in the unprivileged
# per-tenant pod (uid 65532, no CAP_SYS_ADMIN); the pod itself (per-tenant,
# non-root, egress-fenced) is the isolation boundary, so --no-sandbox is safe.
# K8s /dev/shm is 64Mi (too small for Chrome) → /tmp; no GPU → software render.
_CONTAINER_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]


def _launch_args() -> list[str]:
    return [*_STEALTH_LAUNCH_ARGS, *_CONTAINER_LAUNCH_ARGS]


def _env_headless() -> bool:
    """Prod runs HEADED (real window under Xvfb) for the lowest automation
    fingerprint. REQUIRED (Tier 0.5): ``BROWSER_HEADLESS=false`` is declared in
    the image, and the gate/CI declares ``true`` — an unset value must not decide
    between "headed under an X server we may not have" and "headless" silently."""
    return os.environ["BROWSER_HEADLESS"].strip().lower() not in ("false", "0", "no")


def _env_executable_path() -> str | None:
    """The pinned Chrome for Testing binary. REQUIRED, with an EXPLICIT empty
    value as the declared sentinel for "use Playwright's bundled Chromium" (what
    tests/CI set) — the image declares the real CfT path."""
    return os.environ["BROWSER_EXECUTABLE_PATH"] or None


def _clean_ua(ua: str) -> str | None:
    """Strip the ``Headless`` UA marker; None if no change needed. Derived from
    the browser's OWN reported UA so the Chrome version can't drift from a
    hardcoded constant."""
    if "Headless" not in ua:
        return None
    return ua.replace("HeadlessChrome", "Chrome").replace("Headless", "")


def _type_note(typed: str, observed: dict[str, Any] | None) -> str:
    """Build ``type_text``'s result from the post-type read-back. Pure, so the
    phrasing rules are unit-testable without a browser.

    Two situations earn a note: the field's live value differs from what was
    typed (the page reformatted/restricted/autocompleted the input), and the
    field is an autocomplete widget (suggestions may have appeared — clicking
    one beats pressing Enter). A secret field's value is NEVER echoed — the
    mismatch is reported without the bytes, same stance as the snapshot
    redaction in src/snapshot.py."""
    if not observed:
        return "ok"
    notes: list[str] = []
    actual = observed.get("value")
    if actual is not None and actual != typed:
        if str(observed.get("type", "")).lower() == "password":
            notes.append(
                "note: the field's value differs from the text you typed "
                "(value hidden — secret field)"
            )
        else:
            notes.append(
                f"note: the field now contains {actual!r}, not the text you typed — "
                "the page reformatted or restricted your input"
            )
    role = str(observed.get("role", "")).lower()
    autocomplete = str(observed.get("autocomplete", "")).lower()
    if role == "combobox" or (autocomplete and autocomplete != "none"):
        notes.append(
            "this is an autocomplete field — suggestions may have appeared; "
            "call browser_snapshot and click the right suggestion instead of "
            "pressing Enter"
        )
    if not notes:
        return "ok"
    return "ok — " + "; ".join(notes)


def _raise_ref_action_error(action: str, ref: str, exc: Exception) -> NoReturn:
    """Rewrite a Playwright selector timeout on a ref action into an error that
    tells the agent its corrective call, instead of a selector-soup timeout the
    agent can only guess at. Only TimeoutError is rewritten — any other failure
    re-raises untouched. Both causes of a ref timeout are named (stale ref vs
    blocked element) because they have opposite fixes and the timeout alone
    cannot distinguish them."""
    if type(exc).__name__ != "TimeoutError":
        raise exc
    raise ValueError(
        f"could not {action} ref '{ref}': either the ref is stale (refs change "
        "after any navigation or page update — call browser_snapshot for fresh "
        "refs), or the element is covered/not interactable right now"
    ) from exc


def _truncate_url(url: str) -> str:
    """Cap a URL for the network log; inline ``data:``/``blob:`` URIs collapse
    to their scheme tag so a payload never lands in the ring."""
    u = url or ""
    if u.startswith("data:") or u.startswith("blob:"):
        return u.split(",", 1)[0][:40]  # e.g. "data:image/png;base64" — not the payload
    return u if len(u) <= _URL_CAP else u[:_URL_CAP] + "…"


# Open-tab set recorded inside the (EFS-durable) profile dir so a restart
# reopens the same tabs. Chrome ignores unknown files in its user-data-dir.
_OPEN_TABS_FILE = ".cobrowse_open_tabs.json"
# Bound a restore: cap the tab count, and cap each tab's load so one hung URL
# can't stall the whole session start.
_MAX_RESTORE_TABS = 20
_RESTORE_GOTO_TIMEOUT_MS = 15000

# A restore RECREATES every saved tab but LOADS only the active one; the rest
# hold their URL in _Tab.pending_url and navigate on first activation. The tab
# list is a few hundred bytes; the loaded pages are ~150-250 MB of renderer
# each, and the pod's limit is sized for a couple of them. Restoring ten at
# once — which this did, concurrently — reached 99% of the memory limit and was
# OOM-killed, and because the tab set is reloaded from disk at every start, each
# restart replayed it: measured at ten real commercial pages, eager restore
# peaks at 99% of the limit and lazy restore at 35%. Chrome and Firefox both
# restore sessions this way for the same reason.
#
# The guard on top of that: an OOM is a SIGKILL, so the process cannot report
# its own death. Instead the attempt is RECORDED IN THE FILE before any
# navigation and cleared once the session is up, so a mark that is still set on
# the next boot means "the last restore never finished" — the only evidence a
# killed process can leave. One unfinished attempt drops even the active tab to
# lazy, which is the floor: a restore that navigates nothing cannot be what is
# killing us, and past that the tabs are exonerated, so the URLs are KEPT and
# the operator gets a loud log rather than silent data loss.
_RESTORE_GUARD_EXHAUSTED_ATTEMPTS = 3

# Chrome's user-data-dir runs LIVE on the tenant volume, so every cookie/login/
# IndexedDB write is durable the moment Chrome makes it. The two costs are
# handled at their owners: Chrome's dangling Singleton* lock symlinks are
# cleared by US at every launch + container boot under a per-profile flock, and
# the S3 sync layer is strict complete-or-nothing (apps/base-images/s3-sync).
# Design: docs/plan/20260731T123000Z-cobrowse-profile-durability.md;
# incident: docs/incidents/2026-07-30-cobrowse-singleton-symlink-backup-erosion.md.
#
# One release earlier the durable store was a tar.gz snapshot of an ephemeral
# runtime dir; first launch reverse-migrates it (extract, delete the archive).
_DURABLE_ARCHIVE = "profile.tar.gz"
# tar tmp prefix the archive-era checkpointer staged (swept if a crash left one).
_CKPT_TMP_PREFIX = ".profile.tar.gz.tmp-"
# Cross-pod mutual exclusion: an fcntl flock held for the whole session
# (launch → close). Chrome's own SingletonLock can't serve across pods — it
# encodes hostname+pid, so a dead pod's lock just makes the next Chrome refuse
# with 'profile in use'. EFS NFSv4 flocks, same pattern as the workspace
# image's per-chat locks. Anyone clearing Singleton* — or garbage-collecting a
# profile (:mod:`src.profile_gc`) — MUST hold this.
#
# The lock lives OUTSIDE the profile dir (sibling ``.cobrowse/locks/<id>.lock``,
# NEVER deleted) so the GC can rename-then-rm the profile without unlinking the
# exclusion inode mid-delete: a lock inside the dir lets a concurrent launch
# O_CREAT a fresh inode and flock it while the dir is half-gone → SQLite/cookie
# corruption (a proven race). See B.1 in
# docs/plan/20260810T182738Z-cobrowse-profile-footprint.md.
_LOCKS_SUBDIR = "locks"


def profile_lock_path(profile_dir: str) -> str:
    """Stable external flock path: ``.cobrowse/profile/<id>`` →
    ``.cobrowse/locks/<id>.lock`` (see the lock-placement block above)."""
    profile_dir = profile_dir.rstrip("/")
    chat_id = os.path.basename(profile_dir)
    cobrowse_root = os.path.dirname(os.path.dirname(profile_dir))
    return os.path.join(cobrowse_root, _LOCKS_SUBDIR, f"{chat_id}.lock")


def _clear_singleton_locks(profile_dir: str) -> None:
    """Remove Chromium's ``Singleton{Lock,Cookie,Socket}``. Only an in-Chrome
    quit unlinks them (SIGTERM/SIGKILL do not — verified), and the next pod has
    a different hostname, so Chromium reads a stale lock as 'profile in use by
    another computer' and refuses to launch. Only safe under the profile flock
    (caller's responsibility) — never delete a LIVE Chrome's locks."""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        with contextlib.suppress(OSError):
            os.remove(os.path.join(profile_dir, name))


# Regenerable caches Chrome recreates on demand. `Cache`/`Code Cache` are also
# redirected off the PVC (--disk-cache-dir), so an on-PVC copy is stale dead
# weight; the GPU/shader/crx/Service-Worker tail is un-relocatable but equally
# regenerable. Deleting at launch keeps the on-PVC profile to durable state
# (cookies/logins/IndexedDB) and stops cache syncing to S3 — the
# WorkspaceTenantBloat cause. Paths relative to the profile dir (root +
# `Default`). See docs/plan/20260810T182738Z-cobrowse-profile-footprint.md.
_REGENERABLE_CACHE_SUBDIRS = (
    "Cache",
    "Code Cache",
    "GPUCache",
    "GPUPersistentCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "GrShaderCache",
    "ShaderCache",
    "component_crx_cache",
    "extensions_crx_cache",
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/DawnGraphiteCache",
    "Default/DawnWebGPUCache",
    "Default/Service Worker/CacheStorage",
    "Default/Service Worker/ScriptCache",
    "Default/Shared Dictionary/cache",
)


def _write_open_tabs(profile_dir: str, urls: list[str], active: int, attempts: int = 0) -> None:
    """Write (or remove) the saved-tabs file. Pure filesystem — it is handed
    plain data, never a Playwright object, so it is safe to run on a thread.

    Atomic (tmp + ``os.replace``) so a crash can't leave a torn file; an
    all-blank tab set removes the file so a stale set doesn't linger.

    ``attempts`` is the restore dirty bit: non-zero means "a restore of this
    exact set started and has not reported success". Normal persistence writes
    0, so the mark can only be set by :meth:`_mark_restore_attempt`.
    """
    path = os.path.join(profile_dir, _OPEN_TABS_FILE)
    if not any(u and u != "about:blank" for u in urls):
        with contextlib.suppress(OSError):
            os.remove(path)
        return
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump({"tabs": urls, "active": active, "restore_attempts": attempts}, f)
    os.replace(tmp, path)


def _prepare_persistent_dir(profile_dir: str) -> None:
    """The two blocking steps that must happen immediately before Chromium
    takes the profile: ensure the dir, clear stale Singleton locks."""
    os.makedirs(profile_dir, exist_ok=True)
    _clear_singleton_locks(profile_dir)


def purge_regenerable_cache(profile_dir: str) -> int:
    """Delete regenerable cache dirs from a profile (best-effort); returns the
    count removed. MUST run under the profile flock with no live Chrome on the
    dir. Only cache names are matched — never Cookies/Local Storage/IndexedDB."""
    removed = 0
    for rel in _REGENERABLE_CACHE_SUBDIRS:
        target = os.path.join(profile_dir, rel)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
            if not os.path.isdir(target):
                removed += 1
    return removed


def clear_stale_profile_locks(base_dir: str) -> int:
    """Container-boot hygiene: remove stale ``Singleton*`` left by hard-killed
    pods so the tenant-volume S3 sync runs clean. Each profile is tried under a
    NON-blocking flock — a held lock means another pod's live Chrome owns it,
    and we must not touch its locks. Returns the profile count swept.
    Best-effort: a boot sweep must never block serving."""
    swept = 0
    try:
        entries = os.listdir(base_dir)
    except OSError:
        return 0
    for name in entries:
        profile = os.path.join(base_dir, name)
        if not os.path.isdir(profile):
            continue
        lock_path = profile_lock_path(profile)
        try:
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)  # lock held → a live session owns this profile; skip
            continue
        try:
            _clear_singleton_locks(profile)
            swept += 1
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    return swept


class BrowserDriver(Protocol):
    """Everything the pod needs from ONE browser session (with N tabs). Async
    throughout — every method touches the browser over CDP."""

    async def open(self, url: str, *, new_tab: bool = False) -> str:
        """Navigate (optionally in a new tab) and return the landed page's
        ``page_state`` (see :mod:`src.page_state`; ``"unknown"`` when the
        navigation succeeded but classification itself failed)."""
        ...

    async def list_tabs(self) -> list[dict[str, Any]]:
        """The open tabs as ``[{id, title, url, active}]`` in tab order."""
        ...

    async def switch_tab(self, tab_id: str) -> bool:
        """Make ``tab_id`` active (screencast + actions follow it). False if
        unknown."""
        ...

    async def close_tab(self, tab_id: str) -> bool:
        """Close a tab; if it was active, activate another. Never leaves zero
        tabs (opens a blank one). False if unknown."""
        ...

    async def snapshot(self) -> list[Element]:
        """Return the active tab's raw interactable-element list (UN-redacted;
        the caller redacts — see :mod:`src.snapshot`)."""
        ...

    async def click(self, ref: str) -> None: ...

    async def type_text(self, ref: str, text: str) -> str:
        """Type into the ref'd field. Returns ``"ok"``, or ``"ok — note: …"``
        when the field's post-type value differs from what was typed or the
        field is an autocomplete (see :func:`_type_note`)."""
        ...

    async def scroll(self, direction: str, amount: int) -> None: ...

    # --- reading / understanding (view-only) --------------------------------

    async def read(self) -> str:
        """The active tab's readable text (main content, capped)."""
        ...

    async def find_text(self, query: str) -> dict[str, Any]:
        """Find ``query`` on the page; scroll the first hit into view. Returns
        ``{count, snippet}``."""
        ...

    async def screenshot(self) -> bytes: ...

    async def inspect(self, ref: str) -> dict[str, Any]:
        """Details of one element (tag, attributes, text, box, visible/enabled)."""
        ...

    async def get_table(self, ref: str | None = None) -> list[list[list[str]]]:
        """Extract HTML tables as ``[table][row][cell]``. ``ref`` = one table;
        None = all tables on the page."""
        ...

    # --- history / reliability (view-only) ----------------------------------

    async def go_back(self) -> None: ...

    async def go_forward(self) -> None: ...

    async def reload(self) -> None: ...

    async def wait_for(self, *, text: str | None, selector: str | None, timeout_ms: int) -> bool:
        """Wait until ``text`` appears (or CSS ``selector`` matches). False on
        timeout."""
        ...

    # --- extra actions (mutating — approved in chat) ------------------------

    async def press_key(self, key: str) -> None: ...

    async def select_option(self, ref: str, value: str) -> None: ...

    async def upload_file(self, ref: str, path: str) -> None: ...

    async def download(self, ref: str, dest_path: str) -> dict[str, Any]:
        """Click ``ref`` to trigger a download and save it to ``dest_path`` on the
        tenant volume. Returns ``{filename, saved}``."""
        ...

    # --- frames (active-frame model, like active-tab) -----------------------

    async def list_frames(self) -> list[dict[str, Any]]:
        """The active tab's frames as ``[{index, name, url}]`` (index 0 = main)."""
        ...

    async def switch_frame(self, target: str) -> bool:
        """Set the active frame by index or name; empty/"main"/"0" resets to main.
        False if no frame matches."""
        ...

    # --- native dialogs -----------------------------------------------------

    async def set_dialog_mode(self, mode: str) -> None:
        """Set session-wide auto-handling of JS dialogs: ``accept`` or ``dismiss``."""
        ...

    async def last_dialog(self) -> dict[str, Any] | None: ...

    # --- console / network observability ------------------------------------

    async def console_log(self) -> list[dict[str, Any]]:
        """Recent console messages of the active tab as ``[{type, text}]``."""
        ...

    async def network_log(
        self, *, url_substring: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Recent network entries as ``[{method, url, status, resource_type}]``."""
        ...

    async def wait_for_response(self, url_substring: str, timeout_ms: int) -> dict[str, Any]:
        """Wait for a response whose URL contains ``url_substring``. ``{matched, status, url}``."""
        ...

    # --- interaction extras -------------------------------------------------

    async def hover(self, ref: str) -> None: ...

    async def drag(self, from_ref: str, to_ref: str) -> None: ...

    async def scroll_to(self, ref: str) -> bool:
        """Scroll ``ref`` into view. False if the ref isn't on the page."""
        ...

    async def get_options(self, ref: str) -> list[dict[str, Any]]:
        """A native ``<select>``'s options as ``[{value, label, selected}]``."""
        ...

    async def get_links(self, cap: int = 200) -> list[dict[str, str]]:
        """All anchors as ``[{text, href}]`` (absolute), capped at ``cap``."""
        ...

    async def eval_js(self, js: str) -> dict[str, Any]:
        """Run ``js`` via evaluate; ``{result}`` or ``{error}`` — never raises."""
        ...

    async def fill_login(self, username: str, password: str) -> bool:
        """Best-effort: find the active tab's username + password fields, fill
        them (server-side — the secret never touches the agent), and submit."""
        ...

    async def nav_state(self) -> dict[str, Any]:
        """Active tab's ``{url, title, can_go_back, can_go_forward}``."""
        ...

    async def add_frame_sink(self, sink: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
        """Register a viewer's frame sink. The CDP screencast starts on the FIRST
        sink and runs once for ALL viewers — each frame fans out to every sink —
        so N concurrent viewers all see the live browser (not just the newest)."""
        ...

    async def ack_frame(self, frame_id: int) -> None:
        """Ack a screencast frame (no-op: frames are acked server-side)."""
        ...

    async def remove_frame_sink(
        self, sink: Callable[[str, dict[str, Any]], Awaitable[None]]
    ) -> None:
        """Drop a viewer's frame sink. When the LAST sink goes, the CDP screencast
        stops (nothing left to stream to)."""
        ...

    async def send_input(self, event: str, fields: dict[str, Any]) -> None:
        """Replay a human input event onto the active tab (mouse/key/wheel/paste)."""
        ...

    async def cursor_at(self, x: float, y: float) -> str:
        """Best-effort: the raw computed CSS ``cursor`` at remote CSS px (x, y),
        or ``""`` when it can't be read (cross-origin frame, navigation, no
        element). Cosmetic only — callers bound it with a timeout and never let
        its failure touch the screencast or input path."""
        ...

    async def set_viewport(self, width: int, height: int) -> None:
        """Resize the shared session's viewport to ``width`` x ``height`` CSS px so the
        human viewer's panel fills without letterboxing. Applies to every tab; the
        device scale (screencast crispness) is preserved."""
        ...

    async def close(self) -> None: ...


def _read_cgroup_memory() -> tuple[int, int] | None:
    """This container's ``(used, limit)`` memory in bytes, or None where the
    cgroup files aren't readable (macOS dev, CI). Pure filesystem — run it on a
    thread.

    Chromium's cost is per RENDERER, so the number that matters at restore time
    is how much room is left before the cgroup limit, not how many tabs there
    are. Without it an OOM postmortem has only cAdvisor's 30s-resolution
    scrape, which on 2026-09-09 sampled a 50-second container life roughly
    once.
    """
    try:
        with open("/sys/fs/cgroup/memory.current") as f:  # cgroup v2
            used = int(f.read().strip())
        with open("/sys/fs/cgroup/memory.max") as f:
            raw = f.read().strip()
        limit = 0 if raw == "max" else int(raw)  # "max" = unlimited, no ratio to report
    except (OSError, ValueError):
        return None
    return used, limit


async def _memory_snapshot() -> str:
    """A short ``used/limit`` string for the restore logs, or ``unknown``. Never
    raises — an instrument that can break the path it instruments is worse than
    no instrument."""
    got = await asyncio.to_thread(_read_cgroup_memory)
    if got is None:
        return "unknown"
    used, limit = got
    metrics.set_memory(used=used, limit=limit)
    if not limit:
        return f"{used // (1 << 20)}Mi/unlimited"
    return f"{used // (1 << 20)}Mi/{limit // (1 << 20)}Mi ({100 * used // limit}%)"


def _tab_url(tab: _Tab) -> str:
    """A tab's URL for reporting and persistence: where it is, or — while it is
    a not-yet-loaded restored tab — where it is going."""
    return tab.pending_url or str(tab.page.url)


class _SavedTabs(NamedTuple):
    """The restore record on disk. ``attempts`` is the dirty bit — see
    ``_RESTORE_GUARD_EXHAUSTED_ATTEMPTS``."""

    urls: list[str]
    active: int
    attempts: int


class _Tab:
    """One Chromium page + its CDP session, tracked by a stable id."""

    def __init__(self, tab_id: str, page: Any, cdp: Any) -> None:
        self.id = tab_id
        self.page = page
        self.cdp = cdp
        # The Page.screencastFrame listener bound to THIS tab's CDP session while
        # streaming (None otherwise). Stored so removal targets the exact callable
        # registered — pyee removes by identity, and a per-tab closure acks a late
        # frame to the session it came from.
        self._screencast_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        # Which frame element ops act on (None = main). switch_frame pins a
        # child here; reset on a tab switch.
        self.active_frame_key: str | None = None
        # Set on a RESTORED tab that has not loaded yet: the page is real and
        # blank, this is where it will go on first activation. While it is set,
        # this — not page.url — is the tab's URL everywhere the tab is reported
        # or persisted, or a lazy tab would persist as about:blank and erase the
        # very URL it is holding.
        self.pending_url: str | None = None
        # Bounded per-tab console/network rings, fed by _install_observers.
        self.console_ring: deque[dict[str, Any]] = deque(maxlen=_CONSOLE_RING)
        self.network_ring: deque[dict[str, Any]] = deque(maxlen=_NETWORK_RING)
        self._observers_installed = False


class PlaywrightDriver:
    """Real driver over Playwright: a Chromium context with N tabs, screencasting
    the active one. Constructed but NOT started until :meth:`start`."""

    def __init__(
        self,
        *,
        viewport: tuple[int, int] = (1280, 800),
        profile_dir: str | None = None,
        cache_dir: str | None = None,
        disk_cache_size: str | None = None,
        headless: bool | None = None,
        executable_path: str | None = None,
    ) -> None:
        self._viewport = viewport
        # Chrome's user-data-dir, LIVE on the tenant volume (see the module-level
        # durability block). None → fully ephemeral context (unit/CI runs).
        self._profile_dir = profile_dir or None
        # Regenerable disk cache redirected to a PER-SESSION node-disk emptyDir
        # dir, off the EFS profile so it never syncs to S3. Per-session is
        # REQUIRED — concurrent Chromes corrupt a shared cache dir. None → cache
        # stays under the profile (unit/CI). disk_cache_size caps Chrome's own
        # writes (bytes).
        self._cache_dir = cache_dir or None
        self._disk_cache_size = disk_cache_size or None
        # fd of the held per-profile flock, start() → close(). None = not held.
        self._profile_lock_fd: int | None = None
        # Prod: headed Chrome for Testing (lowest bot-wall friction); tests/CI:
        # headless bundled Chromium. Env-derived unless a caller pins them.
        self._headless = _env_headless() if headless is None else headless
        self._executable_path = (
            _env_executable_path() if executable_path is None else executable_path
        ) or None
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._tabs: list[_Tab] = []
        self._active_id: str | None = None
        self._tab_seq = 0
        # Screencast: only the ACTIVE tab's CDP streams. Each viewer registers a
        # frame sink; the capture + pump run ONCE and fan the freshest frame to
        # all sinks. The set being non-empty IS "screencasting". A tab switch
        # moves the capture's CDP session; the sinks are unchanged.
        self._sinks: set[Callable[[str, dict[str, Any]], Awaitable[None]]] = set()
        self._latest_frame: tuple[str, dict[str, Any]] | None = None
        self._frame_ready: asyncio.Event | None = None
        self._pump_task: asyncio.Task[None] | None = None
        # Instance attribute so tests can shrink it without patching the constant.
        self._min_frame_interval = _MIN_FRAME_INTERVAL_S
        # The CDP session actually running the capture — lets teardown prove it
        # targets the streaming session, and a late frame tell it switched away.
        self._capturing_cdp: Any = None
        # Native JS dialog policy. Default DISMISS — auto-accepting a confirm
        # would silently OK a "Delete?"/beforeunload with no human in the loop;
        # accept is opted into via the approval-gated browser_set_dialog_mode.
        self._dialog_mode = "dismiss"
        self._last_dialog: dict[str, Any] | None = None
        # Cleaned UA (Headless token stripped), computed once from the browser's
        # own UA at start() and pushed onto every tab's CDP session. None when
        # the UA needs no cleaning.
        self._ua: str | None = None
        # True only while replaying saved tabs at start(): suppresses the
        # navigation-triggered persistence so a half-restored set can't
        # overwrite the file being restored FROM.
        self._replaying = False
        # Last (urls, active) written — a main-frame framenavigated can re-fire
        # with the SAME url (SPA History churn), and rewriting an unchanged file
        # is pure EFS write amplification.
        self._last_persisted: tuple[tuple[str, ...], int] | None = None
        # Latest snapshot awaiting a write, and the writer that drains it. The
        # Event is created in start(), not here: __init__ can run off a loop.
        self._tab_persist_want: tuple[tuple[str, ...], int] | None = None
        self._tab_persist_wake: asyncio.Event | None = None
        self._tab_persist_task: asyncio.Task[None] | None = None

    def _next_tab_id(self) -> str:
        self._tab_seq += 1
        return f"t{self._tab_seq}"

    def _active(self) -> _Tab:
        for t in self._tabs:
            if t.id == self._active_id:
                return t
        return self._tabs[0]

    async def _match_viewport(self, page: Any) -> None:
        """Bring a freshly-created tab up to the current viewport. New pages inherit
        the CONTEXT viewport (the launch default), not a later human resize, so
        without this a resize is undone the moment another tab opens."""
        with contextlib.suppress(Exception):
            await page.set_viewport_size({"width": self._viewport[0], "height": self._viewport[1]})

    async def _new_page_tab(self) -> _Tab:
        page = await self._context.new_page()
        await self._match_viewport(page)  # keep a human resize across new tabs
        self._install_dialog_handler(page)  # cover every new tab before it navigates
        cdp = await self._context.new_cdp_session(page)
        await self._apply_ua_override(cdp)  # every tab carries the cleaned UA
        tab = _Tab(self._next_tab_id(), page, cdp)
        self._install_observers(tab)
        self._tabs.append(tab)
        metrics.add_open_tabs(1)
        return tab

    async def _apply_ua_override(self, cdp: Any) -> None:
        """Push the cleaned UA onto one tab's CDP session. No-op until start() has
        computed self._ua (the very first tab is overridden by start() itself once
        the UA is known)."""
        if not self._ua:
            return
        with contextlib.suppress(Exception):
            await cdp.send("Network.setUserAgentOverride", {"userAgent": self._ua})

    async def start(self) -> None:
        """Launch Chromium + open the first tab. With ``profile_dir`` set, runs
        a persistent user-data-dir live on the tenant volume: takes the
        per-profile flock, reverse-migrates an archive-era ``profile.tar.gz``,
        clears stale Singleton* locks. Without one, a throwaway ephemeral
        context (unit/CI, or a pod with no volume)."""
        # Lazy import so the module loads on hosts without Playwright installed.
        from playwright.async_api import async_playwright

        try:
            self._playwright = await async_playwright().start()
            w, h = self._viewport
            if self._cache_dir:
                await asyncio.to_thread(os.makedirs, self._cache_dir, exist_ok=True)
            if self._profile_dir:
                # ONE hop for the whole pre-launch profile step. Every part of it
                # blocks — mkdir, an flock, a directory sweep, a `tar -xzf`
                # subprocess, and an rmtree of up to nineteen cache dirs — and it
                # all runs on the loop this tool pod also serves its MCP calls and
                # the human's co-browse WebSocket from. The archive migration
                # alone is unbounded in the profile's size.
                purged = await asyncio.to_thread(self._prepare_profile_dir)
                if purged:
                    log.info("cobrowse purged %d regenerable cache dir(s) from profile", purged)
                self._context = await self._launch_persistent(self._profile_dir, w, h)
                first = await self._adopt_first_tab()
            else:
                self._browser = await self._playwright.chromium.launch(
                    headless=self._headless,
                    executable_path=self._executable_path,
                    args=[*_launch_args(), *self._cache_launch_args()],
                    ignore_default_args=_IGNORED_DEFAULT_ARGS,
                )
                self._context = await self._browser.new_context(
                    viewport={"width": w, "height": h}, device_scale_factor=_DEVICE_SCALE
                )
                first = await self._new_page_tab()
            self._active_id = first.id
            await self._prime_ua_override(first)  # UA set before any restore navigation
            if self._profile_dir:
                self._tab_persist_wake = asyncio.Event()
                self._tab_persist_task = asyncio.create_task(self._tab_persist_writer())
                await self._restore_saved_tabs(first)
        except Exception:
            metrics.inc_chromium_launch_failure()
            await self._stop_tab_persist_writer()
            self._release_profile_lock()  # a failed launch must not wedge the profile
            raise
        metrics.inc_chromium_launch()
        # Sampled unconditionally, once per session, because the restore paths
        # alone do not cover every start: a session with no saved tabs returns
        # early and never sampled, leaving the memory gauge reading a flat 0 —
        # which is indistinguishable from "this container is using no memory"
        # and would let a headroom alert bind to a series that never moves.
        log.info("cobrowse session ready tabs=%d mem=%s", len(self._tabs), await _memory_snapshot())

    async def _prime_ua_override(self, first: _Tab) -> None:
        """Compute the cleaned UA from the first tab and apply it there NOW
        (later tabs get it via _apply_ua_override). Best-effort: a failure must
        not block the session — the browser still works, just less disguised."""
        with contextlib.suppress(Exception):
            raw = await first.page.evaluate("() => navigator.userAgent")
            self._ua = _clean_ua(str(raw))
            await self._apply_ua_override(first.cdp)

    def _cache_launch_args(self) -> list[str]:
        """``--disk-cache-dir`` (+ size cap) pointing Chrome's regenerable cache
        at this session's emptyDir subdir, NOT the EFS profile. Empty when no
        cache dir is configured (unit/CI)."""
        if not self._cache_dir:
            return []
        args = [f"--disk-cache-dir={self._cache_dir}"]
        # A non-numeric cap is dropped rather than fed to Chrome — but loudly:
        # silently ignoring it would let the cache grow unbounded with no signal.
        if self._disk_cache_size:
            if self._disk_cache_size.isdigit():
                args.append(f"--disk-cache-size={self._disk_cache_size}")
            else:
                log.warning(
                    "cobrowse ignoring non-numeric BROWSER_DISK_CACHE_SIZE=%r "
                    "(want bytes); Chrome uses its default cap",
                    self._disk_cache_size,
                )
        log.info(
            "cobrowse disk cache → %s (cap=%s bytes) — off the EFS profile",
            self._cache_dir,
            self._disk_cache_size if (self._disk_cache_size or "").isdigit() else "default",
        )
        return args

    async def _launch_persistent(self, profile_dir: str, w: int, h: int) -> Any:
        """Launch Chromium against a persistent user-data-dir, clearing any stale
        Singleton lock a crashed prior process left behind."""
        await asyncio.to_thread(_prepare_persistent_dir, profile_dir)
        return await self._playwright.chromium.launch_persistent_context(
            profile_dir,
            headless=self._headless,
            executable_path=self._executable_path,
            viewport={"width": w, "height": h},
            device_scale_factor=_DEVICE_SCALE,
            args=[*_launch_args(), *self._cache_launch_args()],
            ignore_default_args=_IGNORED_DEFAULT_ARGS,
        )

    # --- live-on-PVC profile: flock + one-time reverse migration ------------------

    def _profile_lock_path(self) -> str:
        assert self._profile_dir is not None
        return profile_lock_path(self._profile_dir)

    def _prepare_profile_dir(self) -> int:
        """Everything the profile needs before Chromium may touch it, as ONE
        blocking unit: ensure the dir, take the cross-pod flock, retire
        archive-era crash scratch, reverse-migrate a ``profile.tar.gz``, and
        purge regenerable caches. Returns the purged-directory count.

        Grouped rather than offloaded call-by-call because the steps are
        strictly ordered — the flock must be held before anything reads or
        writes the profile — and because five hops would cost five context
        switches to buy exactly what one buys.

        The flock is safe to take here: ``fcntl`` locks belong to the PROCESS,
        not the thread that called ``flock``, so a lock acquired on a worker
        thread is held by this pod and released by ``_release_profile_lock``
        from wherever it runs.
        """
        assert self._profile_dir is not None
        os.makedirs(self._profile_dir, exist_ok=True)
        self._acquire_profile_lock()  # cross-pod single-writer, held to close()
        self._sweep_checkpoint_scratch()  # retire archive-era crash scratch
        self._migrate_archived_profile()  # one-time: tar.gz → live dir
        # Keep only durable state on the PVC. Under the flock, pre-launch.
        return purge_regenerable_cache(self._profile_dir)

    def _acquire_profile_lock(self) -> None:
        """Take the per-profile flock, non-blocking, held until close(). A held
        lock means another pod's live Chrome owns this profile — fail the launch
        loudly rather than corrupt a shared profile or block indefinitely."""
        lock_path = self._profile_lock_path()
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            log.warning("cobrowse profile flock held by another session/pod: %s", lock_path)
            raise RuntimeError(
                "browser profile is in use by another session/pod (profile flock held)"
            ) from exc
        self._profile_lock_fd = fd

    def _release_profile_lock(self) -> None:
        if self._profile_lock_fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(self._profile_lock_fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self._profile_lock_fd)
        self._profile_lock_fd = None

    def _sweep_checkpoint_scratch(self) -> None:
        """Remove checkpoint tmp files the ARCHIVE-ERA image may have left behind
        after a crash (they'd otherwise be backed up forever). Best-effort."""
        if not self._profile_dir:
            return
        with contextlib.suppress(OSError):
            for name in os.listdir(self._profile_dir):
                if name.startswith(_CKPT_TMP_PREFIX):
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(self._profile_dir, name))

    def _migrate_archived_profile(self) -> None:
        """One-time reverse migration: extract an archive-era profile.tar.gz
        onto the profile root so logins carry over, then delete the archive.
        Idempotent — a crash between extract and delete re-extracts next launch.
        A corrupt archive is non-fatal (log + start fresh) but kept for
        forensics rather than silently deleted."""
        if not self._profile_dir:
            return
        archive = os.path.join(self._profile_dir, _DURABLE_ARCHIVE)
        if not os.path.exists(archive):
            return
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["tar", "-xzf", archive, "-C", self._profile_dir],  # noqa: S607
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            log.warning(
                "cobrowse archived-profile migration failed (starting fresh): %s",
                proc.stderr.strip(),
            )
            return
        with contextlib.suppress(OSError):
            os.remove(archive)
        log.info("cobrowse migrated archived profile onto the live profile dir")

    # --- open-tab persistence / restore (session continuity across restart) ---

    def _persist_open_tabs(self) -> None:
        """Record tab URLs + active index for restart restore.

        SNAPSHOT ONLY — this reads Playwright objects, so it must stay on the
        event loop (``page.url`` is a cached string, but the async API's objects
        are not thread-safe and reading them from a worker thread is a bug
        waiting for a version bump). The filesystem half is handed to
        ``_tab_persist_writer``, which owns every write.

        Deliberately still a plain ``def`` with the same name and the same four
        call sites, because ONE of those call sites cannot await: Playwright
        invokes ``on_frame_navigated`` synchronously from its own loop, and that
        is the hottest of the four — every main-frame navigation. An offload
        that only fixed the three awaitable call sites would have made the
        ratchet green while leaving the busiest path blocking.

        No-op without a persistent profile or mid-replay; best-effort — a failed
        write must never break browsing.
        """
        if not self._profile_dir or self._replaying:
            return
        try:
            # _tab_url, NOT page.url: a restored-but-unloaded tab's page really
            # is about:blank, and persisting that would erase the URL it is
            # holding — the tab list would empty itself one restart after a
            # restore nobody clicked through.
            urls = [_tab_url(t) for t in self._tabs]
            active = next((i for i, t in enumerate(self._tabs) if t.id == self._active_id), 0)
        except Exception:
            log.debug("cobrowse snapshot open tabs failed", exc_info=True)
            return
        # Last-write-wins by construction: the writer reads whatever this holds
        # when it wakes, so a burst of navigations collapses to one EFS write
        # and cannot land out of order.
        self._tab_persist_want = (tuple(urls), active)
        if self._tab_persist_wake is not None:
            self._tab_persist_wake.set()

    async def _do_persist_open_tabs(self) -> None:
        """Write the pending snapshot, if it differs from what is on disk.

        ``_last_persisted`` advances only after a SUCCESSFUL write — the same
        rule the inline version followed. Marking it on the attempt would make a
        failed write look persisted and suppress every retry after it.
        """
        want = self._tab_persist_want
        if want is None or want == self._last_persisted or not self._profile_dir:
            return
        try:
            await asyncio.to_thread(_write_open_tabs, self._profile_dir, list(want[0]), want[1])
        except Exception:
            log.debug("cobrowse persist open tabs failed", exc_info=True)
            return
        self._last_persisted = want

    async def _tab_persist_writer(self) -> None:
        """The single writer. One task, so writes are serialised and the file
        can never be overtaken by a stale snapshot from a parallel thread."""
        assert self._tab_persist_wake is not None
        while True:
            await self._tab_persist_wake.wait()
            self._tab_persist_wake.clear()
            await self._do_persist_open_tabs()

    async def _stop_tab_persist_writer(self) -> None:
        """Flush the pending write, then stop the writer. Flushing FIRST is the
        point: the last thing a session does is close its tabs, and losing that
        write is losing the restore."""
        await self._do_persist_open_tabs()
        task, self._tab_persist_task = self._tab_persist_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tab_persist_wake = None

    def _read_saved_tabs(self) -> _SavedTabs | None:
        """The saved tab record, or None — a missing/garbled file is 'nothing to
        restore', never an error.

        ``restore_attempts`` is absent in files written before the guard shipped;
        that reads as 0 (clean), which is the right default for a set no crash
        has been attributed to.
        """
        if not self._profile_dir:
            return None
        try:
            with open(os.path.join(self._profile_dir, _OPEN_TABS_FILE)) as f:
                data = json.load(f)
            urls = [u for u in data.get("tabs", []) if isinstance(u, str)]
            active = int(data.get("active", 0))
            attempts = max(0, int(data.get("restore_attempts", 0)))
        except (OSError, ValueError, TypeError):
            return None
        return _SavedTabs(urls, active, attempts) if urls else None

    async def _restore_saved_tabs(self, first: _Tab) -> None:
        """Recreate the saved tab set, LOADING only the active tab.

        Every saved URL becomes a real tab so the human gets their tab bar back,
        but the non-active ones stay blank with their URL parked in
        ``pending_url`` and navigate on first activation. See
        ``_RESTORE_GUARD_EXHAUSTED_ATTEMPTS`` for why loading them all at once is
        not an option, and for the dirty bit that drops even the active tab to
        lazy after an unfinished attempt.

        A failed/blank URL is skipped, not fatal.
        """
        # The last blocking read on this path: an EFS open+parse before any tab
        # is reopened. Small, and still on the loop that drives the restore.
        saved = await asyncio.to_thread(self._read_saved_tabs)
        if saved is None:
            metrics.record_tab_restore(outcome="nothing_saved", tabs=0, loaded=0, dropped=0)
            return
        urls = saved.urls[:_MAX_RESTORE_TABS]
        dropped = len(saved.urls) - len(urls)
        # The dirty bit: a previous restore of this set started and never
        # reported success, so it is the prime suspect for whatever killed us.
        # Drop to loading nothing rather than replaying the same load.
        load_active = saved.attempts == 0
        outcome = "clean" if load_active else "degraded"
        if not load_active:
            log.warning(
                "cobrowse restore guard: %d unfinished attempt(s) on this tab set — "
                "restoring %d tab(s) WITHOUT loading any; they load on first click",
                saved.attempts,
                len(urls),
            )
            metrics.inc_restore_guard_trip()
        if saved.attempts >= _RESTORE_GUARD_EXHAUSTED_ATTEMPTS:
            # Loading nothing did not help, so the tabs are exonerated: keep
            # every URL and say so, rather than quietly deleting a human's tabs
            # to chase a cause that is somewhere else entirely.
            outcome = "exhausted"
            log.error(
                "cobrowse restore guard EXHAUSTED after %d attempt(s) that loaded no page — "
                "the saved tabs are NOT the cause; look at the session start path itself. "
                "Keeping all %d saved URL(s).",
                saved.attempts,
                len(urls),
            )
        if dropped:
            log.warning(
                "cobrowse restore dropping %d saved tab(s) over the %d cap",
                dropped,
                _MAX_RESTORE_TABS,
            )
        log.info(
            "cobrowse restoring %d saved tab(s) outcome=%s load_active=%s attempts=%d mem=%s",
            len(urls),
            outcome,
            load_active,
            saved.attempts,
            await _memory_snapshot(),
        )
        # Recorded BEFORE the first navigation and cleared after: an OOM kill is
        # a SIGKILL, so a mark still set on the next boot is the only evidence
        # the dead process can leave that its restore never finished.
        await self._mark_restore_attempt(urls, saved.active, saved.attempts + 1)
        self._replaying = True
        try:
            tabs: list[_Tab] = []
            for i, url in enumerate(urls):
                tab = first if i == 0 else await self._new_page_tab()
                tab.pending_url = url
                tabs.append(tab)
            active_tab = tabs[saved.active] if 0 <= saved.active < len(tabs) else tabs[0]
            self._active_id = active_tab.id
            if load_active:
                # Deliberately NOT via _hydrate_tab: that counts a human (or the
                # agent) coming back to a parked tab, which is the measurement
                # justifying laziness. Counting the restore's own load there
                # would put a 1 in it on every clean start and make the ratio
                # meaningless — measured in the 2026-09-09 simulation.
                active_tab.pending_url = None
                await self._safe_restore_goto(active_tab, urls[tabs.index(active_tab)])
        finally:
            self._replaying = False
        self._persist_open_tabs()  # normalize the file to what actually restored
        # Flushed rather than left to the writer: this is the state a crash
        # immediately after start() must find on disk — and it is what clears
        # the attempt mark, so the flush IS the success report.
        await self._do_persist_open_tabs()
        metrics.record_tab_restore(
            outcome=outcome, tabs=len(tabs), loaded=1 if load_active else 0, dropped=dropped
        )
        log.info(
            "cobrowse restored %d tab(s), %d loaded, mem=%s",
            len(tabs),
            1 if load_active else 0,
            await _memory_snapshot(),
        )

    async def _mark_restore_attempt(self, urls: list[str], active: int, attempts: int) -> None:
        """Stamp the saved set with an unfinished-attempt count, flushed to disk
        before any navigation runs. ``_last_persisted`` is deliberately left
        alone: the mark is not a tab-set change, and claiming it as one would
        make the post-restore write (the one that CLEARS the mark) look
        redundant and get skipped."""
        if not self._profile_dir:
            return
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                _write_open_tabs, self._profile_dir, list(urls), active, attempts
            )

    async def _hydrate_tab(self, tab: _Tab) -> None:
        """Load a restored tab's parked URL, once. No-op on an already-loaded
        tab, so every activation can call it."""
        url = tab.pending_url
        if not url:
            return
        tab.pending_url = None  # cleared FIRST: a slow load must not re-enter
        log.info("cobrowse hydrating restored tab id=%s mem=%s", tab.id, await _memory_snapshot())
        metrics.inc_tab_hydrated()
        await self._safe_restore_goto(tab, url)

    async def _safe_restore_goto(self, tab: _Tab, url: str) -> None:
        """Navigate one restored tab — a dead saved link must leave a usable
        (blank) tab, not abort the whole restore."""
        if not url or url == "about:blank":
            return
        with contextlib.suppress(Exception):
            await tab.page.goto(
                url, wait_until="domcontentloaded", timeout=_RESTORE_GOTO_TIMEOUT_MS
            )

    async def _adopt_first_tab(self) -> _Tab:
        """A persistent context opens with one blank page — adopt it as tab 1
        rather than spawning a second (else every restart accretes a blank tab)."""
        pages = list(self._context.pages)
        page = pages[0] if pages else await self._context.new_page()
        await self._match_viewport(page)  # a restored profile may reopen at a resized size
        self._install_dialog_handler(page)  # cover the first (adopted) tab too
        cdp = await self._context.new_cdp_session(page)
        tab = _Tab(self._next_tab_id(), page, cdp)
        self._install_observers(tab)
        self._tabs.append(tab)
        metrics.add_open_tabs(1)
        return tab

    # --- native dialogs (alert/confirm/prompt/beforeunload) -----------------

    def _install_dialog_handler(self, page: Any) -> None:
        """Wired onto every tab at creation so no tab can wedge on an unhandled
        dialog. Reads self._dialog_mode LIVE at fire time, so set_dialog_mode
        governs already-open tabs too."""

        async def _on_dialog(dialog: Any) -> None:
            info: dict[str, Any] = {
                "type": dialog.type,
                "message": dialog.message,
                "default_value": getattr(dialog, "default_value", "") or "",
                "action": self._dialog_mode,
                "url": page.url,
            }
            self._last_dialog = info
            metrics.inc_dialog(str(dialog.type), self._dialog_mode)
            # NEVER leave it unhandled (that blocks the page). accept()/dismiss()
            # can raise if Chromium already freed the dialog (navigation/close) —
            # swallow it: the page is unblocked either way.
            try:
                if self._dialog_mode == "accept":
                    await dialog.accept()
                else:
                    await dialog.dismiss()
            except Exception:
                log.debug("cobrowse dialog handle raced (type=%s)", info["type"])

        page.on("dialog", _on_dialog)

    async def set_dialog_mode(self, mode: str) -> None:
        # Anything but "accept" falls back to the safe "dismiss" — a typo can never
        # auto-OK a destructive confirm.
        self._dialog_mode = "accept" if mode == "accept" else "dismiss"

    async def last_dialog(self) -> dict[str, Any] | None:
        return dict(self._last_dialog) if self._last_dialog is not None else None

    # --- console + network observability (bounded per-tab rings) -------------

    def _install_observers(self, tab: _Tab) -> None:
        """Wire the console + network rings onto one tab's page. Idempotent so
        re-adopting a tab never double-registers."""
        if tab._observers_installed:
            return
        tab._observers_installed = True
        page = tab.page

        def on_console(msg: Any) -> None:
            with contextlib.suppress(Exception):
                tab.console_ring.append(
                    {"type": str(msg.type), "text": str(msg.text)[:_CONSOLE_TEXT_CAP]}
                )

        def on_page_error(exc: Any) -> None:
            # Uncaught exceptions never surface as console.* — capture them too.
            with contextlib.suppress(Exception):
                tab.console_ring.append({"type": "error", "text": str(exc)[:_CONSOLE_TEXT_CAP]})

        def on_response(resp: Any) -> None:
            with contextlib.suppress(Exception):
                req = resp.request
                tab.network_ring.append(
                    {
                        "method": str(req.method),
                        "url": _truncate_url(resp.url),
                        "status": int(resp.status),
                        "resource_type": str(req.resource_type),
                    }
                )

        def on_request_failed(req: Any) -> None:
            # A request with no response (blocked/DNS/aborted/CORS) — status 0.
            with contextlib.suppress(Exception):
                tab.network_ring.append(
                    {
                        "method": str(req.method),
                        "url": _truncate_url(req.url),
                        "status": 0,
                        "resource_type": str(req.resource_type),
                    }
                )

        def on_frame_navigated(frame: Any) -> None:
            # A MAIN-frame navigation changed this tab's URL (agent nav, human nav,
            # OR a click that followed a link) — re-record the open-tab set so a
            # restart reopens the page it's actually on. Subframe navs are ignored.
            with contextlib.suppress(Exception):
                if frame is page.main_frame:
                    self._persist_open_tabs()

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)
        page.on("framenavigated", on_frame_navigated)

    async def console_log(self) -> list[dict[str, Any]]:
        return list(self._active().console_ring)

    async def network_log(
        self, *, url_substring: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        entries = list(self._active().network_ring)
        if url_substring:
            needle = url_substring.lower()
            entries = [e for e in entries if needle in e["url"].lower()]
        if limit > 0:
            entries = entries[-limit:]
        return entries

    async def wait_for_response(self, url_substring: str, timeout_ms: int) -> dict[str, Any]:
        page = self._active().page
        needle = url_substring.lower()
        try:
            async with page.expect_response(
                lambda r: needle in r.url.lower(), timeout=float(timeout_ms)
            ) as info:
                resp = await info.value
            return {"matched": True, "status": int(resp.status), "url": _truncate_url(resp.url)}
        except Exception:
            return {"matched": False, "status": 0, "url": ""}

    # --- frames / iframes ---------------------------------------------------

    @staticmethod
    def _frame_key(frame: Any, page: Any) -> str:
        """Stable-enough child-frame identity (name|url) so a pinned frame stays
        re-findable across a page.frames re-read within the same document."""
        name = ""
        with contextlib.suppress(Exception):
            name = frame.name or ""
        url = ""
        with contextlib.suppress(Exception):
            url = frame.url or ""
        return f"{name}|{url}"

    def _active_frame(self) -> Any:
        """The Frame element ops act on: main, unless a switch_frame pin still
        resolves — a detached/navigated-away pin falls back to main and clears."""
        tab = self._active()
        page = tab.page
        key = tab.active_frame_key
        if key is None:
            return page.main_frame
        for fr in page.frames:
            if fr is page.main_frame:
                continue
            if self._frame_key(fr, page) == key and not fr.is_detached():
                return fr
        tab.active_frame_key = None
        return page.main_frame

    async def list_frames(self) -> list[dict[str, Any]]:
        page = self._active().page
        out: list[dict[str, Any]] = []
        for i, fr in enumerate(page.frames):  # [main, ...children] in document order
            name = ""
            with contextlib.suppress(Exception):
                name = fr.name or ""
            url = ""
            with contextlib.suppress(Exception):
                url = fr.url or ""
            out.append({"index": i, "name": name, "url": url})
        return out

    async def switch_frame(self, target: str) -> bool:
        tab = self._active()
        page = tab.page
        t = (target or "").strip()
        if t == "" or t.lower() == "main" or t == "0":
            tab.active_frame_key = None
            return True
        frames = list(page.frames)
        chosen: Any = None
        if t.isdigit():
            idx = int(t)
            if 0 <= idx < len(frames):
                chosen = frames[idx]
        if chosen is None:
            for fr in frames:
                fname = ""
                with contextlib.suppress(Exception):
                    fname = fr.name or ""
                if fname and fname == t:
                    chosen = fr
                    break
        if chosen is None:
            return False
        if chosen is page.main_frame:
            tab.active_frame_key = None
            return True
        tab.active_frame_key = self._frame_key(chosen, page)
        return True

    # --- tabs ---------------------------------------------------------------

    async def open(self, url: str, *, new_tab: bool = False) -> str:
        if new_tab:
            tab = await self._new_page_tab()
            # Logged with the running count and the memory it is being spent
            # against: a session that accretes tabs is only visible as a trend,
            # and the line that would have named the 2026-09-09 OOM ("tab 9 of
            # 10 at 92% of limit") did not exist.
            log.info(
                "cobrowse new tab id=%s tabs=%d mem=%s",
                tab.id,
                len(self._tabs),
                await _memory_snapshot(),
            )
            await self._activate(tab.id)
        active = self._active()
        active.pending_url = None  # an explicit navigation supersedes a parked URL
        resp = await active.page.goto(url, wait_until="domcontentloaded")
        return await self._classify_landed_page(active, resp)

    async def _classify_landed_page(self, tab: _Tab, resp: Any) -> str:
        """``page_state`` for a navigation that already succeeded. Best-effort
        by design: a classifier failure must never fail the open — but it
        reports ``"unknown"``, never ``"ok"``, so a broken classifier can't
        pass a wall off as content."""
        try:
            title = await tab.page.title()
            status = resp.status if resp else None
            headers = dict(resp.headers) if resp else {}
            state = classify_page_state(status, headers, tab.page.url, title)
        except Exception:
            log.warning("page-state classification failed for %s", _truncate_url(tab.page.url))
            return "unknown"
        if state != "ok":
            metrics.inc_page_blocked(state)
            log.info("cobrowse open landed on %s: %s", state, _truncate_url(tab.page.url))
        return state

    async def list_tabs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t in self._tabs:
            title = ""
            with contextlib.suppress(Exception):
                title = await t.page.title()
            # A restored tab reports where it POINTS and says it isn't loaded —
            # the human's tab bar shows their tabs, and the agent can tell a
            # parked tab from one it has actually looked at.
            out.append(
                {
                    "id": t.id,
                    "title": title,
                    "url": _tab_url(t),
                    "active": t.id == self._active_id,
                    "loaded": t.pending_url is None,
                }
            )
        return out

    async def switch_tab(self, tab_id: str) -> bool:
        if not any(t.id == tab_id for t in self._tabs):
            return False
        await self._activate(tab_id)
        self._persist_open_tabs()  # record the new active tab
        return True

    async def close_tab(self, tab_id: str) -> bool:
        tab = next((t for t in self._tabs if t.id == tab_id), None)
        if tab is None:
            return False
        was_active = tab_id == self._active_id
        # Stop the closing tab's screencast on ITS OWN cdp first: once removed
        # from self._tabs, _active() falls back to the replacement and teardown
        # would target the WRONG session, leaking this one's listener + capture.
        if was_active and self._sinks:
            await self._stop_screencast_on(tab)
        self._tabs = [t for t in self._tabs if t.id != tab_id]
        metrics.add_open_tabs(-1)
        if not self._tabs:
            # Never leave the session tab-less — open a fresh blank tab.
            fresh = await self._new_page_tab()
            self._active_id = None
            await self._activate(fresh.id)
        elif was_active:
            await self._activate(self._tabs[0].id)
        with contextlib.suppress(Exception):
            await tab.page.close()
        self._persist_open_tabs()  # the tab set shrank — record it
        return True

    async def _activate(self, tab_id: str) -> None:
        """Make ``tab_id`` active, moving the screencast to its CDP session."""
        if self._sinks:
            await self._stop_screencast_on(self._active())
        self._active_id = tab_id
        self._active().active_frame_key = None  # a tab switch resets to main frame
        self._latest_frame = None  # drop the previous tab's stale frame
        # A restored tab loads HERE, on the first activation — the whole point of
        # lazy restore. Before bring_to_front so the tab is already navigating
        # when the screencast picks it up.
        await self._hydrate_tab(self._active())
        with contextlib.suppress(Exception):
            await self._active().page.bring_to_front()
        if self._sinks:
            await self._start_screencast_on(self._active())

    # --- page actions (on the active tab) -----------------------------------

    async def snapshot(self) -> list[Element]:
        # Snapshot the ACTIVE FRAME (main by default; a child after switch_frame).
        # data-cobrowse-ref attributes are written into that frame's document, so
        # click/type/etc. resolve refs in the same frame.
        raw = await self._active_frame().evaluate(_SNAPSHOT_JS)
        return list(raw)

    async def click(self, ref: str) -> None:
        try:
            await self._active_frame().click(f"[data-cobrowse-ref='{ref}']")
        except Exception as e:
            _raise_ref_action_error("click", ref, e)

    async def type_text(self, ref: str, text: str) -> str:
        selector = f"[data-cobrowse-ref='{ref}']"
        try:
            await self._active_frame().fill(selector, text)
        except Exception as e:
            _raise_ref_action_error("type", ref, e)
        # Read the field BACK (best-effort): pages reformat/restrict input and
        # autocomplete widgets pop suggestions, and without a read-back the
        # agent only learns at submit time — one wasted turn to discover, one
        # to diagnose. The fill above already succeeded, so a failed read-back
        # degrades to a bare "ok", never to an error.
        observed: dict[str, Any] | None = None
        with contextlib.suppress(Exception):
            raw = await self._active_frame().evaluate(_TYPE_READBACK_JS, selector)
            observed = dict(raw) if raw else None
        return _type_note(text, observed)

    async def scroll(self, direction: str, amount: int) -> None:
        dy = -amount if direction == "up" else amount
        await self._active().page.mouse.wheel(0, dy)

    # --- reading / understanding --------------------------------------------

    async def read(self) -> str:
        raw = await self._active().page.evaluate(_READ_JS)
        return str(raw)

    async def find_text(self, query: str) -> dict[str, Any]:
        result = await self._active().page.evaluate(_FIND_JS, query)
        return dict(result)

    async def screenshot(self) -> bytes:
        # scale="css" (Playwright defaults to "device"): the 2x _DEVICE_SCALE
        # exists for the HUMAN screencast — without this cap every agent
        # screenshot would carry 4x the pixels into model context, a silent
        # per-step token-cost multiplier on every tenant's browsing.
        data = await self._active().page.screenshot(type="png", scale="css")
        return bytes(data)

    async def inspect(self, ref: str) -> dict[str, Any]:
        result = await self._active().page.evaluate(_INSPECT_JS, ref)
        return dict(result) if result else {"found": False, "ref": ref}

    async def get_table(self, ref: str | None = None) -> list[list[list[str]]]:
        result = await self._active().page.evaluate(_TABLE_JS, ref)
        return list(result)

    # --- history / reliability ----------------------------------------------

    async def go_back(self) -> None:
        await self._active().page.go_back(wait_until="domcontentloaded")

    async def go_forward(self) -> None:
        await self._active().page.go_forward(wait_until="domcontentloaded")

    async def reload(self) -> None:
        await self._active().page.reload(wait_until="domcontentloaded")

    async def wait_for(self, *, text: str | None, selector: str | None, timeout_ms: int) -> bool:
        page = self._active().page
        try:
            if selector:
                await page.wait_for_selector(selector, timeout=timeout_ms)
            elif text:
                await page.get_by_text(text).first.wait_for(timeout=timeout_ms)
            else:
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            return False
        return True

    # --- extra actions ------------------------------------------------------

    async def press_key(self, key: str) -> None:
        await self._active().page.keyboard.press(key)

    async def select_option(self, ref: str, value: str) -> None:
        await self._active().page.select_option(f"[data-cobrowse-ref='{ref}']", value)

    async def upload_file(self, ref: str, path: str) -> None:
        await self._active().page.set_input_files(f"[data-cobrowse-ref='{ref}']", path)

    async def download(self, ref: str, dest_path: str) -> dict[str, Any]:
        page = self._active().page
        async with page.expect_download() as dl:
            await page.click(f"[data-cobrowse-ref='{ref}']")
        download = await dl.value
        await download.save_as(dest_path)
        return {"filename": download.suggested_filename, "saved": True}

    async def fill_login(self, username: str, password: str) -> bool:
        # Login forms are often inside an iframe on portals — operate on the active
        # frame, not page.*.
        frame = self._active_frame()
        pw = await frame.query_selector("input[type=password]")
        if pw is None:
            return False
        await pw.fill(password)
        user = await frame.query_selector(
            "input[type=email], input[type=text], "
            "input[name*=user i], input[name*=email i], input[id*=user i]"
        )
        if user is not None:
            await user.fill(username)
        await pw.press("Enter")
        return True

    # --- interaction extras -------------------------------------------------

    async def hover(self, ref: str) -> None:
        await self._active_frame().hover(f"[data-cobrowse-ref='{ref}']")

    async def drag(self, from_ref: str, to_ref: str) -> None:
        await self._active_frame().drag_and_drop(
            f"[data-cobrowse-ref='{from_ref}']", f"[data-cobrowse-ref='{to_ref}']"
        )

    async def scroll_to(self, ref: str) -> bool:
        loc = self._active_frame().locator(f"[data-cobrowse-ref='{ref}']")
        try:
            await loc.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            return False
        return True

    async def get_options(self, ref: str) -> list[dict[str, Any]]:
        raw = await self._active_frame().evaluate(_GET_OPTIONS_JS, ref)
        return list(raw)

    async def get_links(self, cap: int = 200) -> list[dict[str, str]]:
        raw = await self._active_frame().evaluate(_GET_LINKS_JS, cap)
        return list(raw)

    async def eval_js(self, js: str) -> dict[str, Any]:
        # The wrapper stringifies + length-caps INSIDE the page and returns
        # throws as {error} — a broken/hostile snippet can't crash the tool or
        # dump an un-cappable blob into the agent's context.
        try:
            out = await self._active_frame().evaluate(_EVAL_WRAPPER_JS, js)
        except Exception as exc:  # context destroyed by a navigation, syntax error…
            return {"error": str(exc)[:500]}
        if isinstance(out, dict) and "error" in out:
            return {"error": str(out["error"])[:2000]}
        return {"result": str(out.get("result", "")) if isinstance(out, dict) else str(out)}

    async def nav_state(self) -> dict[str, Any]:
        tab = self._active()
        return {
            # Under the restore guard even the active tab can be parked, and the
            # address bar showing about:blank while the tab bar shows a URL
            # would read as data loss. Report the destination, flagged unloaded.
            "url": _tab_url(tab),
            "title": await tab.page.title(),
            "loaded": tab.pending_url is None,
            "can_go_back": False,  # POC: history introspection is a v1 nicety
            "can_go_forward": False,
        }

    # --- screencast (of the active tab) -------------------------------------

    async def add_frame_sink(self, sink: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
        # Start capture + pump on the FIRST viewer only — per-viewer starts would
        # stack duplicate CDP listeners and leak pump tasks. `first` and the add
        # are one atomic step (no await between), so two concurrent attaches
        # can't both see `first`.
        first = not self._sinks
        self._sinks.add(sink)
        metrics.set_frame_sinks(len(self._sinks))
        if first:
            self._frame_ready = asyncio.Event()
            self._pump_task = asyncio.create_task(self._screencast_pump())
            await self._start_screencast_on(self._active())

    async def _start_screencast_on(self, tab: _Tab) -> None:
        # A per-tab closure captures THIS tab's cdp so a frame is always acked
        # to the session it came from (_on_frame); stored on the tab because
        # pyee removes listeners by identity.
        async def _handler(params: dict[str, Any]) -> None:
            await self._on_frame(tab.cdp, params)

        tab._screencast_handler = _handler
        self._capturing_cdp = tab.cdp
        tab.cdp.on("Page.screencastFrame", _handler)
        await tab.cdp.send(
            # everyNthFrame stays 1 — rate limiting lives in the pump (which
            # always fans the FRESHEST frame), so throttling never shows a
            # stale frame.
            "Page.startScreencast",
            {"format": "jpeg", "quality": _SCREENCAST_QUALITY, "everyNthFrame": 1},
        )

    async def _stop_screencast_on(self, tab: _Tab) -> None:
        # Teardown must target the session that is actually capturing. If it doesn't,
        # the ordering bug (F3) has resurfaced — surface it rather than silently
        # stopping the wrong session and leaking the real one.
        if self._capturing_cdp is not None and tab.cdp is not self._capturing_cdp:
            metrics.inc_screencast_teardown_mismatch()
            log.warning("cobrowse screencast teardown targeted a non-capturing tab id=%s", tab.id)
        handler = tab._screencast_handler
        if handler is not None:
            with contextlib.suppress(Exception):
                tab.cdp.remove_listener("Page.screencastFrame", handler)
            tab._screencast_handler = None
        with contextlib.suppress(Exception):
            await tab.cdp.send("Page.stopScreencast")
        if tab.cdp is self._capturing_cdp:
            self._capturing_cdp = None

    async def _on_frame(self, origin_cdp: Any, params: dict[str, Any]) -> None:
        # Ack CDP IMMEDIATELY so it keeps streaming (never gate on the viewer
        # round-trip), and ack the session the frame CAME FROM: a frame from the
        # old tab can still land after a switch, and acking it to the new tab's
        # session is wrong (F4).
        sid = params.get("sessionId")
        if sid is not None:
            if self._capturing_cdp is not None and origin_cdp is not self._capturing_cdp:
                # A frame from a tab we've already switched away from — benign now
                # that we ack its origin, but worth measuring how often it happens.
                metrics.inc_late_frame()
                log.debug("cobrowse late screencast frame from a switched-away session")
            with contextlib.suppress(Exception):
                await origin_cdp.send("Page.screencastFrameAck", {"sessionId": sid})
        meta = params.get("metadata", {})
        self._latest_frame = (
            params["data"],
            {
                "session_frame_id": 0,  # server-acked; viewer ack is a no-op
                "width": meta.get("deviceWidth", self._viewport[0]),
                "height": meta.get("deviceHeight", self._viewport[1]),
                "device_scale": meta.get("pageScaleFactor", 1.0),
            },
        )
        if self._frame_ready is not None:
            self._frame_ready.set()

    async def _screencast_pump(self) -> None:
        """Fan the freshest frame out to every sink, rate-capped at
        ``_min_frame_interval`` (see _MIN_FRAME_INTERVAL_S). One slow viewer
        can't stall others — each send is independent, errors swallowed. Frames
        arriving during the throttle sleep overwrite ``_latest_frame``, so the
        cap trades fps, never latency-to-current-state."""
        assert self._frame_ready is not None
        loop = asyncio.get_running_loop()
        last_sent_at = 0.0
        last_sent_frame: tuple[str, dict[str, Any]] | None = None
        while True:
            await self._frame_ready.wait()
            delay = self._min_frame_interval - (loop.time() - last_sent_at)
            if delay > 0:
                await asyncio.sleep(delay)
            self._frame_ready.clear()
            latest = self._latest_frame
            if latest is None or latest is last_sent_frame:
                # Identity check: the ready-event can fire for a frame that was
                # already picked up (set between clear and the read above) —
                # skip rather than fan the same frame out twice.
                continue
            last_sent_frame = latest
            last_sent_at = loop.time()
            data, meta = latest
            for sink in list(self._sinks):
                with contextlib.suppress(Exception):
                    await sink(data, meta)

    async def ack_frame(self, frame_id: int) -> None:
        # No-op: frames are acked immediately server-side (see _on_frame).
        return None

    async def remove_frame_sink(
        self, sink: Callable[[str, dict[str, Any]], Awaitable[None]]
    ) -> None:
        # Stop the CDP capture + pump only when the LAST viewer leaves; while any
        # sink remains the screencast keeps running for the others.
        self._sinks.discard(sink)
        metrics.set_frame_sinks(len(self._sinks))
        if not self._sinks:
            await self._stop_capture()

    async def _stop_capture(self) -> None:
        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump_task
            self._pump_task = None
        self._latest_frame = None
        if self._tabs:
            await self._stop_screencast_on(self._active())

    async def send_input(self, event: str, fields: dict[str, Any]) -> None:
        try:
            method, params = to_cdp_command(event, fields)
        except ValueError:
            log.warning("cobrowse dropping malformed input event=%s", event)
            return
        await self._active().cdp.send(method, params)

    async def cursor_at(self, x: float, y: float) -> str:
        # TOP frame only — a cross-origin subframe's elementFromPoint is
        # inaccessible and returns null, read as "no reading" (viewer keeps its
        # default cursor). evaluate() wrapped so a destroyed context yields "".
        try:
            out = await self._active().page.evaluate(_CURSOR_AT_JS, {"x": x, "y": y})
        except Exception:  # context destroyed by navigation, detached frame, …
            return ""
        return out if isinstance(out, str) else ""

    async def set_viewport(self, width: int, height: int) -> None:
        # Applied to EVERY tab: the viewport is otherwise a context-level
        # default new tabs inherit, so a resize would silently revert the moment
        # another tab opens. set_viewport_size keeps device_scale_factor (2x
        # screencast stays crisp); the viewer re-derives canvas + input mapping
        # from CDP frame metadata. No-op when unchanged so a debounced resize
        # storm doesn't thrash CDP.
        w = max(_MIN_VIEWPORT_W, min(_MAX_VIEWPORT_W, width))
        h = max(_MIN_VIEWPORT_H, min(_MAX_VIEWPORT_H, height))
        if (w, h) == self._viewport:
            return
        self._viewport = (w, h)
        for tab in self._tabs:
            with contextlib.suppress(Exception):  # a raced-closed tab must not block the rest
                await tab.page.set_viewport_size({"width": w, "height": h})

    async def close(self) -> None:
        # Drop this session's tabs from the pod-wide gauge FIRST: every later
        # step can raise, and a leaked count would read as tabs that are still
        # open — the gauge is the headroom signal, so it has to be honest about
        # a session that went away badly.
        metrics.add_open_tabs(-len(self._tabs))
        self._sinks.clear()
        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump_task
            self._pump_task = None
        # context.close() on a persistent context is an IN-CHROME quit — the only
        # exit path that makes Chrome unlink its Singleton* locks (SIGTERM/SIGKILL
        # do not; verified) — so a graceful close leaves a lock-free profile on the
        # volume and the next sync round runs clean.
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        await self._stop_tab_persist_writer()
        # Chrome has exited and its writes are on the (durable) profile dir; release
        # the cross-pod flock so another pod may take this profile over.
        self._release_profile_lock()


# Injected into every candidate element as data-cobrowse-ref. Password inputs
# keep their type so redaction can find them. The label fallback chain exists
# because an <input> has no textContent: a login form once came back as two
# indistinguishable `{tag: "input", type: "text"}` entries, costing the agent a
# whole extra browser_read per field — measured 3x per task.
_LABEL_JS = """
  function labelFor(el) {
    const byIds = el.getAttribute('aria-labelledby');
    if (byIds) {
      const text = byIds.split(/\\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((n) => n.textContent || '')
        .join(' ')
        .trim();
      if (text) return text;
    }
    if (el.id) {
      // CSS.escape: an id like "user.name" is a valid id but an invalid bare
      // selector, and would throw rather than simply not match.
      const explicit = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (explicit && explicit.textContent.trim()) return explicit.textContent.trim();
    }
    const wrapping = el.closest('label');
    // An <input> contributes no textContent, so the wrapping label's text is
    // the label alone — it does not echo what the user typed.
    if (wrapping && wrapping.textContent.trim()) return wrapping.textContent.trim();
    return '';
  }
"""

_SNAPSHOT_JS = (
    """
() => {
"""
    + _LABEL_JS
    + """
  // Interactivity net: native controls, common widget ARIA roles, and the
  // generic interactivity attributes ([onclick], focusable tabindex). Custom
  // checkboxes/tabs/menus are invisible to the agent without the role list —
  // it cannot click what the snapshot does not show.
  const sel = 'a,button,input,textarea,select,summary,' +
    '[onclick],[tabindex]:not([tabindex="-1"]),' +
    '[role=button],[role=link],[role=checkbox],[role=radio],[role=switch],' +
    '[role=tab],[role=menuitem],[role=option],[role=combobox],' +
    '[role=searchbox],[role=slider]';
  const nodes = Array.from(document.querySelectorAll(sel));
  return nodes.slice(0, 200).map((el, i) => {
    const ref = 'e' + i;
    el.setAttribute('data-cobrowse-ref', ref);
    const out = { ref, tag: el.tagName.toLowerCase() };
    if (el.type) out.type = el.type;
    const role = el.getAttribute('role'); if (role) out.role = role;
    const name = (
      el.getAttribute('aria-label') ||
      el.textContent ||
      labelFor(el) ||
      el.getAttribute('placeholder') ||
      el.getAttribute('name') ||
      el.id ||
      ''
    ).trim().slice(0, 120);
    if (name) out.name = name;
    if ('value' in el && el.value != null) out.value = String(el.value).slice(0, 200);
    const ph = el.getAttribute('placeholder'); if (ph) out.placeholder = ph;
    return out;
  });
}
"""
)

# Post-type read-back for _type_note: the field's live value plus the two
# attributes that mark an autocomplete widget. Takes the same selector string
# type_text just filled, so the two cannot disagree about the target.
_TYPE_READBACK_JS = """
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const out = { tag: el.tagName.toLowerCase() };
  if (el.type) out.type = el.type;
  if ('value' in el && el.value != null) out.value = String(el.value).slice(0, 200);
  const role = el.getAttribute('role'); if (role) out.role = role;
  const ac = el.getAttribute('aria-autocomplete'); if (ac) out.autocomplete = ac;
  return out;
}
"""

# Readable text of the main content (article/main if present, else body), capped
# so a huge page can't blow the agent's context.
_READ_JS = """
() => {
  const main = document.querySelector('main, article, [role=main]') || document.body;
  const text = (main.innerText || '').replace(/\\n{3,}/g, '\\n\\n').trim();
  return text.slice(0, 15000);
}
"""

# Count case-insensitive occurrences of a query in the page text and scroll the
# first hit into view; return a short surrounding snippet.
_FIND_JS = """
(query) => {
  const q = (query || '').toLowerCase();
  if (!q) return { count: 0, snippet: '' };
  const text = document.body.innerText || '';
  const hay = text.toLowerCase();
  let count = 0, idx = hay.indexOf(q);
  const first = idx;
  while (idx !== -1) { count++; idx = hay.indexOf(q, idx + q.length); }
  let snippet = '';
  if (first !== -1) snippet = text.slice(Math.max(0, first - 60), first + q.length + 60).trim();
  // Best-effort scroll: find the first element whose text contains the query.
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    if ((n.textContent || '').toLowerCase().includes(q)) {
      const el = n.parentElement;
      if (el && el.scrollIntoView) { el.scrollIntoView({ block: 'center' }); }
      break;
    }
  }
  return { count, snippet };
}
"""

# Details of one element, addressed by its data-cobrowse-ref from a snapshot.
_INSPECT_JS = """
(ref) => {
  const el = document.querySelector('[data-cobrowse-ref="' + ref + '"]');
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const attrs = {};
  for (const a of el.attributes) attrs[a.name] = a.value;
  const style = getComputedStyle(el);
  return {
    found: true, ref,
    tag: el.tagName.toLowerCase(),
    text: (el.innerText || el.textContent || '').trim().slice(0, 300),
    attributes: attrs,
    box: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
    visible: !!(r.width && r.height && style.visibility !== 'hidden' && style.display !== 'none'),
    disabled: !!el.disabled,
  };
}
"""

# Extract tables as [table][row][cell]. With a ref, just that table.
_TABLE_JS = """
(ref) => {
  let tables;
  if (ref) {
    const el = document.querySelector('[data-cobrowse-ref="' + ref + '"]');
    const t = el ? el.closest('table') || el.querySelector('table') : null;
    tables = t ? [t] : [];
  } else {
    tables = Array.from(document.querySelectorAll('table')).slice(0, 10);
  }
  return tables.map((t) =>
    Array.from(t.rows).slice(0, 200).map((row) =>
      Array.from(row.cells).map((c) => (c.innerText || '').trim().slice(0, 300))
    )
  );
}
"""


# Native <select> options, addressed by data-cobrowse-ref. Empty for a non-select.
_GET_OPTIONS_JS = """
(ref) => {
  const el = document.querySelector("[data-cobrowse-ref='" + ref + "']");
  if (!el || el.tagName.toLowerCase() !== 'select') return [];
  return Array.from(el.options).slice(0, 200).map((o) => ({
    value: String(o.value),
    label: (o.textContent || '').trim().slice(0, 200),
    selected: !!o.selected,
  }));
}
"""

# All links as [{text, href}] with ABSOLUTE hrefs (a.href is DOM-resolved), deduped
# and capped for navigation planning.
_GET_LINKS_JS = """
(cap) => {
  const seen = new Set();
  const out = [];
  for (const a of Array.from(document.querySelectorAll('a[href]'))) {
    const href = a.href;
    if (!href || href.startsWith('javascript:') || seen.has(href)) continue;
    seen.add(href);
    out.push({ text: (a.textContent || '').trim().slice(0, 200), href: href });
    if (out.length >= cap) break;
  }
  return out;
}
"""

# Run agent-authored JS: resolve a returned Promise, JSON-stringify + cap the value
# IN the page, and turn a throw into {error} so page.evaluate never surfaces a crash.
_EVAL_WRAPPER_JS = """
(src) => {
  const CAP = 20000;
  const run = () => (0, eval)(src);
  try {
    return Promise.resolve(run()).then(
      (v) => {
        let s;
        try { s = typeof v === 'string' ? v : JSON.stringify(v); }
        catch (e) { s = String(v); }
        if (s === undefined) s = 'undefined';
        return { result: String(s).slice(0, CAP) };
      },
      (e) => ({ error: String((e && e.message) || e).slice(0, 2000) })
    );
  } catch (e) {
    return { error: String((e && e.message) || e).slice(0, 2000) };
  }
}
"""

# Read the computed CSS cursor at a point, defensively: any failure (a page that
# overrode getComputedStyle, a detached node) yields "" so the caller falls back
# to the default cursor. Returns the raw value; the pod normalizes + allowlists.
_CURSOR_AT_JS = """
({x, y}) => {
  try {
    const el = document.elementFromPoint(x, y);
    if (!el) return "";
    const c = getComputedStyle(el).cursor;
    return typeof c === "string" ? c.slice(0, 200) : "";
  } catch (e) {
    return "";
  }
}
"""

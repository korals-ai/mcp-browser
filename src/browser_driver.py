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
from typing import TYPE_CHECKING, Any, Protocol

from src import metrics
from src.input_map import to_cdp_command
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

# Screencast picture tuning. The three knobs balance each other and were set
# TOGETHER — don't tune one in isolation:
# - _DEVICE_SCALE 2: the context renders at 2x DPI so screencast JPEGs carry
#   2x the CSS viewport's pixels (2560x1600 for the default 1280x800, more once
#   a human resize grows the viewport — the 2x ratio holds) — this is what makes text
#   crisp on the viewer's HiDPI display (at 1x the picture is blurry no matter
#   the JPEG quality). CDP frame *metadata* stays in CSS px; the web viewer
#   sizes its canvas from the JPEG's own pixels and its input math from the
#   metadata (see apps/chat-ui/src/lib/cobrowsePaint.ts).
# - _SCREENCAST_QUALITY 85: below ~60, JPEG ringing artifacts on text are
#   visible even at native size; 85 keeps small UI text and thin strokes crisp.
#   Frames are larger than at 70, but the pump only ever fans out the FRESHEST
#   frame (stale ones are dropped), so steady-state egress stays bounded.
# - _MIN_FRAME_INTERVAL_S: CDP emits up to ~60 fps while a page animates —
#   frames no human needs. Capping fan-out at ~15 fps keeps scrolling and
#   cursor motion visibly smooth (10 fps reads as steppy) while still discarding
#   the bulk of CDP's frames. The pump fans out only the FRESHEST frame, so the
#   cap bounds egress: worst case is ~15 x per-frame bytes per viewer. See the
#   CoBrowseEgressHigh alert (infra/grafana_cloud/cobrowse_alerts.tf) for the
#   per-pod ceiling this is expected to stay under.
_DEVICE_SCALE = 2
_SCREENCAST_QUALITY = 85
_MIN_FRAME_INTERVAL_S = 0.066

# Bounds for a human-driven viewport resize (:meth:`PlaywrightDriver.set_viewport`).
# The floor keeps a sliver-sized panel from rendering an unusable page; the ceiling
# caps the screencast JPEG — which carries _DEVICE_SCALE x these pixels — so a
# maximized panel on a 4K display can't balloon per-frame egress without limit.
_MIN_VIEWPORT_W, _MIN_VIEWPORT_H = 400, 300
_MAX_VIEWPORT_W, _MAX_VIEWPORT_H = 2560, 1600

# Chromium launch flags that lower the automation fingerprint. Co-browse drives
# REAL user portals (tender sites, supplier logins, Google) on the human's
# behalf — a browser advertising itself as automated gets CAPTCHA-walled or
# outright blocked, which breaks the feature for the user. Verified empirically:
# `--disable-blink-features=AutomationControlled` flips `navigator.webdriver`
# from true to false; stripping the "Headless" UA token (see `_clean_ua`) drops
# the other dominant signal. This is not evasion for its own sake — it makes an
# agent-driven session look like the ordinary browser session the human would
# otherwise run themselves. (Residual signals like navigator.plugins=0 remain;
# chasing full stealth is an arms race with no end and is out of scope.)
_STEALTH_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]

# Flags every containerized Chrome launch needs, independent of fingerprint. The
# setuid/namespace sandbox can't initialize in the unprivileged per-tenant pod
# (uid 65532, no CAP_SYS_ADMIN) — the pod itself (per-tenant, non-root,
# egress-fenced) is the isolation boundary, so --no-sandbox is safe here.
# K8s /dev/shm defaults to 64Mi (too small for Chrome's shared memory) → route it
# to /tmp instead; the pod has no GPU → software rendering.
_CONTAINER_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]


def _launch_args() -> list[str]:
    """The full Chrome arg list handed to every launch: fingerprint + container."""
    return [*_STEALTH_LAUNCH_ARGS, *_CONTAINER_LAUNCH_ARGS]


def _env_headless() -> bool:
    """Whether to launch Chrome headless. Prod runs HEADED (a real window under
    Xvfb) for the lowest automation fingerprint on real portals; the test/CI
    image has no X server, so the default is headless. ``BROWSER_HEADLESS=false``
    (set in the pod image) → headed."""
    return os.environ.get("BROWSER_HEADLESS", "true").strip().lower() not in ("false", "0", "no")


def _env_executable_path() -> str | None:
    """The pinned Chrome for Testing binary baked into the pod image
    (``BROWSER_EXECUTABLE_PATH``). Unset (tests/CI) → Playwright's bundled
    Chromium, so the unit suite needs no browser install."""
    return os.environ.get("BROWSER_EXECUTABLE_PATH") or None


def _clean_ua(ua: str) -> str | None:
    """Strip the ``Headless`` marker from a Chromium UA (``HeadlessChrome`` →
    ``Chrome``), returning the cleaned string — or None if it needs no change.
    Derived from the browser's OWN reported UA at runtime, so the Chrome version
    never drifts out of sync with a hardcoded constant."""
    if "Headless" not in ua:
        return None
    return ua.replace("HeadlessChrome", "Chrome").replace("Headless", "")


def _truncate_url(url: str) -> str:
    """Cap a URL for the network log: collapse huge inline ``data:``/``blob:`` URIs
    to a short scheme tag, and length-cap the rest with an ellipsis."""
    u = url or ""
    if u.startswith("data:") or u.startswith("blob:"):
        return u.split(",", 1)[0][:40]  # e.g. "data:image/png;base64" — not the payload
    return u if len(u) <= _URL_CAP else u[:_URL_CAP] + "…"


# Where a session's open-tab set is recorded inside its (EFS-durable) profile dir,
# so a pod/container restart can reopen the same tabs instead of one blank page.
# Chrome ignores unknown files in its user-data-dir, so a sidecar file here is safe.
_OPEN_TABS_FILE = ".cobrowse_open_tabs.json"
# Bound a restore: don't reopen a pathological number of tabs, and cap each tab's
# load so one slow/hung URL can't stall the whole session start.
_MAX_RESTORE_TABS = 20
_RESTORE_GOTO_TIMEOUT_MS = 15000

# Chrome's user-data-dir runs LIVE on the tenant volume (<profile_dir> itself), so
# every cookie/login/IndexedDB write is durable the moment Chrome makes it —
# continuous durability, no checkpoint interval, no coverage lists. The two costs of
# this placement are handled at their owners: Chrome's Singleton* lock symlinks
# (dangling by design) are cleared by US at every launch + container boot under a
# per-profile flock, and the S3 sync layer is strict complete-or-nothing (a walker
# skip can never silently truncate the backup — see apps/base-images/s3-sync).
# Design: docs/plan/20260731T123000Z-cobrowse-profile-durability.md;
# incident: docs/incidents/2026-07-30-cobrowse-singleton-symlink-backup-erosion.md.
#
# One release earlier the durable store was a single tar.gz snapshot
# (<profile_dir>/profile.tar.gz) of an ephemeral runtime dir; on first launch we
# reverse-migrate it (extract to the profile root, delete the archive) so logins
# carry over, then never write an archive again.
_DURABLE_ARCHIVE = "profile.tar.gz"
# tar tmp prefix the archive-era checkpointer staged (swept if a crash left one).
_CKPT_TMP_PREFIX = ".profile.tar.gz.tmp-"
# Cross-pod mutual exclusion for the profile: an fcntl flock held for the whole
# session (launch → close). Chrome's own SingletonLock cannot serve this purpose
# across pods — it encodes hostname+pid, and a dead pod's lock just makes the next
# Chrome refuse with 'profile in use' — so we clear Chrome's lock and hold a real
# one on the shared volume instead (EFS NFSv4 flocks; same pattern as the
# workspace image's per-chat locks). Anyone clearing Singleton* — or
# garbage-collecting a profile (:mod:`src.profile_gc`) — MUST hold this.
#
# The lock lives OUTSIDE the profile dir, in a sibling ``.cobrowse/locks/<id>.lock``
# that is NEVER deleted, so the GC can rename-then-rm the profile dir without
# unlinking the exclusion inode mid-delete. Were the lock inside the dir, a
# concurrent launch would O_CREAT a fresh inode at the same path and flock it while
# the dir is half-gone → SQLite/cookie corruption (a proven race). A launch and the
# GC contend on this one stable inode instead. See B.1 in
# docs/plan/20260810T182738Z-cobrowse-profile-footprint.md.
_LOCKS_SUBDIR = "locks"


def profile_lock_path(profile_dir: str) -> str:
    """Stable external flock path for the profile at ``profile_dir``:
    ``.cobrowse/profile/<id>`` → ``.cobrowse/locks/<id>.lock``. Lives outside the
    (deletable) profile subtree and is never unlinked, so a launch and the profile
    GC mutually exclude on it across the profile's own deletion."""
    profile_dir = profile_dir.rstrip("/")
    chat_id = os.path.basename(profile_dir)
    cobrowse_root = os.path.dirname(os.path.dirname(profile_dir))
    return os.path.join(cobrowse_root, _LOCKS_SUBDIR, f"{chat_id}.lock")


def _clear_singleton_locks(profile_dir: str) -> None:
    """Remove Chromium's ``Singleton{Lock,Cookie,Socket}`` from a persistent
    profile. Only an in-Chrome quit unlinks them (SIGTERM/SIGKILL do not —
    verified), and a next pod has a different hostname, so Chromium treats a stale
    lock as 'profile in use by another computer' and refuses to launch. Clearing is
    therefore load-bearing, and is only safe under the profile flock (the caller's
    responsibility) — never delete a LIVE Chrome's locks."""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        with contextlib.suppress(OSError):
            os.remove(os.path.join(profile_dir, name))


# Regenerable browser caches Chrome recreates on demand. `Cache` + `Code Cache`
# are ALSO redirected off the PVC to the per-pod emptyDir (--disk-cache-dir), so
# any copy left in the profile is stale (pre-redirect) dead weight; the rest
# (GPU/shader/crx/Service-Worker caches) are un-relocatable but equally
# regenerable. Deleting them at launch keeps the on-PVC profile to durable state
# only (cookies/logins/IndexedDB) and stops cache accumulating across sessions +
# syncing to S3 — the WorkspaceTenantBloat cause. The per-launch complement to the
# boot-time orphan GC. Relative to the profile dir (root + Chrome's `Default`
# sub-profile). See docs/plan/20260810T182738Z-cobrowse-profile-footprint.md.
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


def purge_regenerable_cache(profile_dir: str) -> int:
    """Delete regenerable browser cache dirs from a profile (best-effort); returns
    the count removed. MUST be called under the profile flock with no live Chrome
    on the dir — Chrome regenerates what it needs on the next launch, and the
    durable state (cookies/logins/IndexedDB) is untouched (only cache names are
    matched, never Cookies/Local Storage/IndexedDB/Sessions)."""
    removed = 0
    for rel in _REGENERABLE_CACHE_SUBDIRS:
        target = os.path.join(profile_dir, rel)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
            if not os.path.isdir(target):
                removed += 1
    return removed


def clear_stale_profile_locks(base_dir: str) -> int:
    """Container-boot hygiene: sweep every chat profile under ``base_dir`` and
    remove stale ``Singleton*`` left by hard-killed pods, so the S3 sync of the
    tenant volume runs clean between sessions. Each profile is tried under a
    NON-blocking flock — a held lock means a live Chrome (another pod) owns that
    profile, and we must not touch its locks. Returns the number of profiles
    swept. Best-effort: a boot sweep must never block serving."""
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

    async def open(self, url: str, *, new_tab: bool = False) -> None:
        """Navigate the active tab to ``url`` (or open a new tab first)."""
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

    async def click(self, ref: str) -> None:
        """Click the element identified by a ref from a prior snapshot."""
        ...

    async def type_text(self, ref: str, text: str) -> None:
        """Type ``text`` into the element identified by ``ref``."""
        ...

    async def scroll(self, direction: str, amount: int) -> None:
        """Scroll the active tab ``up``/``down`` by ``amount`` pixels (viewing)."""
        ...

    # --- reading / understanding (view-only) --------------------------------

    async def read(self) -> str:
        """The active tab's readable text (main content, capped)."""
        ...

    async def find_text(self, query: str) -> dict[str, Any]:
        """Find ``query`` on the page; scroll the first hit into view. Returns
        ``{count, snippet}``."""
        ...

    async def screenshot(self) -> bytes:
        """A PNG screenshot of the active tab's viewport."""
        ...

    async def inspect(self, ref: str) -> dict[str, Any]:
        """Details of one element (tag, attributes, text, box, visible/enabled)."""
        ...

    async def get_table(self, ref: str | None = None) -> list[list[list[str]]]:
        """Extract HTML tables as ``[table][row][cell]``. ``ref`` = one table;
        None = all tables on the page."""
        ...

    # --- history / reliability (view-only) ----------------------------------

    async def go_back(self) -> None:
        """Navigate back in history."""
        ...

    async def go_forward(self) -> None:
        """Navigate forward in history."""
        ...

    async def reload(self) -> None:
        """Reload the active tab."""
        ...

    async def wait_for(self, *, text: str | None, selector: str | None, timeout_ms: int) -> bool:
        """Wait until ``text`` appears (or CSS ``selector`` matches). False on
        timeout."""
        ...

    # --- extra actions (mutating — approved in chat) ------------------------

    async def press_key(self, key: str) -> None:
        """Press a key on the active tab (Enter/Escape/Tab/ArrowDown/…)."""
        ...

    async def select_option(self, ref: str, value: str) -> None:
        """Choose ``value`` in a native ``<select>`` identified by ``ref``."""
        ...

    async def upload_file(self, ref: str, path: str) -> None:
        """Set a file input (``ref``) to a file at ``path`` on the tenant volume."""
        ...

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

    async def last_dialog(self) -> dict[str, Any] | None:
        """The most recent auto-handled dialog, or None."""
        ...

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

    async def hover(self, ref: str) -> None:
        """Hover the pointer over the element ``ref`` (reveals hover menus)."""
        ...

    async def drag(self, from_ref: str, to_ref: str) -> None:
        """Drag the ``from_ref`` element onto the ``to_ref`` element."""
        ...

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

    async def close(self) -> None:
        """Tear down all tabs + the browser context."""
        ...


class _Tab:
    """One Chromium page + its CDP session, tracked by a stable id."""

    def __init__(self, tab_id: str, page: Any, cdp: Any) -> None:
        self.id = tab_id
        self.page = page
        self.cdp = cdp
        # The Page.screencastFrame listener bound to THIS tab's CDP session while it
        # is streaming (None otherwise). Stored so we remove the exact callable we
        # registered — a per-tab closure that captures this cdp, so a late frame is
        # acked to the session it came from, and removal never mismatches a shared
        # handler across tabs.
        self._screencast_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        # Which frame element ops act on (None = the page's main frame). A prior
        # switch_frame pins a child frame here; reset on a tab switch.
        self.active_frame_key: str | None = None
        # Bounded per-tab observability buffers (console + network), populated by
        # the listeners _install_observers wires onto the page.
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
        # Chrome's user-data-dir, LIVE on the tenant volume: every write is durable
        # the moment Chrome makes it (survives pod death via the PVC; survives reap
        # via the strict S3 sync). None → fully ephemeral context (unit/CI runs).
        self._profile_dir = profile_dir or None
        # Chrome's REGENERABLE disk cache (HTTP + Code Cache) is redirected here via
        # --disk-cache-dir — a PER-SESSION dir on a node-disk emptyDir, OFF the EFS
        # profile so it never syncs to S3. None → cache stays under the profile
        # (unit/CI runs, or when BROWSER_CACHE_DIR is unset). disk_cache_size caps
        # Chrome's own writes (bytes, --disk-cache-size). See the profile-footprint
        # plan; per-session isolation is REQUIRED (concurrent Chromes corrupt a
        # shared cache dir).
        self._cache_dir = cache_dir or None
        self._disk_cache_size = disk_cache_size or None
        # fd of the held per-profile flock (cross-pod mutual exclusion), held from
        # start() until close(). None while not holding it.
        self._profile_lock_fd: int | None = None
        # Prod: headed real Chrome (Chrome for Testing) for the lowest bot-wall
        # friction; tests/CI: headless bundled Chromium (no X server, no CfT
        # binary). Env-derived unless a caller pins them explicitly.
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
        # Screencast: only the ACTIVE tab's CDP streams. Every attached viewer
        # registers a frame sink here; the CDP capture + pump run once and fan the
        # freshest frame out to ALL sinks, so N viewers share one capture. The set
        # being non-empty IS "screencasting". On tab switch, capture moves CDP
        # sessions but the sinks are unchanged.
        self._sinks: set[Callable[[str, dict[str, Any]], Awaitable[None]]] = set()
        self._latest_frame: tuple[str, dict[str, Any]] | None = None
        self._frame_ready: asyncio.Event | None = None
        self._pump_task: asyncio.Task[None] | None = None
        # Fan-out rate cap (see _MIN_FRAME_INTERVAL_S). An instance attribute so
        # tests can shrink it without patching the module constant.
        self._min_frame_interval = _MIN_FRAME_INTERVAL_S
        # The CDP session currently running the capture (the active tab's, while any
        # viewer is attached). Tracked so teardown can prove it targets the session
        # that was actually streaming and a late frame can tell it switched away.
        self._capturing_cdp: Any = None
        # Session-wide policy for native JS dialogs (alert/confirm/prompt/
        # beforeunload). Default DISMISS — auto-accepting a confirm would silently
        # OK a "Delete?"/"Submit?"/beforeunload with no human in the loop. The
        # agent opts into accept via the approval-gated browser_set_dialog_mode.
        self._dialog_mode = "dismiss"
        self._last_dialog: dict[str, Any] | None = None
        # Cleaned user-agent (Headless token stripped), computed once from the
        # browser's own UA at start(). Applied to every tab's CDP session so
        # navigator.userAgent + request headers carry it. None once we've
        # confirmed the browser's UA already needs no cleaning.
        self._ua: str | None = None
        # True only while replaying saved tabs at start(): suppresses the
        # navigation-triggered persistence so a half-restored set can't overwrite
        # the file we're restoring FROM.
        self._replaying = False
        # Last (urls, active) written, to skip redundant EFS writes — a main-frame
        # framenavigated can re-fire with the SAME url (SPA History API churn), and
        # rewriting an unchanged file every time is pure write amplification.
        self._last_persisted: tuple[tuple[str, ...], int] | None = None

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
        """Launch Chromium + open the first tab. Import Playwright here so the
        module imports cleanly on hosts without the browser installed.

        With ``profile_dir`` set, Chromium runs against a **persistent**
        user-data-dir — the profile dir itself, live on the tenant volume, so
        cookies/logins are durable continuously. The launch takes the per-profile
        flock first (cross-pod single-writer), reverse-migrates an archive-era
        ``profile.tar.gz`` if one exists, and clears any stale Singleton* locks.
        Without a profile dir, a throwaway ephemeral context is used (unit/CI runs,
        or a pod with no volume)."""
        from playwright.async_api import async_playwright  # lazy: heavy dep

        try:
            self._playwright = await async_playwright().start()
            w, h = self._viewport
            if self._cache_dir:
                # This session's own cache dir on the emptyDir (never the profile).
                os.makedirs(self._cache_dir, exist_ok=True)
            if self._profile_dir:
                os.makedirs(self._profile_dir, exist_ok=True)
                self._acquire_profile_lock()  # cross-pod single-writer, held to close()
                self._sweep_checkpoint_scratch()  # retire archive-era crash scratch
                self._migrate_archived_profile()  # one-time: tar.gz → live dir
                # Keep only durable state on the PVC: drop regenerable cache (the
                # HTTP/Code cache already redirects to the emptyDir; this clears
                # the stale on-PVC copy + the un-relocatable GPU/SW cache tail so
                # it never accumulates across sessions). Under the flock, pre-launch.
                purged = purge_regenerable_cache(self._profile_dir)
                if purged:
                    log.info("cobrowse purged %d regenerable cache dir(s) from profile", purged)
                self._context = await self._launch_persistent(self._profile_dir, w, h)
                first = await self._adopt_first_tab()
            else:
                self._browser = await self._playwright.chromium.launch(
                    headless=self._headless,
                    executable_path=self._executable_path,
                    args=[*_launch_args(), *self._cache_launch_args()],
                )
                self._context = await self._browser.new_context(
                    viewport={"width": w, "height": h}, device_scale_factor=_DEVICE_SCALE
                )
                first = await self._new_page_tab()
            self._active_id = first.id
            await self._prime_ua_override(first)  # UA set before any restore navigation
            if self._profile_dir:
                await self._restore_saved_tabs(first)
        except Exception:
            metrics.inc_chromium_launch_failure()
            self._release_profile_lock()  # a failed launch must not wedge the profile
            raise
        metrics.inc_chromium_launch()

    async def _prime_ua_override(self, first: _Tab) -> None:
        """Read the browser's own UA from the first tab; if it advertises Headless,
        compute the cleaned UA and apply it to the first tab NOW (later tabs pick it
        up via _apply_ua_override). Best-effort: a UA read/override failure must not
        block the session — the browser still works, just less disguised."""
        with contextlib.suppress(Exception):
            raw = await first.page.evaluate("() => navigator.userAgent")
            self._ua = _clean_ua(str(raw))
            await self._apply_ua_override(first.cdp)

    def _cache_launch_args(self) -> list[str]:
        """``--disk-cache-dir`` (+ ``--disk-cache-size`` cap) so Chrome's
        regenerable HTTP/Code cache lands on this session's emptyDir subdir, NOT
        the EFS profile. Empty when no cache dir is configured (unit/CI, or
        BROWSER_CACHE_DIR unset). The dir is per-session (server._cache_dir_for);
        a shared dir corrupts concurrent Chromes + leaks cache across chats."""
        if not self._cache_dir:
            return []
        args = [f"--disk-cache-dir={self._cache_dir}"]
        # str of bytes from BROWSER_DISK_CACHE_SIZE; ignore a non-numeric value
        # rather than feed Chrome a flag it silently drops — but say so, don't
        # swallow it (a mis-set cap would otherwise let the cache grow unbounded
        # with no signal).
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
        os.makedirs(profile_dir, exist_ok=True)
        _clear_singleton_locks(profile_dir)
        return await self._playwright.chromium.launch_persistent_context(
            profile_dir,
            headless=self._headless,
            executable_path=self._executable_path,
            viewport={"width": w, "height": h},
            device_scale_factor=_DEVICE_SCALE,
            args=[*_launch_args(), *self._cache_launch_args()],
        )

    # --- live-on-PVC profile: flock + one-time reverse migration ------------------
    #
    # Chrome runs directly on <profile_dir> (the tenant volume). The per-profile
    # flock is the cross-pod single-writer guarantee Chrome's own SingletonLock
    # cannot provide across pods; it is held for the whole session so no other pod
    # can clear our live locks or double-launch on this profile.

    def _profile_lock_path(self) -> str:
        assert self._profile_dir is not None
        return profile_lock_path(self._profile_dir)

    def _acquire_profile_lock(self) -> None:
        """Take the per-profile flock, non-blocking. A held lock means another pod's
        live Chrome owns this profile — fail the launch loudly rather than corrupt a
        shared profile or block a user request indefinitely."""
        lock_path = self._profile_lock_path()
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)  # external .cobrowse/locks/
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
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
        """One-time reverse migration from the archive-era layout: a previous image
        kept the profile as <profile_dir>/profile.tar.gz (tar of the user-data-dir
        root). Extract it onto the profile root so logins carry over, then delete
        the archive — from here on the live dir IS the durable store. Idempotent: a
        crash between extract and delete just re-extracts the same content next
        launch. A corrupt archive is non-fatal (log + start fresh) but is kept for
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
        """Record the current tabs' URLs + active index into the profile dir, so a
        restart can reopen them. No-op without a persistent profile or mid-replay.
        Best-effort and non-fatal: a failed write must never break browsing.

        Writes atomically (tmp + os.replace) so a crash mid-write can't leave a torn
        file. If nothing worth restoring is open (only blank tabs), the file is
        removed so a stale set doesn't linger."""
        if not self._profile_dir or self._replaying:
            return
        try:
            urls = [t.page.url for t in self._tabs]
            active = next((i for i, t in enumerate(self._tabs) if t.id == self._active_id), 0)
            key = (tuple(urls), active)
            if key == self._last_persisted:
                return  # unchanged since the last write — skip the EFS churn
            path = os.path.join(self._profile_dir, _OPEN_TABS_FILE)
            if not any(u and u != "about:blank" for u in urls):
                with contextlib.suppress(OSError):
                    os.remove(path)
                self._last_persisted = key
                return
            tmp = f"{path}.tmp"
            with open(tmp, "w") as f:
                json.dump({"tabs": urls, "active": active}, f)
            os.replace(tmp, path)
            self._last_persisted = key
        except Exception:
            log.debug("cobrowse persist open tabs failed", exc_info=True)

    def _read_saved_tabs(self) -> tuple[list[str], int] | None:
        """The saved ``(urls, active_index)`` from a prior session, or None when
        there's no profile dir / no file / a garbled file (treated as 'nothing to
        restore', never an error)."""
        if not self._profile_dir:
            return None
        try:
            with open(os.path.join(self._profile_dir, _OPEN_TABS_FILE)) as f:
                data = json.load(f)
            urls = [u for u in data.get("tabs", []) if isinstance(u, str)]
            active = int(data.get("active", 0))
        except (OSError, ValueError, TypeError):
            return None
        return (urls, active) if urls else None

    async def _restore_saved_tabs(self, first: _Tab) -> None:
        """Reopen the saved tab set onto a freshly-launched persistent context: the
        first saved URL on the already-adopted ``first`` tab, the rest as new tabs,
        loaded concurrently (bounded) so many tabs don't serialize the session start.
        A single failed/blank URL is skipped, not fatal. Restores the active tab."""
        saved = self._read_saved_tabs()
        if saved is None:
            return
        urls, active = saved
        self._replaying = True
        try:
            targets: list[tuple[_Tab, str]] = []
            for i, url in enumerate(urls[:_MAX_RESTORE_TABS]):
                tab = first if i == 0 else await self._new_page_tab()
                targets.append((tab, url))
            await asyncio.gather(*(self._safe_restore_goto(t, u) for t, u in targets))
            if 0 <= active < len(self._tabs):
                self._active_id = self._tabs[active].id
        finally:
            self._replaying = False
        self._persist_open_tabs()  # normalize the file to what actually restored

    async def _safe_restore_goto(self, tab: _Tab, url: str) -> None:
        """Navigate one restored tab, tolerating a blank/failed/slow URL — a dead
        saved link must leave a usable (blank) tab, not abort the whole restore."""
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
        return tab

    # --- native dialogs (alert/confirm/prompt/beforeunload) -----------------

    def _install_dialog_handler(self, page: Any) -> None:
        """Wire a dialog handler onto ONE page (every tab, at creation) so no tab
        can wedge on an unhandled dialog. Reads self._dialog_mode LIVE at fire time,
        so set_dialog_mode governs already-open tabs too."""

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
        """Wire bounded console + network ring buffers onto ONE tab's page.
        Idempotent (guarded) so re-adopting a tab never double-registers."""
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
        """A stable-enough identity for a child frame (name|url) so a pinned frame
        stays re-findable across a page.frames re-read within the same document."""
        name = ""
        with contextlib.suppress(Exception):
            name = frame.name or ""
        url = ""
        with contextlib.suppress(Exception):
            url = frame.url or ""
        return f"{name}|{url}"

    def _active_frame(self) -> Any:
        """The Playwright Frame element ops act on for the active tab. Defaults to
        the main frame; a switch_frame pin resolves to a child unless it detached/
        navigated away, in which case we fall back to main and clear the stale pin."""
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

    async def open(self, url: str, *, new_tab: bool = False) -> None:
        if new_tab:
            tab = await self._new_page_tab()
            await self._activate(tab.id)
        await self._active().page.goto(url, wait_until="domcontentloaded")

    async def list_tabs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t in self._tabs:
            title = ""
            with contextlib.suppress(Exception):
                title = await t.page.title()
            out.append(
                {"id": t.id, "title": title, "url": t.page.url, "active": t.id == self._active_id}
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
        # Stop the closing tab's screencast on ITS OWN cdp first — while it's still
        # the active tab — so teardown targets the session that was actually
        # streaming. Once it's removed from self._tabs, _active() would fall back to
        # the replacement and _activate would tear the screencast down against the
        # WRONG session, leaking this one's listener + capture.
        if was_active and self._sinks:
            await self._stop_screencast_on(tab)
        self._tabs = [t for t in self._tabs if t.id != tab_id]
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
        await self._active_frame().click(f"[data-cobrowse-ref='{ref}']")

    async def type_text(self, ref: str, text: str) -> None:
        await self._active_frame().fill(f"[data-cobrowse-ref='{ref}']", text)

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
        # scale="css" pins the agent's screenshot to 1 px per CSS px (the live
        # viewport, which a human resize may have changed from the 1280x800 default)
        # regardless of the context's 2x _DEVICE_SCALE. The 2x rendering exists
        # for the HUMAN screencast; without this cap every agent screenshot
        # would carry 4x the pixels into model context — a silent per-step
        # token-cost multiplier on every tenant's browsing, human watching or
        # not. (Playwright's default scale is "device".)
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
        # Wrap so any value (incl. a returned Promise) is JSON-stringified and
        # length-capped INSIDE the page, and a thrown error comes back as {error}
        # instead of raising — a broken/hostile snippet can't crash the tool or
        # dump an un-cappable blob into the agent's context.
        try:
            out = await self._active_frame().evaluate(_EVAL_WRAPPER_JS, js)
        except Exception as exc:  # context destroyed by a navigation, syntax error…
            return {"error": str(exc)[:500]}
        if isinstance(out, dict) and "error" in out:
            return {"error": str(out["error"])[:2000]}
        return {"result": str(out.get("result", "")) if isinstance(out, dict) else str(out)}

    async def nav_state(self) -> dict[str, Any]:
        page = self._active().page
        return {
            "url": page.url,
            "title": await page.title(),
            "can_go_back": False,  # POC: history introspection is a v1 nicety
            "can_go_forward": False,
        }

    # --- screencast (of the active tab) -------------------------------------

    async def add_frame_sink(self, sink: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
        # Start CDP capture + the pump on the FIRST viewer only; a 2nd viewer just
        # joins the sink set (idempotent). Starting per-viewer would stack a
        # duplicate CDP listener and leak an extra pump task per attach.
        # `first` and the add are one atomic step (no await between them) — under
        # asyncio's single loop two concurrent attaches can't both see `first`.
        first = not self._sinks
        self._sinks.add(sink)
        metrics.set_frame_sinks(len(self._sinks))
        if first:
            self._frame_ready = asyncio.Event()
            self._pump_task = asyncio.create_task(self._screencast_pump())
            await self._start_screencast_on(self._active())

    async def _start_screencast_on(self, tab: _Tab) -> None:
        # Register a per-tab listener that captures THIS tab's cdp, so a frame is
        # always acked to the session it came from (see _on_frame). Store it on the
        # tab so _stop_screencast_on removes the exact callable — pyee removal is by
        # identity, and a shared handler couldn't be removed per-tab.
        async def _handler(params: dict[str, Any]) -> None:
            await self._on_frame(tab.cdp, params)

        tab._screencast_handler = _handler
        self._capturing_cdp = tab.cdp
        tab.cdp.on("Page.screencastFrame", _handler)
        await tab.cdp.send(
            # everyNthFrame stays 1 — rate limiting lives in the pump (which
            # always sends the FRESHEST frame), not at capture, so a throttled
            # stream never shows a stale frame. Quality/size trade-offs are
            # documented at _SCREENCAST_QUALITY.
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
        # Ack CDP IMMEDIATELY so it keeps streaming — never gate on the viewer
        # round-trip. Ack to the session the frame CAME FROM (origin_cdp), not
        # whatever tab is active now: a frame from the old tab can still land here
        # after a switch, and acking it to the new tab's session is wrong (F4).
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
        """Forward the freshest frame to EVERY attached viewer, coalescing (drop
        stale frames if a viewer is behind), keeping latency flat. One slow viewer
        can't stall others — each send is independent and its errors are
        swallowed (a dead sink is dropped by remove_frame_sink on disconnect).

        Fan-out is rate-capped at ``_min_frame_interval``: CDP produces up to
        ~60 fps during animation, and shipping them all is pure bandwidth waste
        (see _MIN_FRAME_INTERVAL_S). Frames arriving during the throttle sleep
        overwrite ``_latest_frame``, so the frame sent after the wait is always
        the freshest — the cap trades fps, never latency-to-current-state."""
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
        # Read the computed CSS cursor at the pointer in the TOP frame only — a
        # cross-origin subframe's elementFromPoint is inaccessible and returns
        # null, which we treat as "no reading" (the viewer keeps its default). The
        # snippet is self-contained and swallows its own errors; evaluate() itself
        # is wrapped so a destroyed context (mid-navigation) yields "" too.
        try:
            out = await self._active().page.evaluate(_CURSOR_AT_JS, {"x": x, "y": y})
        except Exception:  # context destroyed by navigation, detached frame, …
            return ""
        return out if isinstance(out, str) else ""

    async def set_viewport(self, width: int, height: int) -> None:
        # Applied to EVERY tab, not just the active one: the viewport is otherwise a
        # context-level default that new tabs inherit, so a resize would silently
        # revert the moment the human/agent opens another tab. Clamped to sane
        # bounds; set_viewport_size keeps the context's device_scale_factor, so the
        # 2x screencast stays crisp. The screencast auto-adapts — CDP frame metadata
        # now reports the new deviceWidth/Height and the viewer re-derives its canvas
        # + input mapping from it (see _on_frame + apps/chat-ui/.../cobrowsePaint.ts). A
        # no-op when unchanged so a debounced resize storm doesn't thrash CDP.
        w = max(_MIN_VIEWPORT_W, min(_MAX_VIEWPORT_W, width))
        h = max(_MIN_VIEWPORT_H, min(_MAX_VIEWPORT_H, height))
        if (w, h) == self._viewport:
            return
        self._viewport = (w, h)
        for tab in self._tabs:
            with contextlib.suppress(Exception):  # a raced-closed tab must not block the rest
                await tab.page.set_viewport_size({"width": w, "height": h})

    async def close(self) -> None:
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
        # Chrome has exited and its writes are on the (durable) profile dir; release
        # the cross-pod flock so another pod may take this profile over.
        self._release_profile_lock()


# Injected into every candidate element as data-cobrowse-ref, then returned as a
# flat list. Password inputs keep their type so redaction can find them.
# An <input> has no textContent and usually no aria-label, so naming it from
# those alone leaves every text field anonymous: a login form came back as two
# indistinguishable `{tag: "input", type: "text"}` entries, and the agent had to
# spend a whole extra browser_read to learn which was the username — measured
# doing exactly that, 3 times per task. So the name falls back through the ways
# HTML actually labels a field.
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
  const sel = 'a,button,input,textarea,select,[role=button],[role=link]';
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

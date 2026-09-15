"""Toolspace sidecar (browser) — co-browsing agent.

TWO planes on ONE port, acting on ONE Chromium session PER CHAT:
  * ``/mcp``      — agent control plane (FastMCP streamable-HTTP), dialed by
                    the in-pod broker.
  * ``/cobrowse`` — human view+input plane (WebSocket), proxied by the
                    dispatcher: live screencast out, clicks/keys in.

The chat id rides each plane (broker-added ``?chat_id=`` read via
:func:`_session_id`; the ``/cobrowse/{session_id}`` path), so a chat's tools
and viewer meet on that chat's OWN browser (isolated cookies/window/tabs).
The agent plane is **chat_id-mandatory** (Pillar C): a call with no chat_id
is rejected, never merged into one ``shared`` profile — that would leak one
chat's cookies/logins into another. The viewer plane keeps
:data:`SHARED_SESSION` only as the dir builders' path-traversal safe-landing.
See docs/plan/20260728T111318Z-cobrowse-per-chat-isolation.md.

The agent surface is the Claude-in-Chrome extension's, verbatim — the same
tool names, argument names and semantics (``computer``, ``read_page``,
``find``, ``browser_batch``, …) plus a few sandbox-only extras (``login``,
``wait_for``, ``download``, frames, dialogs, ``run_recipe``) documented as
"not in the extension" — so a browser skill written against Anthropic's own
surface runs here unchanged. docs/plan/20260914-cobrowse-extension-parity.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import loopwatch
import toollog
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel.server import request_ctx

from src import agent_ops, browser_driver, metrics, profile_gc, recipes
from src.agent_ops import AgentPaused, ToolInputError
from src.browser_driver import BrowserDriver, PlaywrightDriver, StaleRefError, UnknownTabError
from src.cobrowse_ws import CoBrowseConnection
from src.find_model import FindConfig
from src.portal_creds import read_portals
from src.sessions import SessionManager

log = logging.getLogger("workspace-tool-browser")

# REQUIRED, never defaulted (Tier 0.5). Both are declared in the image
# (Dockerfile ENV) so the standalone container binds what it EXPOSEs; in-cluster
# the operator overrides WORKSPACE_TOOL_PORT from the sidecar roster's `port:`.
# A missing value CrashLoops instead of silently binding a guessed port.
HOST = os.environ["WORKSPACE_TOOL_HOST"]
PORT = int(os.environ["WORKSPACE_TOOL_PORT"])
# When the data volume is mounted, the host points this at a dir on it so
# Chromium's profile (cookies/logins) survives a restart. Unset → ephemeral.
PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR") or None
# The host points this at a node-disk emptyDir (NOT the durable volume) so
# Chrome's regenerable disk cache never syncs anywhere. Unset → cache stays
# under the profile (CI/local). DISK_CACHE_SIZE (bytes) caps Chrome's own
# writes. See the driver's _cache_launch_args.
CACHE_DIR = os.environ.get("BROWSER_CACHE_DIR") or None
DISK_CACHE_SIZE = os.environ.get("BROWSER_DISK_CACHE_SIZE") or None
# The model tier of `find` (src/find_model.py): an Anthropic-compatible
# endpoint + key. REQUIRED, with an explicit EMPTY url as the declared sentinel
# for "literal tier only" — absence is not a mode.
FIND_CONFIG = FindConfig(
    url=os.environ["BROWSER_FIND_INFERENCE_URL"].strip(),
    key=os.environ["BROWSER_FIND_INFERENCE_KEY"].strip(),
    model=os.environ["BROWSER_FIND_MODEL"].strip(),
)


def _data_root() -> str:
    """The data volume's mount root — NOT the container's ``$HOME`` (the image
    runs as ``tool`` with ``HOME=/home/tool``; the volume mounts elsewhere).
    Derived from the ``BROWSER_PROFILE_DIR`` anchor; falls back to ``$HOME``
    only with no volume (CI/local, where they coincide). Deriving from
    ``$HOME`` once pointed the file-ops guard AND the profile GC at
    ``/home/tool`` — the GC fail-closed forever
    (docs/incidents/2026-08-11-cobrowse-gc-projects-root-home.md)."""
    if PROFILE_DIR:  # <mount>/.cobrowse/profile → <mount>
        return os.path.dirname(os.path.dirname(PROFILE_DIR.rstrip("/")))
    return os.path.realpath(os.environ["HOME"])


# The data volume mount — file_upload / download / run_recipe / save_to_disk
# only touch files under it, so the agent can't attach arbitrary pod paths
# (e.g. /etc/…) to a web form or write outside the volume.
_DATA_ROOT = _data_root()

# The SDK's chat-transcript root on the shared volume. The profile GC reads chat
# existence straight off here (both pods mount this volume) to decide which
# per-chat profiles are orphaned — no call to the workspace pod. See profile_gc.
PROJECTS_ROOT = os.path.join(_DATA_ROOT, ".claude", "projects")

# Where captured network bodies and saved screenshots land when they are too
# big for the context or the caller asked for a file.
_ARTIFACT_DIR = os.path.join(_DATA_ROOT, ".cobrowse", "artifacts")

# Path-traversal safe-landing id. The agent plane REJECTS chat_id-less calls
# (Pillar C, see _session_id); this remains the viewer plane's default and
# the dir builders' safe landing for a present-but-non-slug id.
SHARED_SESSION = "shared"

# Query param the in-pod broker adds to name the chat
# (apps/workspace/src/mcp_config.py).
_CHAT_ID_PARAM = "chat_id"

# Cap on concurrent Chromiums per pod. REQUIRED: declared in the image
# (Dockerfile ENV) and overridable per-env from the sidecar roster. The idle
# reaper closes walked-away sessions; this bounds the worst case, and the pod's
# memLimit is sized against it — so a silently-guessed cap is an OOM waiting to
# happen, not a harmless default.
_MAX_SESSIONS = int(os.environ["BROWSER_MAX_SESSIONS"])

# Where the built viewer bundle lives. REQUIRED, declared in the image
# (Dockerfile ENV): an explicit empty value disables the root mount — absence is
# not a mode, because "no viewer" and "viewer path forgotten" must not look alike.
VIEWER_DIR = os.environ["BROWSER_VIEWER_DIR"]

# file_upload: total bytes across the paths of one call (the extension's cap).
_UPLOAD_CAP = 10 * 1024 * 1024

# The id arrives from a URL the agent's MCP client sends, and is used
# verbatim as a profile-dir NAME — a hostile ``../../etc`` could escape
# BROWSER_PROFILE_DIR. Only a conservative slug becomes a path.
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def _session_id() -> str:
    """The current chat's session id, from the live MCP request's
    ``?chat_id=`` (via ``request_ctx``, so handlers need no ``Context`` arg).

    **Fail-closed (Pillar C): an HTTP tool CALL with no ``?chat_id=`` is
    rejected** rather than silently sharing one profile across chats (a
    cookie/login leak). Only tool handlers read this — ``initialize`` /
    ``tools/list`` / readiness are chat_id-less broker probes and never
    invoke a handler, so rejecting here never strips the toolset. A no-HTTP
    scope (stdio / unit tests) has nothing to isolate and returns
    :data:`SHARED_SESSION`."""
    try:
        request = request_ctx.get().request
    except LookupError:
        request = None
    if request is None:
        return SHARED_SESSION
    chat_id = getattr(request, "query_params", {}).get(_CHAT_ID_PARAM)
    if not chat_id:
        raise ToolError(
            "This browser is per-chat and needs an active chat context; the "
            "request carried no chat_id."
        )
    return chat_id


def _safe_dir_name(session_id: str) -> str:
    """The session id as a single path segment, else :data:`SHARED_SESSION`.
    ``_SAFE_SESSION_ID``'s char class alone matches ``.`` and ``..`` — which
    as a dir NAME traverse out of the profile root, catastrophic once the GC
    can rm/rename a profile path — so dot-only names and path separators are
    rejected explicitly (B.4)."""
    if session_id in (".", "..") or "/" in session_id or os.sep in session_id:
        return SHARED_SESSION
    return session_id if _SAFE_SESSION_ID.fullmatch(session_id) else SHARED_SESSION


def _profile_dir_for(session_id: str) -> str | None:
    """The per-chat Chromium profile dir under :data:`PROFILE_DIR`; None when
    no volume is mounted (CI). Per-chat subdirs keep persistent contexts from
    fighting over one user-data-dir's Singleton lock."""
    if not PROFILE_DIR:
        return None
    return os.path.join(PROFILE_DIR, _safe_dir_name(session_id))


def _cache_dir_for(session_id: str) -> str | None:
    """This chat's Chrome disk-cache dir under :data:`CACHE_DIR` (node-disk
    emptyDir), or None when unconfigured. Per-session on purpose: concurrent
    Chromes sharing one --disk-cache-dir corrupt it and leak HTTP cache
    across chats. Same slug guard as the profile dir."""
    if not CACHE_DIR:
        return None
    return os.path.join(CACHE_DIR, _safe_dir_name(session_id))


# Opportunistic orphan-profile GC (:mod:`src.profile_gc`), triggered at
# container boot (cold backlog) and session start (mid-uptime deletes) — a
# natural "next use", not a standing timer. Throttled per interval; runs in a
# worker thread so it never blocks a launch.
_GC_MIN_INTERVAL_S = 600.0
_gc_lock = asyncio.Lock()
_gc_last_ts = 0.0
_bg_tasks: set[asyncio.Task[Any]] = set()


async def _sweep_profiles_bg(force: bool = False) -> None:
    """Profile GC in a worker thread. Throttled unless ``force`` (boot);
    ``_gc_lock`` collapses overlapping triggers. ``run_sweep`` never raises."""
    global _gc_last_ts
    if not PROFILE_DIR:
        return
    if not force and time.monotonic() - _gc_last_ts < _GC_MIN_INTERVAL_S:
        return
    if _gc_lock.locked():
        return
    async with _gc_lock:
        _gc_last_ts = time.monotonic()
        await asyncio.to_thread(profile_gc.run_sweep, PROFILE_DIR, PROJECTS_ROOT)


def _schedule_profile_gc(force: bool = False) -> None:
    """Fire a throttled GC sweep without awaiting — a launch/boot must not
    wait on filesystem work. The task ref keeps it from being GC'd mid-flight."""
    task = asyncio.create_task(_sweep_profiles_bg(force=force))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _playwright_factory(session_id: str) -> BrowserDriver:
    """Mint a started real driver for one chat's session (own profile + cache
    subdirs). Injected into the manager in prod; tests pass a fake factory."""
    driver = PlaywrightDriver(
        profile_dir=_profile_dir_for(session_id),
        cache_dir=_cache_dir_for(session_id),
        disk_cache_size=DISK_CACHE_SIZE,
    )
    await driver.start()
    log.info("cobrowse session started session=%s", session_id)
    _schedule_profile_gc()
    return driver


manager = SessionManager(_playwright_factory, max_sessions=_MAX_SESSIONS)

# How often the background reaper sweeps for idle, viewer-less sessions to close.
_REAP_INTERVAL_S = 60.0


async def _reap_idle_loop() -> None:
    """Close idle, viewer-less sessions on a timer (walked-away Chromiums).
    A reap error is logged, not fatal — the loop must outlive one bad sweep."""
    while True:
        await asyncio.sleep(_REAP_INTERVAL_S)
        try:
            closed = await manager.reap_idle()
            if closed:
                log.info("cobrowse reaped %d idle session(s): %s", len(closed), ", ".join(closed))
        except Exception:
            log.exception("cobrowse reaper loop error")


mcp = FastMCP("browser", host=HOST, port=PORT, lifespan=loopwatch.lifespan)

# The liveness target. Answered by the loop above, so silence means wedged —
# see loopwatch.serve_health.
loopwatch.serve_health(mcp)


# --- error mapping: domain errors become tool errors the model can act on ------

_DOMAIN_ERRORS = (AgentPaused, UnknownTabError, StaleRefError, ToolInputError, recipes.RecipeError)


async def _run(coro: Awaitable[Any]) -> Any:
    """Await an agent op; a domain error becomes a ToolError (an error result
    with its message, not a crash), so the model reads the corrective call."""
    try:
        return await coro
    except _DOMAIN_ERRORS as exc:
        raise ToolError(str(exc)) from exc


def _resolve_data_path(path: str) -> str | None:
    """Resolve ``path`` under the data volume, or None if it escapes it — so the
    agent can't read/write arbitrary pod paths (``/etc/…``) via upload/download."""
    base = path if os.path.isabs(path) else os.path.join(_DATA_ROOT, path)
    resolved = os.path.realpath(base)
    if resolved != _DATA_ROOT and not resolved.startswith(_DATA_ROOT + os.sep):
        return None
    return resolved


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _ensure_parent(dest: str) -> None:
    os.makedirs(os.path.dirname(dest) or _DATA_ROOT, exist_ok=True)


def _write_bytes(dest: str, data: bytes) -> None:
    _ensure_parent(dest)
    with open(dest, "wb") as f:
        f.write(data)


def _write_text(dest: str, text: str) -> None:
    _ensure_parent(dest)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(text)


def _artifact_path(kind: str, ext: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(
        _ARTIFACT_DIR, f"{kind}-{stamp}-{os.getpid()}-{int(time.monotonic() * 1000) % 100000}.{ext}"
    )


def _image_result(out: dict[str, Any]) -> Any:
    """A `computer` result: text, plus the PNG as an image block when there is one."""
    png = out.get("png")
    if png is None:
        return out["text"]
    return [out["text"], Image(data=png, format="png")]


# --- tabs ---------------------------------------------------------------------------


@mcp.tool()
async def tabs_context_mcp(createIfEmpty: bool = False) -> dict[str, Any]:
    """List the browser's open tabs: ``{tabs: [{tabId, url, title, active, loaded}]}``.

    Every other tool names its tab by ``tabId`` — call this first in a chat to
    learn them. The browser runs server-side; the user watches it live in their
    viewer and can take over at any time. ``loaded`` is False for a tab restored
    from a previous session that has not been opened yet (its URL is real, the
    page loads when you first act on it). ``createIfEmpty`` is accepted for
    compatibility: a session always has at least one tab.
    """
    return await _run(agent_ops.tabs_context(manager, _session_id()))


@mcp.tool()
async def tabs_create_mcp() -> dict[str, Any]:
    """Open a new blank tab and make it active; returns ``{tabId, url}``. Close it
    with ``tabs_close_mcp`` when done — every open tab is a live page holding
    memory, and the human inherits whatever you leave behind.
    """
    return await _run(agent_ops.tabs_create(manager, _session_id()))


@mcp.tool()
async def tabs_close_mcp(tabId: int) -> dict[str, Any]:
    """Close tab ``tabId``. The browser never drops to zero tabs."""
    return await _run(agent_ops.tabs_close(manager, _session_id(), tabId))


# --- navigation ------------------------------------------------------------------------


@mcp.tool()
async def navigate(url: str, tabId: int, reason: str = "") -> dict[str, Any]:
    """Navigate tab ``tabId`` to ``url`` (``"back"`` / ``"forward"`` walk history).

    **Not your default way to reach the web.** This spins up a real browser the
    user watches live — heavier and slower than it needs to be for anything that
    doesn't require interactive, multi-step work (forms, logins, portals). Before
    reaching for it: for "look something up" / "read this page" prefer
    WebSearch/WebFetch; if a purpose-built tool exists for the data (a connector,
    an API tool) prefer it. This browser cannot reach every site — it runs from
    the platform's own network, and some sites block or are unreachable from it;
    a page that hangs blank is often this, not a slow network — say so instead
    of retrying.

    **The FIRST call each chat is refused unless it carries ``reason``** — one
    short phrase on why this can't be WebSearch/WebFetch (a login, a form, a
    multi-step portal, "the user asked for the browser"). Ignored after that.

    Returns ``{tabId, url, title, loaded, page_state, changes}``. A ``page_state``
    other than ``"ok"`` means the site served a WALL, not content:
    ``blocked_challenge`` (anti-bot/CAPTCHA), ``blocked_denied`` (401/403),
    ``rate_limited`` (429), ``server_error``, or ``unknown``. Retrying a blocked
    page will not change it — tell the user and suggest they take over in the
    live view, or use another source.
    """
    return await _run(agent_ops.navigate(manager, _session_id(), url, tabId))


# --- reading the page ------------------------------------------------------------------


@mcp.tool()
async def read_page(
    tabId: int,
    filter: str = "interactive",
    depth: int | None = None,
    max_chars: int = agent_ops.READ_PAGE_MAX_CHARS,
    ref_id: str | None = None,
    boxes: bool = False,
) -> str:
    """Read tab ``tabId`` as its accessibility tree: one line per node,
    ``- role "name" [ref=e12]``, indented by nesting.

    The ``ref`` is what you pass to ``computer`` (click/type/hover/scroll_to),
    ``form_input``, ``file_upload`` and ``download``. Refs are stable while the
    page stays the same document — re-reading, typing and scrolling keep them —
    and die with a navigation, when a tool tells you the ref is from a previous
    page: read again. Password values are never shown.

    Args:
        tabId: The tab to read.
        filter: ``"interactive"`` (default) — only nodes with a ref, the things you
            can act on; ``"all"`` — the whole tree including text, for reading.
        depth: Limit nesting depth (a cheap overview of a huge page).
        max_chars: Cap on the reply (default 50000); cut at a line boundary with
            the full size stated.
        ref_id: Read only the subtree under this ref (from an earlier read).
        boxes: Add ``[box=x,y,w,h]`` coordinates to each node, for coordinate clicks
            or a ``zoom`` region — without a screenshot.
    """
    if filter not in ("interactive", "all"):
        raise ToolError('filter must be "interactive" or "all"')
    return await _run(
        agent_ops.read_page(
            manager,
            _session_id(),
            tabId,
            filter=filter,
            depth=depth,
            max_chars=int(max_chars),
            ref_id=ref_id,
            boxes=boxes,
        )
    )


@mcp.tool()
async def get_page_text(tabId: int) -> str:
    """The readable text of tab ``tabId`` — the main content (article/main first),
    with a ``Title / URL / Source element`` header. Use it to actually READ what a
    page says; ``read_page`` is for what you can click. Capped at 50000 chars at a
    line boundary, the full size stated when cut.
    """
    return await _run(agent_ops.get_page_text(manager, _session_id(), tabId))


@mcp.tool()
async def find(tabId: int, query: str) -> str:
    """Find elements on tab ``tabId`` by description — "the search box in the
    header", "add to cart", "the price".

    A literal/regex match over the page's tree runs first (free); on a miss a small
    model reads the tree and names the refs, each with a one-line reason. Up to
    20 hits as ``ref: role "name"`` lines, tagged ``source: literal`` or
    ``source: model``; the refs are ready to use with ``computer``. Cheaper than
    reading a large page yourself.
    """
    return await _run(agent_ops.find(manager, _session_id(), tabId, query, config=FIND_CONFIG))


# --- acting on the page --------------------------------------------------------------------


@mcp.tool()
async def computer(
    action: str,
    tabId: int,
    coordinate: list[int] | None = None,
    ref: str | None = None,
    text: str | None = None,
    scale: float | None = None,
    region: list[int] | None = None,
    scroll_direction: str | None = None,
    scroll_amount: int | None = None,
    modifiers: list[str] | None = None,
    repeat: int | None = None,
    duration: float | None = None,
    start_coordinate: list[int] | None = None,
    save_to_disk: bool = False,
) -> Any:
    """Act on tab ``tabId`` — every pointer and keyboard action in one tool.

    Actions: ``left_click`` / ``right_click`` / ``double_click`` / ``triple_click``
    (by ``ref`` from read_page/find, or by ``coordinate`` [x, y] in page CSS px;
    ``modifiers`` like ["ctrl"]), ``type`` (``text`` typed as keystrokes into the
    focused element, or into ``ref`` — for form fields prefer ``form_input``),
    ``key`` (``text`` is a key or combo: "Return", "ctrl+a", "shift+Tab";
    ``repeat``), ``scroll`` (``scroll_direction`` up/down/left/right,
    ``scroll_amount`` in ~100 px clicks, default 5; at ``coordinate`` or ``ref``),
    ``scroll_to`` (bring ``ref`` into view), ``hover`` (``ref`` or ``coordinate``),
    ``left_click_drag`` (``start_coordinate`` → ``coordinate``), ``wait``
    (``duration`` seconds, max 10), ``screenshot`` (the page as an image;
    ``scale`` 0.1-2 shrinks/enlarges, ``region`` [x0, y0, x1, y1] crops; the reply
    states the image-to-page coordinate mapping) and ``zoom`` (``region``
    required, at 2x detail by default). ``save_to_disk`` also writes a
    screenshot/zoom to the workspace and returns the path.

    Clicks, typing, keys, drags, hover, scroll and scroll_to act on the page and
    are approved by the user in chat before they run; screenshot, zoom and wait are
    not. A mutating action replies with what it did plus ONLY what changed — a
    navigation, a new tab, a dialog — never the page: chain it with ``read_page``
    in one ``browser_batch`` when you want the page back.
    """
    out = await _run(
        agent_ops.computer(
            manager,
            _session_id(),
            tabId,
            action,
            coordinate=coordinate,
            ref=ref,
            text=text,
            scale=scale,
            region=region,
            scroll_direction=scroll_direction,
            scroll_amount=scroll_amount,
            modifiers=modifiers,
            repeat=repeat,
            duration=duration,
            start_coordinate=start_coordinate,
        )
    )
    if save_to_disk and out.get("png") is not None:
        dest = _artifact_path("screenshot", "png")
        await asyncio.to_thread(_write_bytes, dest, out["png"])
        out["text"] = f"{out['text']}\nSaved to {dest}"
    return _image_result(out)


@mcp.tool()
async def form_input(tabId: int, ref: str, value: str | bool | float) -> str:
    """Set a form field's value on tab ``tabId``: fill a text field (replacing its
    content), choose a ``<select>`` option by label or value, or check/uncheck a
    box with a boolean. Prefer this over ``computer`` ``type`` for forms. Approved
    in chat. The reply notes when the page reformatted or restricted the value,
    or the field is an autocomplete (read_page and click the suggestion instead
    of pressing Enter).
    """
    return await _run(agent_ops.form_input(manager, _session_id(), tabId, ref, value))


@mcp.tool()
async def javascript_tool(tabId: int, text: str, action: str = "javascript_exec") -> dict[str, Any]:
    """Run JavaScript ``text`` in tab ``tabId`` and return ``{result}`` (stringified,
    a returned Promise awaited) or ``{error}``. Use it when the structured tools
    can't answer (a computed value, a count, text a read misses). It runs IN the
    page with the page's privileges, so the user approves it in chat.
    """
    if action != "javascript_exec":
        raise ToolError('action must be "javascript_exec"')
    return await _run(agent_ops.javascript(manager, _session_id(), tabId, text))


@mcp.tool()
async def file_upload(tabId: int, ref: str, paths: list[str]) -> dict[str, Any]:
    """Attach workspace files to the file ``<input>`` at ``ref`` on tab ``tabId`` —
    e.g. a document for a tender. Paths are under the workspace only, 10 MB in
    total. Approved in chat.
    """
    resolved: list[str] = []
    total = 0
    for path in paths:
        dest = _resolve_data_path(path)
        if dest is None:
            return {"status": "path_not_allowed", "path": path}
        if not await asyncio.to_thread(os.path.isfile, dest):
            return {"status": "not_found", "path": path}
        total += await asyncio.to_thread(os.path.getsize, dest)
        resolved.append(dest)
    if not resolved:
        raise ToolError("file_upload needs at least one path")
    if total > _UPLOAD_CAP:
        return {"status": "too_large", "bytes": total, "limit": _UPLOAD_CAP}
    return await _run(agent_ops.upload(manager, _session_id(), tabId, ref, resolved))


@mcp.tool()
async def resize_window(tabId: int, width: int, height: int) -> dict[str, Any]:
    """Resize the browser viewport (CSS px) — e.g. wider for a table, narrower to
    see a mobile layout. Applies to every tab.
    """
    return await _run(agent_ops.resize(manager, _session_id(), tabId, width, height))


# --- observability ----------------------------------------------------------------------------


@mcp.tool()
async def read_console_messages(
    tabId: int,
    pattern: str | None = None,
    onlyErrors: bool = False,
    limit: int = 100,
    clear: bool = False,
) -> list[dict[str, Any]]:
    """Recent console messages of tab ``tabId`` as ``[{type, text}]`` (uncaught
    errors included as type "error"). Diagnose an action that seemed to do
    nothing — a validation error or exception usually logs here. ``pattern`` is a
    regex filter; ``onlyErrors`` keeps errors/warnings; ``clear`` empties the
    buffer after reading.
    """
    return await _run(
        agent_ops.console_messages(
            manager,
            _session_id(),
            tabId,
            pattern=pattern,
            only_errors=onlyErrors,
            limit=int(limit),
            clear=clear,
        )
    )


@mcp.tool()
async def read_network_requests(
    tabId: int,
    urlPattern: str | None = None,
    limit: int = 100,
    clear: bool = False,
) -> list[dict[str, Any]]:
    """Recent network requests of tab ``tabId`` as ``[{index, method, url, status,
    resource_type, size}]`` — no headers, no bodies. ``status`` 0 = a request that
    never completed (blocked/aborted) — a strong signal a submit silently failed.
    ``urlPattern`` is a regex filter. Pass an ``index`` to ``get_network_request``
    for headers or a body.
    """
    return await _run(
        agent_ops.network_requests(
            manager, _session_id(), tabId, url_pattern=urlPattern, limit=int(limit), clear=clear
        )
    )


@mcp.tool()
async def get_network_request(
    tabId: int,
    index: int,
    reason: str,
    part: str | None = None,
    path: str | None = None,
    raw_headers: bool = False,
) -> dict[str, Any]:
    """One request from ``read_network_requests`` in full: headers (cookie and
    authorization redacted unless ``raw_headers``), and with ``part`` =
    ``"response_body"`` or ``"request_body"`` the body — inline up to 10000 chars,
    written to a workspace file above that (or to ``path`` when given).

    Reading a body shows data the page may not render (a price behind a spinner,
    an API payload) — that is why ``reason`` is required and every body read is
    logged, and why the result is tagged ``source: "network"``: tell the user the
    value came from the network, not the page. Bodies are captured for
    xhr/fetch/document responses with a text content type, up to 256 KB.
    """
    if not (reason or "").strip():
        raise ToolError("get_network_request needs a stated reason")
    dest: str | None = None
    if path:
        dest = _resolve_data_path(path)
        if dest is None:
            return {"status": "path_not_allowed", "path": path}

    async def write_file(text: str) -> str:
        target = dest or _artifact_path(f"network-{tabId}-{index}", "txt")
        await asyncio.to_thread(_write_text, target, text)
        return target

    return await _run(
        agent_ops.network_request(
            manager,
            _session_id(),
            tabId,
            index,
            part=part,
            raw_headers=raw_headers,
            reason=reason,
            write_file=write_file,
        )
    )


# --- extras (not in the extension) ------------------------------------------------------


@mcp.tool()
async def login(portal_id: str, ref: str | None = None, tabId: int | None = None) -> dict[str, Any]:
    """Log in to a portal the user has configured for this workspace. Not in the
    extension.

    You pass ONLY the portal id (e.g. "acme-portal"); the username and password
    are stored securely and injected server-side — they are never shown to you,
    and every later result is scrubbed of them. Then ``read_page`` to see whether
    login succeeded or a challenge (MFA/CAPTCHA) needs the user — if it does,
    say so and END YOUR TURN; the user acts in the live view and your next
    message resumes.

    Two ways to use it: **no ``ref``** — goes to the portal's saved login URL and
    fills the form there (try this first); **with ``ref``** (the username/email
    field from read_page on the page you are already on) — fills that form
    without navigating, for sites that keep the form behind a menu or a separate
    sign-in provider. A ``no_login_form`` answer is not a dead end — find the
    real form and call again with its ref. Never ask the user for the password.

    Returns ``{status, portal_id, url, ...}`` with status ``submitted``,
    ``unknown_portal``, ``no_stored_password`` (ask the user to add one in their
    settings; nothing was typed) or ``no_login_form`` (``url`` and ``tried`` say
    where it looked).
    """
    portals = await asyncio.to_thread(read_portals)
    return await _run(
        agent_ops.login(manager, _session_id(), portal_id, portals, ref=ref or None, tab_id=tabId)
    )


@mcp.tool()
async def wait_for(
    tabId: int,
    text: str | None = None,
    selector: str | None = None,
    url: str | None = None,
    response: str | None = None,
    timeout_ms: int = 8000,
) -> dict[str, Any]:
    """Wait until tab ``tabId`` is ready before reading/acting: for ``text`` to
    appear, a CSS ``selector`` to match, the page ``url`` to contain a string
    (after a submit), or a ``response`` whose URL contains a string (an async
    save landing); with none, for the network to go idle. Returns ``{ready}``
    (False on timeout). Not in the extension.
    """
    return await _run(
        agent_ops.wait_for(
            manager,
            _session_id(),
            tabId,
            text=text,
            selector=selector,
            url=url,
            response=response,
            timeout_ms=int(timeout_ms),
        )
    )


@mcp.tool()
async def download(tabId: int, ref: str, path: str) -> dict[str, Any]:
    """Click the download link/button at ``ref`` on tab ``tabId`` and SAVE the file
    into the workspace at ``path`` (e.g. "downloads/tender.pdf") so the user can
    open it. Approved in chat. Not in the extension.
    """
    dest = _resolve_data_path(path)
    if dest is None:
        return {"status": "path_not_allowed", "path": path}
    await asyncio.to_thread(_ensure_parent, dest)
    result = await _run(agent_ops.download(manager, _session_id(), tabId, ref, dest))
    return {"status": "downloaded", "path": dest, **result}


@mcp.tool()
async def list_frames(tabId: int) -> list[dict[str, Any]]:
    """The frames (iframes) of tab ``tabId`` as ``[{index, name, url}]``; 0 is the
    top page, 1+ are embedded frames — portals often put a login form or a PDF
    viewer inside one. Then ``switch_frame`` to act inside it. Not in the extension.
    """
    return await _run(agent_ops.list_frames(manager, _session_id(), tabId))


@mcp.tool()
async def switch_frame(tabId: int, frame: str = "") -> dict[str, Any]:
    """Choose which frame of tab ``tabId`` ``read_page`` / ``computer`` /
    ``form_input`` act inside — a frame ``index`` or ``name`` from ``list_frames``;
    empty, "main" or "0" resets to the top page. Returns ``{status, target}``:
    ``switched`` / ``reset`` / ``unknown_frame``. Not in the extension.
    """
    return await _run(agent_ops.switch_frame(manager, _session_id(), tabId, frame))


@mcp.tool()
async def set_dialog_mode(mode: str) -> dict[str, Any]:
    """Choose how the browser answers native pop-ups (alert / confirm / prompt /
    "leave this page?") that would otherwise freeze the page: ``"dismiss"``
    (default — Cancel/stay; a confirm never commits) or ``"accept"`` (OK/leave —
    use ONLY right before an action you KNOW raises a benign confirm, then set it
    back). Accepting can approve a delete or an order, so the user approves this
    in chat. Not in the extension.
    """
    return await _run(agent_ops.set_dialog_mode(manager, _session_id(), mode))


@mcp.tool()
async def last_dialog(tabId: int) -> dict[str, Any]:
    """The most recent native pop-up the browser auto-handled: ``{status: "none"}``
    or ``{status: "handled", type, message, action, ...}`` — ``action`` is how it
    was answered; a dismissed confirm means the action did NOT go through. Not in
    the extension.
    """
    return await _run(agent_ops.last_dialog(manager, _session_id(), tabId))


@mcp.tool()
async def run_recipe(
    path: str, params: dict[str, str] | None = None, tabId: int | None = None
) -> dict[str, Any]:
    """Run a SAVED click-path in one call — prefer this over driving the browser
    step by step whenever a recipe exists for the task. Not in the extension.

    A recipe is a proven sequence stored as JSON in the workspace: a list of
    ``{name, input}`` steps exactly like ``browser_batch`` items (``navigate``,
    ``computer``, ``read_page``, ``find``, ``form_input``, ``wait_for``, ``login``),
    where a step names its control by ``target: {role, name, nth}`` instead of a
    ref. Running it costs ONE tool call with no model between the steps.

    Args:
        path: Workspace path to the recipe JSON (e.g. "skills/supplier-portal.recipe.json").
        params: Values for the recipe's declared parameters, e.g. ``{"keyword": "pumps"}``.
        tabId: The tab to run on (default: the active tab).

    Returns ``{status, steps_run, extracted}`` on success — ``extracted`` holds
    the page content the recipe collected. On ``step_failed`` it names the step
    index and why, which usually means the site changed: fall back to driving the
    browser yourself from that point, and tell the user the recipe needs
    re-recording.
    """
    resolved = _resolve_data_path(path)
    if resolved is None:
        return {"status": "path_not_allowed", "path": path}
    if not await asyncio.to_thread(os.path.isfile, resolved):
        return {"status": "not_found", "path": path}
    try:
        raw = json.loads(await asyncio.to_thread(_read_text, resolved))
        recipe = recipes.parse(raw)
    except json.JSONDecodeError as exc:
        return {"status": "invalid_recipe", "reason": f"not valid JSON: {exc}"}
    except recipes.RecipeError as exc:
        return {"status": "invalid_recipe", "reason": str(exc)}
    session_id = _session_id()
    if tabId is None:
        session = await manager.get_or_create(session_id)
        tabId = session.driver.active_num()
    return await agent_ops.run_recipe(recipe, params or {}, tab_id=int(tabId), dispatch=_DISPATCH)


# --- batch: several calls behind ONE round trip ------------------------------------------


@mcp.tool()
async def browser_batch(actions: list[dict[str, Any]]) -> Any:
    """Run several browser tool calls in ONE round trip: ``actions`` is a list of
    ``{name, input}`` (any tool here except browser_batch itself), executed in
    order, stopping at the first error. The reply lists every item's result in
    order, screenshots interleaved. Each item carries the SAME approval it would
    standalone, so a batch with a click prompts the user once for the whole set.

    This is the cheap way to act and look: ``[{computer left_click e5},
    {read_page}]`` clicks and returns the new page in one call.
    """
    if not isinstance(actions, list) or not actions:
        raise ToolError("browser_batch needs a non-empty actions list of {name, input}")
    results = await agent_ops.run_batch(actions, _DISPATCH)
    content: list[Any] = []
    for i, item in enumerate(results, 1):
        if item["status"] != "ok":
            error = str(item["error"]).removeprefix("ToolError: ")
            content.append(f"#{i} {item['name']}: ERROR — {error}")
            break
        out = item["result"]
        if isinstance(out, list) and any(isinstance(x, Image) for x in out):
            texts = [x for x in out if isinstance(x, str)]
            content.append(f"#{i} {item['name']}: " + "\n".join(texts))
            content.extend(x for x in out if isinstance(x, Image))
        elif isinstance(out, str):
            content.append(f"#{i} {item['name']}:\n{out}")
        else:
            content.append(f"#{i} {item['name']}: {json.dumps(out, ensure_ascii=False)}")
    return content


# The handlers a batch item (and a recipe step) can name, by bare tool name.
# Explicit, not `getattr(module, name)`: the allowlist in recipes.py is only a
# real boundary if nothing here can reach a function it does not name.
_DISPATCH: dict[str, Callable[..., Awaitable[Any]]] = {
    "tabs_context_mcp": tabs_context_mcp,
    "tabs_create_mcp": tabs_create_mcp,
    "tabs_close_mcp": tabs_close_mcp,
    "navigate": navigate,
    "read_page": read_page,
    "get_page_text": get_page_text,
    "find": find,
    "computer": computer,
    "form_input": form_input,
    "javascript_tool": javascript_tool,
    "file_upload": file_upload,
    "resize_window": resize_window,
    "read_console_messages": read_console_messages,
    "read_network_requests": read_network_requests,
    "get_network_request": get_network_request,
    "login": login,
    "wait_for": wait_for,
    "download": download,
    "list_frames": list_frames,
    "switch_frame": switch_frame,
    "set_dialog_mode": set_dialog_mode,
    "last_dialog": last_dialog,
    "run_recipe": run_recipe,
}


def build_app() -> Any:
    """Starlette app serving BOTH ``/mcp`` (FastMCP) and ``/cobrowse`` (WS).

    We take FastMCP's own streamable-HTTP app (so its session-manager lifespan is
    preserved) and add the co-browse WebSocket route onto it — one app, one port.
    """
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Mount, Route, WebSocketRoute
    from starlette.websockets import WebSocket

    app = mcp.streamable_http_app()

    async def cobrowse_endpoint(ws: WebSocket) -> None:
        # The path id is the chat id — the viewer meets the agent on THAT
        # chat's session. Missing/blank → the shared fallback, matching the
        # agent plane so the two never split.
        session_id = ws.path_params.get("session_id") or SHARED_SESSION
        # ?control=0 = watch-only viewer; ?control=1 or absent = driver, so an
        # older dispatcher that doesn't send it behaves as before.
        can_drive = ws.query_params.get("control") != "0"
        await ws.accept()
        metrics.inc_viewer_connection()
        log.info("cobrowse viewer connected session=%s can_drive=%s", session_id, can_drive)
        adapter = _StarletteWsAdapter(ws)
        conn = CoBrowseConnection(manager, session_id, adapter, can_drive=can_drive)
        try:
            await conn.run()
        except Exception:
            metrics.inc_session_error()
            log.exception("cobrowse connection error session=%s", session_id)
        finally:
            log.info("cobrowse viewer disconnected session=%s", session_id)
            with contextlib.suppress(Exception):
                await ws.close()

    async def metrics_endpoint(_req: Request) -> Response:
        body, content_type = metrics.render()
        return Response(content=body, media_type=content_type)

    # Append rather than app.add_websocket_route(...) — not typed on this
    # Starlette version; the router's route list is the stable seam.
    app.router.routes.append(WebSocketRoute("/cobrowse/{session_id}", cobrowse_endpoint))
    app.router.routes.append(Route("/metrics", metrics_endpoint, methods=["GET"]))

    # Bundled viewer, mounted LAST and at the root so it can never shadow
    # /mcp, /cobrowse or /metrics (Starlette matches in order). Absence is
    # fine — the platform serves its own React viewer.
    if VIEWER_DIR and os.path.isdir(VIEWER_DIR):
        from starlette.staticfiles import StaticFiles

        app.router.routes.append(
            Mount("/", app=StaticFiles(directory=VIEWER_DIR, html=True), name="viewer")
        )
        log.info("serving bundled co-browse viewer from %s", VIEWER_DIR)

    # Run the idle-session reaper alongside FastMCP's own lifespan — wrap,
    # don't replace, so both run.
    mcp_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _lifespan_with_reaper(app_: Any) -> Any:
        async with mcp_lifespan(app_):
            # Boot hygiene: clear stale Singleton* left by hard-killed pods
            # (only an in-Chrome quit unlinks them) so the strict S3 sync
            # runs clean. No Chrome runs in THIS pod yet, and another pod's
            # live profiles are skipped via the per-profile flock.
            # Best-effort; must never block serving.
            if PROFILE_DIR:
                with contextlib.suppress(Exception):
                    swept = browser_driver.clear_stale_profile_locks(PROFILE_DIR)
                    if swept:
                        log.info("boot sweep cleared stale browser locks in %d profiles", swept)
                # Cold-backlog orphan reclaim — forced (bypass throttle),
                # threaded so a large backlog never delays readiness.
                _schedule_profile_gc(force=True)
            reaper = asyncio.create_task(_reap_idle_loop())
            try:
                yield
            finally:
                reaper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reaper
                # Graceful shutdown: context.close() is an IN-CHROME quit —
                # the only exit that unlinks Singleton* locks — so a drain
                # leaves every profile lock-free on the volume. Best-effort,
                # bounded by terminationGracePeriod.
                with contextlib.suppress(Exception):
                    await manager.close_all()

    app.router.lifespan_context = _lifespan_with_reaper
    return app


class _StarletteWsAdapter:
    """Adapts a Starlette ``WebSocket`` to the ``send_json``/``receive_json``
    surface :class:`CoBrowseConnection` expects; ``None`` on close so the recv
    loop exits cleanly.

    "Socket gone" is one fact (:attr:`_closed`) set by whichever concurrent
    side discovers it first, because Starlette surfaces a disconnect
    asymmetrically: a failed send raises ``WebSocketDisconnect``, after which
    the recv guard raises a *bare* ``RuntimeError`` ('WebSocket is not
    connected'). Catching only the former let the RuntimeError kill the whole
    handler — inflating ``cobrowse_session_errors_total`` on ordinary
    session-ends. Both directions treat either exception as a clean close."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._closed = False

    async def send_json(self, data: dict[str, Any]) -> None:
        # Best-effort: a failed push propagating would tear down the WHOLE
        # handler, dropping the human's next input until they reconnect.
        if self._closed:
            return
        from starlette.websockets import WebSocketDisconnect

        try:
            await self._ws.send_json(data)
        except (WebSocketDisconnect, RuntimeError):
            self._closed = True
            metrics.inc_send_drop()
            log.debug("cobrowse: viewer socket gone on send; marking closed")

    async def receive_json(self) -> dict[str, Any] | None:
        # A send-side disconnect makes the receive guard raise a bare
        # RuntimeError (see class docstring) — both are a clean close: return
        # None so the error-counting except-branch never runs.
        if self._closed:
            return None
        from starlette.websockets import WebSocketDisconnect

        try:
            return await self._ws.receive_json()
        except (WebSocketDisconnect, RuntimeError):
            self._closed = True
            metrics.inc_recv_close()
            log.debug("cobrowse: viewer socket gone on receive; ending loop cleanly")
            return None


def main() -> None:
    """Run both planes over uvicorn. Blocks; container entrypoint."""
    import uvicorn

    toollog.configure("browser")
    log.info("workspace-tool-browser on %s:%d — /mcp (agent) + /cobrowse (human)", HOST, PORT)
    uvicorn.run(build_app(), host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()

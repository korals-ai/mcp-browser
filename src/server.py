"""Toolspace sidecar (browser) — co-browsing agent, POC v0.

Serves TWO planes on ONE port:

  * ``/mcp``      — the agent control plane (FastMCP streamable-HTTP). The in-pod
                    broker dials this; the agent gets ``browser_open`` /
                    ``browser_snapshot`` / ``browser_click`` / ``browser_type``.
  * ``/cobrowse`` — the human view+input plane (WebSocket). The dispatcher
                    proxies the browser tab here; it streams the live screencast
                    and forwards the human's clicks/keys.

Both planes act on ONE Chromium session PER CHAT. The chat id rides in on each
plane — a ``?chat_id=`` query param the in-pod broker adds to the agent's MCP
requests (read here via :func:`_session_id`), and the ``/cobrowse/{session_id}``
path the dispatcher proxies — so the agent's tools and the human's viewer for a
given chat meet on that chat's OWN browser (isolated cookies/window/tabs). The
agent plane is **chat_id-mandatory** (Pillar C): a tool call carrying no
``?chat_id=`` is rejected — never silently merged into one ``shared`` profile,
which would leak one chat's cookies/logins into another. The ``/cobrowse``
viewer plane keeps :data:`SHARED_SESSION` only as a path-traversal safe-landing
in the dir builders. See docs/plan/20260728T111318Z-cobrowse-per-chat-isolation.md
and docs/plan/20260810T182738Z-cobrowse-profile-footprint.md (Pillar C).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from typing import Any

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel.server import request_ctx

from src import agent_ops, browser_driver, metrics, profile_gc
from src.browser_driver import BrowserDriver, PlaywrightDriver
from src.cobrowse_ws import CoBrowseConnection
from src.portal_creds import read_portals
from src.sessions import SessionManager

log = logging.getLogger("workspace-tool-browser")

HOST = os.environ.get("WORKSPACE_TOOL_HOST", "0.0.0.0")  # noqa: S104 - pod-local
PORT = int(os.environ.get("WORKSPACE_TOOL_PORT", "8096"))
# When the tenant volume is mounted, the operator points this at a dir on it so
# Chromium's profile (cookies/logins) survives a pod restart. Unset → ephemeral.
PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR") or None
# The operator points this at a node-disk emptyDir (NOT the PVC) so Chrome's
# regenerable disk cache never syncs to S3. Unset → cache stays under the profile
# (CI/local). DISK_CACHE_SIZE (bytes) caps Chrome's own writes. See the driver's
# _cache_launch_args + docs/plan/20260810T182738Z-cobrowse-profile-footprint.md.
CACHE_DIR = os.environ.get("BROWSER_CACHE_DIR") or None
DISK_CACHE_SIZE = os.environ.get("BROWSER_DISK_CACHE_SIZE") or None


def _tenant_volume_root() -> str:
    """The tenant PVC mount root. CRITICAL: this is NOT the container's ``$HOME``.
    The browser image runs as user ``tool`` with ``HOME=/home/tool`` (Chrome's
    home), while the operator mounts the tenant volume at a FIXED path
    (``/home/agent``) and anchors ``BROWSER_PROFILE_DIR`` (``…/.cobrowse/profile``)
    there. Derive the mount from that anchor; fall back to ``$HOME`` only when no
    volume is mounted (CI/local, where they coincide). Deriving from ``$HOME``
    instead pointed both the file-ops guard AND the profile GC at ``/home/tool`` —
    the GC's keep-set read empty and it fail-closed forever
    (docs/incidents/2026-08-11-cobrowse-gc-projects-root-home.md)."""
    if PROFILE_DIR:  # <mount>/.cobrowse/profile → <mount>
        return os.path.dirname(os.path.dirname(PROFILE_DIR.rstrip("/")))
    return os.path.realpath(os.environ.get("HOME", "/home/agent"))


# The tenant volume mount — browser_upload_file only accepts files under it, so
# the agent can't attach arbitrary pod paths (e.g. /etc/…) to a web form.
_AGENT_HOME = _tenant_volume_root()

# The SDK's chat-transcript root on the shared PVC. The profile GC reads chat
# existence straight off here (both pods mount this volume) to decide which
# per-chat profiles are orphaned — no call to the workspace pod. See profile_gc.
PROJECTS_ROOT = os.path.join(_AGENT_HOME, ".claude", "projects")

# The path-traversal safe-landing session id. Since Pillar C the agent plane
# REJECTS a chat_id-less tool call (see _session_id), so this is no longer an
# agent-plane fallback; it remains the /cobrowse viewer plane's default and the
# dir builders' safe landing for a present-but-non-slug id (see _safe_dir_name).
SHARED_SESSION = "shared"

# Query param the in-pod broker adds to the agent's MCP requests to name the chat
# (see apps/workspace/src/mcp_config.py). Read off the live request below.
_CHAT_ID_PARAM = "chat_id"

# Cap on concurrent Chromiums in one pod (env-overridable so stg can retune once
# headed-Chrome memory is measured). The idle reaper closes walked-away sessions;
# the cap bounds the worst case of many chats co-browsing at once.
_MAX_SESSIONS = int(os.environ.get("BROWSER_MAX_SESSIONS", "3"))

# Where the built viewer bundle lives, when the image carries one. Empty
# disables the root mount entirely rather than guessing a path.
VIEWER_DIR = os.environ.get("BROWSER_VIEWER_DIR", "")

# A session id used verbatim as a profile-dir NAME must be filesystem-safe: the id
# reaches us from a URL the agent's MCP client sends, so a hostile ``chat_id`` like
# ``../../etc`` could escape BROWSER_PROFILE_DIR. Accept only a conservative slug;
# anything else is refused BEFORE it becomes a path (callers fall back / reject).
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def _session_id() -> str:
    """The current chat's session id, from the live MCP request's ``?chat_id=``.

    Read off ``request_ctx`` (the low-level server's per-request ContextVar) so no
    tool handler needs a ``Context`` parameter.

    **Fail-closed (Pillar C): a tool CALL that arrives over HTTP with no
    ``?chat_id=`` is rejected**, instead of silently sharing one ``shared``
    profile across every chat — which would leak one chat's cookies/logins into
    another. Only the agent's tool handlers read this; ``initialize`` /
    ``tools/list`` / readiness are the broker's chat_id-less probes and never
    invoke a handler, so rejecting here never strips the toolset. A no-HTTP
    scope (stdio / unit tests) has nothing to isolate and still returns
    :data:`SHARED_SESSION`; the dir builders keep their own ``shared``
    safe-landing (:func:`_safe_dir_name`) for a present-but-non-slug id."""
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
    """The session id as a single path segment, or :data:`SHARED_SESSION` if it
    isn't a safe one. ``_SAFE_SESSION_ID``'s char class alone matches ``.`` and
    ``..`` (both chars are in ``[A-Za-z0-9_.-]``), which as a profile-dir NAME
    traverses out of the profile root — harmless under read/write today, but
    catastrophic once the GC can rm/rename a profile path. Reject dot-only names
    and any path separator explicitly before trusting the slug (B.4)."""
    if session_id in (".", "..") or "/" in session_id or os.sep in session_id:
        return SHARED_SESSION
    return session_id if _SAFE_SESSION_ID.fullmatch(session_id) else SHARED_SESSION


def _profile_dir_for(session_id: str) -> str | None:
    """The per-chat Chromium profile dir under :data:`PROFILE_DIR`. None when no
    volume is mounted (ephemeral, e.g. CI). Each chat gets its own subdir so their
    persistent contexts don't fight over one user-data-dir's Singleton lock, and a
    non-slug id can't traverse out of the profile root."""
    if not PROFILE_DIR:
        return None
    return os.path.join(PROFILE_DIR, _safe_dir_name(session_id))


def _cache_dir_for(session_id: str) -> str | None:
    """This chat's Chrome disk-cache dir under :data:`CACHE_DIR` (the node-disk
    emptyDir), or None when no cache dir is configured. Per-session on purpose:
    concurrent Chromes sharing one --disk-cache-dir corrupt the cache and leak
    HTTP cache across chats. Same slug guard as the profile dir so a non-slug id
    can't traverse out of the cache root."""
    if not CACHE_DIR:
        return None
    return os.path.join(CACHE_DIR, _safe_dir_name(session_id))


# Opportunistic orphan-profile GC (:mod:`src.profile_gc`): reclaim per-chat
# profiles whose chat was deleted while this pod was up. Triggered at container
# boot and at session start — a natural "next use", NOT a standing timer —
# throttled so a busy pod sweeps at most once per interval, and run in a worker
# thread so it never blocks a launch. Boot drains the cold backlog; session-start
# catches deletes that happen mid-uptime.
_GC_MIN_INTERVAL_S = 600.0
_gc_lock = asyncio.Lock()
_gc_last_ts = 0.0
_bg_tasks: set[asyncio.Task[Any]] = set()


async def _sweep_profiles_bg(force: bool = False) -> None:
    """Run the profile GC in a worker thread (never blocks the event loop).
    Throttled to ``_GC_MIN_INTERVAL_S`` unless ``force`` (boot); deduped via
    ``_gc_lock`` so overlapping triggers collapse to one sweep. Best-effort —
    ``run_sweep`` never raises."""
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
    """Fire a throttled GC sweep without awaiting it — a session launch or
    container boot must not wait on filesystem work. Holds a task ref so the
    fire-and-forget sweep isn't GC'd mid-flight."""
    task = asyncio.create_task(_sweep_profiles_bg(force=force))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _playwright_factory(session_id: str) -> BrowserDriver:
    """Mint a started real driver for one chat's session, with its own persistent
    profile subdir + its own node-disk cache subdir. Injected into the manager in
    prod; tests pass a fake factory."""
    driver = PlaywrightDriver(
        profile_dir=_profile_dir_for(session_id),
        cache_dir=_cache_dir_for(session_id),
        disk_cache_size=DISK_CACHE_SIZE,
    )
    await driver.start()
    _schedule_profile_gc()  # opportunistic orphan-profile reclaim (throttled, threaded)
    return driver


manager = SessionManager(_playwright_factory, max_sessions=_MAX_SESSIONS)

# How often the background reaper sweeps for idle, viewer-less sessions to close.
_REAP_INTERVAL_S = 60.0


async def _reap_idle_loop() -> None:
    """Close idle, viewer-less chat sessions on a timer so a pod doesn't accumulate
    walked-away Chromiums. Runs for the app's life (started/stopped by the lifespan).
    A transient reap error is logged, not fatal — the loop must outlive one bad sweep."""
    while True:
        await asyncio.sleep(_REAP_INTERVAL_S)
        try:
            closed = await manager.reap_idle()
            if closed:
                log.info("cobrowse reaped %d idle session(s): %s", len(closed), ", ".join(closed))
        except Exception:
            log.exception("cobrowse reaper loop error")


mcp = FastMCP("browser", host=HOST, port=PORT)


@mcp.tool()
async def browser_open(url: str, new_tab: bool = False, reason: str = "") -> dict[str, Any]:
    """Open a URL in the shared co-browsing browser.

    The browser runs server-side; the user watches it live in the viewer and can
    take over at any time. Use this to start working a web page — a public form,
    a portal, a vendor site — then read it with ``browser_snapshot``.

    **Not your default way to reach the web.** This spins up a real browser the
    user watches live — heavier and slower than it needs to be for anything that
    doesn't require interactive, multi-step work (forms, logins, portals) or live
    human collaboration. Before reaching for it:
    - For "look something up" / "read this page" / "search for X", prefer your
      WebSearch/WebFetch tools — they're cheaper and faster, and this browser adds
      nothing for a plain read.
    - If a purpose-built tool exists for the data you need (a connector, an MCP
      data/API tool), prefer it over browsing the raw site — it's more reliable
      and won't hit this browser's network limits.
    - This browser cannot reach every site — it runs from the platform's own
      infrastructure, and some sites block or are unreachable from that
      network/region (e.g. some countries' financial data sites). A page that
      hangs at a blank/loading state is often this, not a slow network — don't
      retry indefinitely; say so and suggest an alternative instead.

    **The FIRST call each chat is refused unless you've already tried
    WebSearch/WebFetch, or you pass ``reason``.** If you already know this page
    needs a login, a form, or a multi-step session — so WebSearch/WebFetch
    couldn't do it anyway — say so in ``reason`` and it proceeds immediately, no
    wasted round trip. Leave ``reason`` empty only when you're not yet sure this
    needs a live browser.

    Args:
        url: Absolute URL to navigate to.
        new_tab: Open in a NEW tab (and make it active) instead of navigating the
            current one. Use this to keep a page open while working another.
        reason: One short phrase on why this can't be a WebSearch/WebFetch
            instead (e.g. "tender portal, needs login"). Only checked on a
            chat's first browser_open; ignored after that.

    Returns:
        Navigation state ``{url, title, can_go_back, can_go_forward}``.
    """
    return await agent_ops.open_url(manager, _session_id(), url, new_tab=new_tab)


@mcp.tool()
async def browser_list_tabs() -> list[dict[str, Any]]:
    """List the open browser tabs as ``[{id, title, url, active}]``.

    Use the ``id`` with ``browser_switch_tab`` / ``browser_close_tab``. Snapshot,
    click, and type always act on the ACTIVE tab.
    """
    return await agent_ops.list_tabs(manager, _session_id())


@mcp.tool()
async def browser_switch_tab(tab_id: str) -> dict[str, Any]:
    """Switch to an open tab (from ``browser_list_tabs``), making it active.

    Args:
        tab_id: The tab's id.
    """
    return await agent_ops.switch_tab(manager, _session_id(), tab_id)


@mcp.tool()
async def browser_close_tab(tab_id: str) -> dict[str, Any]:
    """Close an open tab (from ``browser_list_tabs``).

    Args:
        tab_id: The tab's id.
    """
    return await agent_ops.close_tab(manager, _session_id(), tab_id)


@mcp.tool()
async def browser_snapshot() -> list[dict[str, Any]]:
    """Read the current page as a list of interactable elements.

    Each element has a ``ref`` you pass to ``browser_click`` / ``browser_type``,
    plus its tag/type/name/value. Password field values are ALWAYS redacted —
    you will never receive a secret you (or the user) typed.
    """
    return await agent_ops.snapshot(manager, _session_id())


@mcp.tool()
async def browser_login(portal_id: str) -> dict[str, Any]:
    """Log in to a portal the user has configured for this workspace.

    You pass ONLY the portal id (e.g. "acme-portal"); the username and password
    are stored securely and injected server-side — they are never shown to you.
    Use this when a page needs a login you don't have visible credentials for.
    Call ``browser_snapshot`` afterwards to see whether login succeeded or a
    challenge (MFA/CAPTCHA) needs the user.

    Args:
        portal_id: The configured portal to authenticate to.

    Returns:
        ``{status, portal_id, ...}`` where status is ``submitted`` (credentials
        entered), ``unknown_portal`` (no such portal configured), or
        ``no_login_form`` (no login form found on the page).
    """
    return await agent_ops.login(manager, _session_id(), portal_id, read_portals())


@mcp.tool()
async def browser_click(ref: str, label: str = "") -> str:
    """Click an element on the current page.

    The user's workspace asks them to approve click/type actions in the chat
    before they run, so pass a short human-readable ``label`` — the approval
    prompt shows it (e.g. "Click 'Submit application'" instead of a raw ref).

    Args:
        ref: An element ref from a prior ``browser_snapshot``.
        label: A short description of what you're clicking (the element's visible
            text), for the user's approval prompt. Optional but strongly preferred.
    """
    await agent_ops.click(manager, _session_id(), ref)
    return "ok"


@mcp.tool()
async def browser_request_takeover(reason: str) -> dict[str, Any]:
    """Ask the user to take over the browser when you can't proceed alone.

    Use this for a two-factor prompt, a CAPTCHA, an unexpected login challenge,
    or anything needing a human decision. It pauses you and shows the user a
    take-over banner in their live view. After they act, call ``browser_snapshot``
    to see the new page state and continue.

    Args:
        reason: A short, user-facing explanation of what you need them to do.
    """
    return await agent_ops.request_takeover(manager, _session_id(), reason)


@mcp.tool()
async def browser_type(ref: str, text: str, label: str = "") -> str:
    """Type text into a field on the current page.

    As with ``browser_click``, the user approves this in chat first — pass a
    short ``label`` (the field's name) so the prompt is readable.

    Args:
        ref: An element ref from a prior ``browser_snapshot``.
        text: The text to enter.
        label: A short description of the field you're typing into, for the user's
            approval prompt. Optional but strongly preferred.
    """
    await agent_ops.type_text(manager, _session_id(), ref, text)
    return "ok"


@mcp.tool()
async def browser_scroll(direction: str = "down", amount: int = 600) -> str:
    """Scroll the current page to reveal more content, then ``browser_snapshot``
    to read the newly visible elements.

    Scrolling is just viewing, so it runs without asking the user (unlike
    click/type). Use it to reach content below or above the fold.

    Args:
        direction: "down" (default) or "up".
        amount: How far to scroll in pixels (default 600, roughly one screen).
    """
    dirn = "up" if str(direction).lower() == "up" else "down"
    await agent_ops.scroll(manager, _session_id(), dirn, int(amount))
    return "ok"


# --- reading / understanding the page (run without asking the user) ---------


@mcp.tool()
async def browser_read() -> str:
    """Return the current page's readable text (article/main content, capped).

    Use this to actually READ what a page says — instructions, data, prose,
    tables as text. ``browser_snapshot`` lists what you can click; this gives you
    the content. Viewing only, so it doesn't ask the user.
    """
    return await agent_ops.read(manager, _session_id())


@mcp.tool()
async def browser_find(query: str) -> dict[str, Any]:
    """Find text on the current page (like Ctrl+F) and scroll the first match into
    view. Returns how many times it appears and a snippet around the first hit.

    Args:
        query: The text to search for (case-insensitive).
    """
    return await agent_ops.find_text(manager, _session_id(), query)


@mcp.tool()
async def browser_screenshot() -> Image:
    """Take a screenshot of the current page so you can SEE it — layout, images,
    charts, or anything the DOM snapshot doesn't convey. Viewing only.
    """
    png = await agent_ops.screenshot(manager, _session_id())
    return Image(data=png, format="png")


@mcp.tool()
async def browser_save_screenshot(path: str) -> dict[str, Any]:
    """Save a screenshot of the current page to the user's workspace, so it's
    stored and the user can open it in their files. Use when the user asks you to
    capture something, or you want to keep a record of a page.

    Args:
        path: Where to save it in the workspace (e.g. "screenshots/quote.png").
            A `.png` file under the user's workspace.
    """
    dest = _resolve_workspace_path(path)
    if dest is None:
        return {"status": "path_not_allowed", "path": path}
    png = await agent_ops.screenshot(manager, _session_id())
    await asyncio.to_thread(_write_bytes, dest, png)
    return {"status": "saved", "path": dest}


@mcp.tool()
async def browser_inspect(ref: str) -> dict[str, Any]:
    """Inspect one element from a snapshot: its tag, text, attributes, on-screen
    box, and whether it's visible/enabled. Use it when a click/type didn't behave
    as expected, or to understand a control before acting.

    Args:
        ref: An element ref from a prior ``browser_snapshot``.
    """
    return await agent_ops.inspect(manager, _session_id(), ref)


@mcp.tool()
async def browser_get_table(ref: str = "") -> list[list[list[str]]]:
    """Extract tables on the page as structured rows (``[table][row][cell]``).
    Great for reading tender listings, prices, or any tabular data.

    Args:
        ref: Optionally a ref inside/at one table (from a snapshot) to get just
            that table; omit to get all tables on the page.
    """
    return await agent_ops.get_table(manager, _session_id(), ref or None)


# --- history + reliability (navigation; run without asking the user) --------


@mcp.tool()
async def browser_back() -> str:
    """Go back to the previous page in history."""
    await agent_ops.go_back(manager, _session_id())
    return "ok"


@mcp.tool()
async def browser_forward() -> str:
    """Go forward in history."""
    await agent_ops.go_forward(manager, _session_id())
    return "ok"


@mcp.tool()
async def browser_reload() -> str:
    """Reload the current page."""
    await agent_ops.reload(manager, _session_id())
    return "ok"


@mcp.tool()
async def browser_wait_for(
    text: str = "", selector: str = "", timeout_ms: int = 8000
) -> dict[str, Any]:
    """Wait until content loads before reading/acting — pages often load
    asynchronously. Wait for some ``text`` to appear, or a CSS ``selector`` to
    match; with neither, wait for the network to go idle. Returns ``{ready: bool}``.

    Args:
        text: Text to wait for on the page.
        selector: A CSS selector to wait for.
        timeout_ms: How long to wait before giving up (default 8000).
    """
    ready = await agent_ops.wait_for(
        manager,
        _session_id(),
        text=text or None,
        selector=selector or None,
        timeout_ms=int(timeout_ms),
    )
    return {"ready": ready}


# --- extra actions (the user approves these in chat, like click/type) -------


@mcp.tool()
async def browser_press_key(key: str, label: str = "") -> str:
    """Press a key on the current page — e.g. "Enter" to submit, "Escape" to close
    a dialog, "Tab" to move fields, "ArrowDown" in a menu.

    The user approves this in chat (Enter can submit a form), so pass a short
    ``label`` describing the effect (e.g. "Submit the search").

    Args:
        key: A key name (Enter, Escape, Tab, ArrowDown, PageDown, …).
        label: A short description of what pressing it does, for the approval prompt.
    """
    await agent_ops.press_key(manager, _session_id(), key)
    return "ok"


@mcp.tool()
async def browser_select_option(ref: str, value: str, label: str = "") -> str:
    """Choose an option in a native dropdown (``<select>``). Approved in chat.

    Args:
        ref: A ref (from a snapshot) for the ``<select>`` element.
        value: The option's value or visible label to select.
        label: A short description for the approval prompt (e.g. "Set Country = UK").
    """
    await agent_ops.select_option(manager, _session_id(), ref, value)
    return "ok"


@mcp.tool()
async def browser_upload_file(ref: str, path: str, label: str = "") -> dict[str, Any]:
    """Attach a file from the workspace to a file-upload field — e.g. to submit a
    document with a tender. The file must be one of the user's workspace files.
    Approved in chat.

    Args:
        ref: A ref (from a snapshot) for the file ``<input>``.
        path: Path to the file in the workspace (under the agent home).
        label: A short description for the approval prompt (e.g. "Upload proposal.pdf").
    """
    resolved = _resolve_workspace_path(path)
    if resolved is None:
        return {"status": "path_not_allowed", "path": path}
    if not await asyncio.to_thread(os.path.isfile, resolved):
        return {"status": "not_found", "path": path}
    await agent_ops.upload_file(manager, _session_id(), ref, resolved)
    return {"status": "attached", "path": resolved}


@mcp.tool()
async def browser_download(ref: str, path: str, label: str = "") -> dict[str, Any]:
    """Download a file the page offers (click a download link/button by ``ref``)
    and SAVE it into the user's workspace so it's kept and the user can open it —
    e.g. a tender document, receipt, or export. Approved in chat.

    Args:
        ref: A ref (from a snapshot) for the download link/button to click.
        path: Where to save it in the workspace (e.g. "downloads/tender.pdf").
        label: A short description for the approval prompt (e.g. "Download the tender pack").
    """
    dest = _resolve_workspace_path(path)
    if dest is None:
        return {"status": "path_not_allowed", "path": path}
    await asyncio.to_thread(_ensure_parent, dest)
    result = await agent_ops.download(manager, _session_id(), ref, dest)
    return {"status": "downloaded", "path": dest, **result}


# --- frames / iframes (viewing — no chat prompt) ----------------------------


@mcp.tool()
async def browser_list_frames() -> list[dict[str, Any]]:
    """List the frames (iframes) in the active tab as ``[{index, name, url}]``.

    Index 0 is the top-level page; 1+ are embedded iframes in document order —
    portals often put a login form or PDF viewer inside one. Then
    ``browser_switch_frame`` to act inside it. Viewing only.
    """
    return await agent_ops.list_frames(manager, _session_id())


@mcp.tool()
async def browser_switch_frame(target: str = "") -> dict[str, Any]:
    """Choose which frame of the active tab ``browser_snapshot`` / ``browser_click``
    / ``browser_type`` act inside — e.g. an embedded form or PDF viewer. Switching
    between views you already have open, so it runs without asking the user.

    Args:
        target: A frame ``index`` (from ``browser_list_frames``) or ``name``.
            Empty (default), "main", or "0" resets to the top-level page.

    Returns:
        ``{status, target}`` — ``switched`` / ``reset`` / ``unknown_frame``.
    """
    return await agent_ops.switch_frame(manager, _session_id(), target)


# --- native dialogs ---------------------------------------------------------


@mcp.tool()
async def browser_set_dialog_mode(mode: str, label: str = "") -> dict[str, Any]:
    """Choose how the browser answers native JS pop-ups (alert / confirm / prompt /
    the "leave this page?" beforeunload) that would otherwise freeze the page.

    Modes:
      * "dismiss" (default) — click Cancel / stay. SAFE: a confirm never commits,
        a beforeunload never abandons unsaved work.
      * "accept" — click OK / leave. Use ONLY right before an action you KNOW
        raises a benign confirm you intend to accept (e.g. a cookie banner's "I
        agree"). Accepting is committing — it can approve a delete, submit, or
        order — so the user must approve this. Set it back to "dismiss" after.

    Args:
        mode: "accept" or "dismiss" (anything else is treated as "dismiss").
        label: Short reason (shown in the user's approval prompt).
    """
    return await agent_ops.set_dialog_mode(manager, _session_id(), mode)


@mcp.tool()
async def browser_last_dialog() -> dict[str, Any]:
    """Report the most recent native pop-up the browser auto-handled: ``{status:
    "none"}`` or ``{status: "handled", type, message, action, ...}`` (``action`` is
    how it was answered). A "dismiss"ed confirm means the action did NOT go
    through. Viewing only.
    """
    return await agent_ops.last_dialog(manager, _session_id())


# --- console / network observability (viewing) ------------------------------


@mcp.tool()
async def browser_console() -> list[dict[str, Any]]:
    """Read the page's recent console messages as ``[{type, text}]`` (incl. uncaught
    errors, type "error"). Use this to diagnose an action that appeared to do
    nothing — a validation error or thrown exception usually logs here. Viewing only.
    """
    return await agent_ops.console_log(manager, _session_id())


@mcp.tool()
async def browser_network(url_substring: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """Read the page's recent network requests as ``[{method, url, status,
    resource_type}]``. ``status`` 0 = a request that never completed
    (blocked/aborted) — a strong signal a submit silently failed. Filter with
    ``url_substring`` (e.g. "/api/save"). Viewing only.

    Args:
        url_substring: Only entries whose URL contains this (empty = all).
        limit: Max entries (default 20).
    """
    sub = url_substring or None
    return await agent_ops.network_log(manager, _session_id(), url_substring=sub, limit=int(limit))


@mcp.tool()
async def browser_wait_for_response(url_substring: str, timeout_ms: int = 15000) -> dict[str, Any]:
    """Wait for a network response whose URL contains ``url_substring`` — use right
    after a click that triggers an async save/submit to confirm it landed. Returns
    ``{matched, status, url}`` (matched False on timeout). Viewing only.

    Args:
        url_substring: Text the target response URL must contain (e.g. "/api/apply").
        timeout_ms: How long to wait (default 15000, capped at 60000).
    """
    sub = (url_substring or "").strip()
    if not sub:
        return {"matched": False, "status": 0, "url": "", "error": "url_substring is required"}
    budget = max(0, min(int(timeout_ms), 60000))
    return await agent_ops.wait_for_response(manager, _session_id(), sub, budget)


# --- interaction extras -----------------------------------------------------


@mcp.tool()
async def browser_hover(ref: str, label: str = "") -> str:
    """Hover the pointer over an element to reveal a menu/tooltip it only shows on
    hover; then ``browser_snapshot`` to read what appeared. Acts on the page, so
    the user approves it in chat — pass a short ``label``.

    Args:
        ref: An element ref from a prior ``browser_snapshot``.
        label: Short description of what you're hovering, for the approval prompt.
    """
    await agent_ops.hover(manager, _session_id(), ref)
    return "ok"


@mcp.tool()
async def browser_drag(from_ref: str, to_ref: str, label: str = "") -> str:
    """Drag one element onto another (reorder a list, drop onto a drop-zone). Both
    refs come from a prior snapshot. Changes the page, so the user approves it.

    Args:
        from_ref: The element to drag.
        to_ref: The element to drop it onto.
        label: Short description of the drag, for the approval prompt.
    """
    await agent_ops.drag(manager, _session_id(), from_ref, to_ref)
    return "ok"


@mcp.tool()
async def browser_scroll_to(ref: str) -> dict[str, Any]:
    """Scroll a specific element into view so it can be clicked or read — use when
    a click reports "not interactable" or to reveal a below-fold element. Viewing
    only.

    Args:
        ref: An element ref from a prior ``browser_snapshot``.

    Returns:
        ``{status}`` — ``scrolled`` or ``not_found`` (re-snapshot and retry).
    """
    return await agent_ops.scroll_to(manager, _session_id(), ref)


@mcp.tool()
async def browser_get_options(ref: str) -> list[dict[str, Any]]:
    """List a dropdown's (``<select>``) options as ``[{value, label, selected}]`` so
    you can pick a real ``value`` before ``browser_select_option``. A snapshot only
    shows the current value. Empty if ``ref`` isn't a select. Viewing only.

    Args:
        ref: The ``<select>`` element's ref from a prior ``browser_snapshot``.
    """
    return await agent_ops.get_options(manager, _session_id(), ref)


@mcp.tool()
async def browser_get_links() -> list[dict[str, str]]:
    """List the page's links as ``[{text, href}]`` with absolute URLs (up to ~200),
    to plan navigation without scrolling and snapshotting the whole page. Viewing
    only.
    """
    return await agent_ops.get_links(manager, _session_id())


@mcp.tool()
async def browser_eval(js: str, label: str = "") -> dict[str, Any]:
    """Run a small JavaScript snippet on the current page and get its result. Use
    ONLY when the structured tools can't answer (read a computed value, count
    nodes, extract text a snapshot misses). It runs IN the page with the page's
    privileges, so the user must approve it — pass a ``label`` saying what it does.
    A script error returns ``{error}`` instead of failing.

    Args:
        js: A JS expression or block; its value (or a returned Promise's) is
            returned, stringified and length-capped.
        label: A short description of what the script does, for the approval prompt.

    Returns:
        ``{result}`` with the stringified value, or ``{error}`` on a JS error.
    """
    return await agent_ops.eval_js(manager, _session_id(), js)


def _resolve_workspace_path(path: str) -> str | None:
    """Resolve ``path`` under the tenant volume, or None if it escapes it — so the
    agent can't read/write arbitrary pod paths (``/etc/…``) via upload/download."""
    base = path if os.path.isabs(path) else os.path.join(_AGENT_HOME, path)
    resolved = os.path.realpath(base)
    if resolved != _AGENT_HOME and not resolved.startswith(_AGENT_HOME + os.sep):
        return None
    return resolved


def _ensure_parent(dest: str) -> None:
    os.makedirs(os.path.dirname(dest) or _AGENT_HOME, exist_ok=True)


def _write_bytes(dest: str, data: bytes) -> None:
    _ensure_parent(dest)
    with open(dest, "wb") as f:
        f.write(data)


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
        # The path id is the chat id (the dispatcher proxies to /cobrowse/<chat_id>):
        # the viewer meets the agent on THAT chat's session. Missing/blank → the
        # shared fallback, matching the agent plane so the two never split.
        session_id = ws.path_params.get("session_id") or SHARED_SESSION
        # The dispatcher sets ?control=0 for a WATCH-ONLY viewer (chat grant = view)
        # and ?control=1 (or omits it) for a driver (edit/owner). Absent → drive, so
        # an older dispatcher that doesn't send it behaves as before.
        can_drive = ws.query_params.get("control") != "0"
        await ws.accept()
        metrics.inc_viewer_connection()
        adapter = _StarletteWsAdapter(ws)
        conn = CoBrowseConnection(manager, session_id, adapter, can_drive=can_drive)
        try:
            await conn.run()
        except Exception:
            metrics.inc_session_error()
            log.exception("cobrowse connection error")
        finally:
            with contextlib.suppress(Exception):
                await ws.close()

    async def metrics_endpoint(_req: Request) -> Response:
        body, content_type = metrics.render()
        return Response(content=body, media_type=content_type)

    # Append rather than app.add_websocket_route(...) — the latter isn't typed on
    # the Starlette app in this version; the router's route list is the stable seam.
    app.router.routes.append(WebSocketRoute("/cobrowse/{session_id}", cobrowse_endpoint))
    app.router.routes.append(Route("/metrics", metrics_endpoint, methods=["GET"]))

    # The bundled viewer, when one was built into the image. Mounted LAST and at
    # the root so it can never shadow /mcp, /cobrowse or /metrics — Starlette
    # matches routes in order. Its absence is not an error: the platform serves
    # its own React viewer and does not need this one, so a build without the
    # bundle simply has no root route.
    if VIEWER_DIR and os.path.isdir(VIEWER_DIR):
        from starlette.staticfiles import StaticFiles

        app.router.routes.append(
            Mount("/", app=StaticFiles(directory=VIEWER_DIR, html=True), name="viewer")
        )
        log.info("serving bundled co-browse viewer from %s", VIEWER_DIR)

    # Run the idle-session reaper alongside FastMCP's own lifespan (which starts the
    # streamable-HTTP session manager) — wrap, don't replace, so both run.
    mcp_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _lifespan_with_reaper(app_: Any) -> Any:
        async with mcp_lifespan(app_):
            # Container-boot hygiene: clear stale Singleton* left on the tenant
            # volume by hard-killed pods (only an in-Chrome quit unlinks them), so
            # the strict S3 sync runs clean between sessions. Container-up ≠
            # Chrome-up — no Chrome is running in THIS pod yet, and profiles owned
            # by a live Chrome in ANOTHER pod are skipped via the per-profile
            # flock. Best-effort; must never block serving.
            if PROFILE_DIR:
                with contextlib.suppress(Exception):
                    swept = browser_driver.clear_stale_profile_locks(PROFILE_DIR)
                    if swept:
                        log.info("boot sweep cleared stale browser locks in %d profiles", swept)
                # Reclaim profiles orphaned while this pod was down (the cold
                # backlog) off the durable volume — forced (bypass throttle),
                # threaded, so a large backlog never delays readiness.
                _schedule_profile_gc(force=True)
            reaper = asyncio.create_task(_reap_idle_loop())
            try:
                yield
            finally:
                reaper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reaper
                # Graceful shutdown (SIGTERM → lifespan exit on a deploy/drain):
                # close every live session. context.close() is an IN-CHROME quit —
                # the only exit that unlinks Chrome's Singleton* locks — so a drain
                # leaves every profile lock-free on the volume. Best-effort and
                # bounded by the pod's terminationGracePeriod.
                with contextlib.suppress(Exception):
                    await manager.close_all()

    app.router.lifespan_context = _lifespan_with_reaper
    return app


class _StarletteWsAdapter:
    """Adapts a Starlette ``WebSocket`` to the ``send_json``/``receive_json``
    surface :class:`CoBrowseConnection` expects, returning ``None`` on close so
    the recv loop exits cleanly instead of raising.

    "The socket is gone" is a single fact (:attr:`_closed`), set by WHICHEVER
    side discovers the disconnect first — a failed send OR a failed receive. This
    matters because the two run in concurrent tasks (the screencast pump sends
    while the recv loop receives) and Starlette surfaces a disconnect asymmetrically:
    a send that hits ``OSError`` flips the socket's ``application_state`` to
    DISCONNECTED and raises ``WebSocketDisconnect``; the recv guard then reads that
    same state and raises a *bare* ``RuntimeError`` ('WebSocket is not connected'),
    NOT a ``WebSocketDisconnect``. Catching only the latter on receive let that
    ``RuntimeError`` escape and kill the whole handler — inflating
    ``cobrowse_session_errors_total`` on ordinary session-ends. Both directions now
    treat either exception as a clean close and short-circuit once closed."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._closed = False

    async def send_json(self, data: dict[str, Any]) -> None:
        # Best-effort: a client mid-disconnect makes Starlette's send raise
        # (WebSocketDisconnect / RuntimeError). Swallow it — letting a failed push
        # propagate would tear down the WHOLE handler, dropping the human's next
        # input (e.g. a tab-close click) until they reconnect.
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
        # A disconnect discovered on the send side (pump) flips application_state,
        # so Starlette's receive guard raises a bare RuntimeError here rather than
        # WebSocketDisconnect. Treat both as a clean close: return None so the recv
        # loop exits normally and the handler's except-branch (which counts a
        # session error) never runs.
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

    logging.basicConfig(level=logging.INFO)
    log.info("workspace-tool-browser on %s:%d — /mcp (agent) + /cobrowse (human)", HOST, PORT)
    uvicorn.run(build_app(), host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()

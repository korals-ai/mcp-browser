"""Agent-plane operations, independent of the MCP transport.

The MCP tools in :mod:`src.server` are thin handlers; the real work — resolve
the session and the tab, honour the human's pause, act on the driver, build
the reply, redact — lives here so it's unit-testable without spinning up
FastMCP. Each function takes a :class:`SessionManager`, a ``session_id`` and
(for per-tab tools) the numeric ``tab_id`` the call named, and returns plain
data / raises a domain error the handler maps.

The contract is the Claude-in-Chrome extension's: a mutating action replies
with what it did plus ONLY what changed (a navigation, a new tab, a dialog) — never the page. The model
chains ``computer`` + ``read_page`` in one ``browser_batch`` when it wants the
page back.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from src import recipes
from src.browser_driver import UnknownTabError
from src.find_model import FindConfig, find_with_model
from src.portal_creds import PortalCred
from src.protocol import BrowserAgentState, BrowserNav, BrowserTabs
from src.refs_tree import (
    format_matches,
    interactive_only,
    literal_matches,
    redact_values,
    ref_lines,
    truncate_at_line,
)
from src.sessions import SessionManager

log = logging.getLogger("workspace-tool-browser")

# Reply caps. The tree's is the extension's own default; the page text's was
# picked from simulation 1 (wesco.com pages read 4.9-5.2k chars of text, so
# 50k cuts nothing normal and still bounds a runaway page). Both cut at a
# line boundary and state the full size.
READ_PAGE_MAX_CHARS = 50_000
PAGE_TEXT_MAX_CHARS = 50_000
# A response body above this goes to a file instead of the context.
BODY_INLINE_CAP = 10_000
# Screenshot sizing: the extension downscales a big capture; we report the
# real frame so coordinates stay right.
ZOOM_DEFAULT_SCALE = 2.0

# The `computer` actions, exactly as the extension enumerates them.
COMPUTER_ACTIONS = frozenset(
    {
        "left_click",
        "right_click",
        "double_click",
        "triple_click",
        "type",
        "key",
        "screenshot",
        "zoom",
        "wait",
        "scroll",
        "scroll_to",
        "hover",
        "left_click_drag",
    }
)
# View-only ones observe the page while a human drives; the rest are gated
# on the human's pause (and, upstream, on the chat's approval).
_COMPUTER_VIEW_ACTIONS = frozenset({"screenshot", "zoom", "wait"})

Dispatch = Mapping[str, Callable[..., Awaitable[Any]]]


class AgentPaused(Exception):
    """Raised when the human has paused the agent on this session. The tool maps
    it to a benign message so the agent waits rather than fighting the human."""


class ToolInputError(ValueError):
    """The call's arguments cannot be acted on (an unknown action, a missing
    coordinate, a bad path). Named so the handler can report it as a tool error
    rather than a crash."""


# --- session / tab resolution --------------------------------------------------


async def _session(manager: SessionManager, session_id: str, *, act: bool) -> Any:
    """The chat's session. ``act`` honours the human's pause: a paused agent
    may observe (read/screenshot) but not act."""
    session = await manager.get_or_create(session_id)
    if act and session.agent_paused:
        raise AgentPaused("The human has taken over this browser; wait for them to hand back.")
    session.touch(actor="agent" if act else None)
    return session


async def _on_tab(manager: SessionManager, session_id: str, tab_id: int, *, act: bool) -> Any:
    """The session with tab ``tab_id`` made active. Acting on a tab activates
    it — the human's live view follows the agent."""
    session = await _session(manager, session_id, act=act)
    if not await session.driver.activate_num(int(tab_id)):
        raise UnknownTabError(
            f"No tab {tab_id} is open in this browser — call tabs_context_mcp to list "
            "the tabs, or tabs_create_mcp for a new one."
        )
    return session


async def _broadcast_tabs(session: Any) -> list[dict[str, Any]]:
    """Push the current tab list to viewers (drives the tab strip) and return it."""
    tabs = await session.driver.list_tabs()
    await session.broadcast(BrowserTabs(tabs=tabs).to_json())
    return tabs


async def _broadcast_nav(session: Any) -> dict[str, Any]:
    """Push the active tab's URL/title so the viewer's address bar tracks it."""
    nav = await session.driver.nav_state()
    await session.broadcast(
        BrowserNav(
            url=nav.get("url", ""),
            title=nav.get("title", ""),
            can_go_back=bool(nav.get("can_go_back")),
            can_go_forward=bool(nav.get("can_go_forward")),
        ).to_json()
    )
    return nav


def _redact(session: Any, text: str) -> str:
    """Outbound redaction of every secret this session injected — over every
    tool result that carries page-derived text."""
    return redact_values(text, session.injected_secrets)


def _tab_line(tab: dict[str, Any]) -> dict[str, Any]:
    return {
        "tabId": int(tab.get("tabId", 0)),
        "url": str(tab.get("url", "")),
        "title": str(tab.get("title", "")),
        "active": bool(tab.get("active")),
        "loaded": bool(tab.get("loaded", True)),
    }


# --- tabs -----------------------------------------------------------------------


async def tabs_context(manager: SessionManager, session_id: str) -> dict[str, Any]:
    session = await _session(manager, session_id, act=False)
    tabs = await session.driver.list_tabs()
    return {"tabs": [_tab_line(t) for t in tabs]}


async def tabs_create(manager: SessionManager, session_id: str) -> dict[str, Any]:
    session = await _session(manager, session_id, act=True)
    num = await session.driver.new_tab()
    await _broadcast_nav(session)
    await _broadcast_tabs(session)
    return {"tabId": num, "url": "about:blank"}


async def tabs_close(manager: SessionManager, session_id: str, tab_id: int) -> dict[str, Any]:
    session = await _session(manager, session_id, act=True)
    ok = await session.driver.close_tab_num(int(tab_id))
    if not ok:
        return {"status": "unknown_tab", "tabId": tab_id}
    await _broadcast_nav(session)
    tabs = await _broadcast_tabs(session)
    return {"status": "closed", "tabId": tab_id, "tabs": [_tab_line(t) for t in tabs]}


# --- navigation -------------------------------------------------------------------


async def navigate(
    manager: SessionManager, session_id: str, url: str, tab_id: int
) -> dict[str, Any]:
    """Open ``url`` in tab ``tab_id`` (``"back"`` / ``"forward"`` walk history).

    Returns the landed state with ``page_state`` — a wall (challenge, 403,
    429, 5xx) is named in the result itself so the agent never has to infer
    "blocked" from re-reading a challenge page; left to infer it, the model
    reliably retried the same navigation."""
    session = await manager.get_or_create(session_id)
    # A fresh navigation is the user redirecting the agent — it RESUMES
    # control. Clear any pause (a takeover the human never handed back from)
    # so "open <url>" always works instead of the agent locking itself out.
    was_paused = session.agent_paused
    session.agent_paused = False
    session.touch(actor="agent")
    if not await session.driver.activate_num(int(tab_id)):
        raise UnknownTabError(
            f"No tab {tab_id} is open in this browser — call tabs_context_mcp to list "
            "the tabs, or tabs_create_mcp for a new one."
        )
    driver = session.driver
    marker = driver.state_marker()
    target = (url or "").strip()
    if target == "back":
        await driver.go_back()
        page_state = "ok"
    elif target == "forward":
        await driver.go_forward()
        page_state = "ok"
    elif not target:
        raise ToolInputError('navigate needs a url (or "back" / "forward")')
    else:
        page_state = await driver.open(target)
    nav = await _broadcast_nav(session)
    await _broadcast_tabs(session)  # a title change updates the strip
    if was_paused:
        await session.broadcast(BrowserAgentState(state="idle", last_actor="agent").to_json())
    changes = [c for c in await driver.settle(marker) if not c.startswith("Page navigated")]
    return {
        "tabId": int(tab_id),
        "url": nav.get("url", ""),
        "title": nav.get("title", ""),
        "loaded": bool(nav.get("loaded", True)),
        "page_state": page_state,
        "changes": changes,
    }


# --- reading -----------------------------------------------------------------------


async def read_page(
    manager: SessionManager,
    session_id: str,
    tab_id: int,
    *,
    filter: str = "interactive",
    depth: int | None = None,
    max_chars: int = READ_PAGE_MAX_CHARS,
    ref_id: str | None = None,
    boxes: bool = False,
) -> str:
    session = await _on_tab(manager, session_id, tab_id, act=False)
    tree = await session.driver.read_page(depth=depth, boxes=boxes, ref_id=ref_id or None)
    if filter != "all":
        tree = interactive_only(tree)
    tree = _redact(session, tree)
    total = len(tree)
    body, cut = truncate_at_line(tree, max_chars)
    nav = await session.driver.nav_state()
    trailer = f'\n\nPage: {nav.get("url", "")} — "{nav.get("title", "")}" (tab {tab_id})'
    if cut:
        trailer += (
            f"\n[truncated at a line boundary: showing {len(body)} of {total} chars — "
            "raise max_chars, pass ref_id for a subtree, or use find]"
        )
    return body + trailer


async def get_page_text(manager: SessionManager, session_id: str, tab_id: int) -> str:
    session = await _on_tab(manager, session_id, tab_id, act=False)
    got = await session.driver.page_text()
    text = _redact(session, str(got.get("text", "")))
    total = len(text)
    body, cut = truncate_at_line(text, PAGE_TEXT_MAX_CHARS)
    head = (
        f"Title: {got.get('title', '')}\nURL: {got.get('url', '')}\n"
        f"Source element: {got.get('source', 'body')}\n\n"
    )
    if cut:
        body += f"\n[truncated at a line boundary: showing {len(body)} of {total} chars]"
    return head + body


async def find(
    manager: SessionManager,
    session_id: str,
    tab_id: int,
    query: str,
    *,
    config: FindConfig,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Two tiers: a literal/regex match over the tree first (free); the
    configured model only on a miss, and only when one is configured."""
    q = (query or "").strip()
    if not q:
        raise ToolInputError("find needs a query")
    session = await _on_tab(manager, session_id, tab_id, act=False)
    tree = _redact(session, await session.driver.read_page())
    hits = literal_matches(tree, q)
    if hits:
        return format_matches(hits, source="literal")
    if not config.enabled:
        return (
            "No match for the query in the page's tree (literal tier; no model tier is "
            "configured). Try other words, or read_page and look yourself."
        )
    known = set(ref_lines(tree))
    model_hits = await find_with_model(config, tree=tree, query=q, known_refs=known, client=client)
    if not model_hits:
        return (
            "No match — neither a literal match nor the model found an element for the "
            "query. Try other words, or read_page and look yourself."
        )
    lines = ref_lines(tree)
    for hit in model_hits:
        hit["line"] = lines.get(str(hit["ref"]), "")
    return format_matches(model_hits, source="model")


# --- `computer` ---------------------------------------------------------------------


def _coordinate(raw: Any, name: str = "coordinate") -> tuple[int, int] | None:
    if raw is None:
        return None
    try:
        x, y = raw
        return int(x), int(y)
    except (TypeError, ValueError) as exc:
        raise ToolInputError(f"{name} must be [x, y]") from exc


def _region(raw: Any) -> tuple[int, int, int, int] | None:
    if raw is None:
        return None
    try:
        x0, y0, x1, y1 = (int(v) for v in raw)
    except (TypeError, ValueError) as exc:
        raise ToolInputError("region must be [x0, y0, x1, y1]") from exc
    if x1 <= x0 or y1 <= y0:
        raise ToolInputError("region must have x1 > x0 and y1 > y0")
    return x0, y0, x1, y1


def _scale(raw: Any, default: float) -> float:
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError) as exc:
        raise ToolInputError("scale must be a number") from exc
    if not 0.1 <= val <= 2.0:
        raise ToolInputError("scale must be between 0.1 and 2.0")
    return val


def _image_reply(kind: str, meta: dict[str, Any]) -> str:
    factor = (meta["image_width"] / meta["width"]) if meta.get("width") else meta.get("scale", 1.0)
    return (
        f"{kind}: image {meta.get('image_width')}x{meta.get('image_height')} px covers page "
        f"region x={meta.get('x', 0)}..{meta.get('x', 0) + meta.get('width', 0)}, "
        f"y={meta.get('y', 0)}..{meta.get('y', 0) + meta.get('height', 0)} CSS px "
        f"({factor:.2f} image px per CSS px). Page coordinate = region origin + image "
        f"px / {factor:.2f}."
    )


async def computer(
    manager: SessionManager,
    session_id: str,
    tab_id: int,
    action: str,
    **kw: Any,
) -> dict[str, Any]:
    """One tool, thirteen actions. Returns ``{"text": …}`` plus ``"png"``
    bytes for screenshot/zoom. Mutating actions report only what changed."""
    if action not in COMPUTER_ACTIONS:
        raise ToolInputError(
            f"unknown computer action {action!r}; one of {', '.join(sorted(COMPUTER_ACTIONS))}"
        )
    session = await _on_tab(manager, session_id, tab_id, act=action not in _COMPUTER_VIEW_ACTIONS)
    driver = session.driver
    ref = str(kw.get("ref") or "") or None
    coordinate = _coordinate(kw.get("coordinate"))
    text = kw.get("text")

    if action == "screenshot":
        png, meta = await driver.screenshot(
            scale=_scale(kw.get("scale"), 1.0), region=_region(kw.get("region"))
        )
        return {"text": _image_reply("Screenshot", meta), "png": png, "meta": meta}
    if action == "zoom":
        region = _region(kw.get("region"))
        if region is None:
            raise ToolInputError("zoom needs a region [x0, y0, x1, y1]")
        png, meta = await driver.screenshot(
            scale=_scale(kw.get("scale"), ZOOM_DEFAULT_SCALE), region=region
        )
        return {"text": _image_reply("Zoom", meta), "png": png, "meta": meta}
    if action == "wait":
        seconds = float(kw.get("duration") or 1.0)
        await driver.wait(seconds)
        return {"text": f"Waited {min(seconds, 10.0):g}s"}

    marker = driver.state_marker()
    targeted = action in ("left_click", "right_click", "double_click", "triple_click", "hover")
    if targeted and ref is None and coordinate is None:
        raise ToolInputError(f"{action} needs a ref or a coordinate")
    if action in ("left_click", "right_click", "double_click", "triple_click"):
        button = "right" if action == "right_click" else "left"
        count = {"double_click": 2, "triple_click": 3}.get(action, 1)
        from src.keys import click_modifiers  # local: keeps keys a leaf module

        did = await driver.click(
            ref=ref,
            coordinate=coordinate,
            button=button,
            count=count,
            modifiers=click_modifiers(kw.get("modifiers")),
        )
    elif action == "type":
        if not isinstance(text, str) or text == "":
            raise ToolInputError("type needs text")
        note = await driver.type_text(text, ref=ref)
        did = f"Typed {len(text)} chars" + (f" into {ref}" if ref else "")
        if note != "ok":
            did += f" — {note.removeprefix('ok — ')}"
    elif action == "key":
        if not isinstance(text, str) or not text.strip():
            raise ToolInputError("key needs text (a key or combo like ctrl+a)")
        repeat = int(kw.get("repeat") or 1)
        await driver.press_keys(text, repeat=repeat)
        did = f"Pressed {text}" + (f" x{repeat}" if repeat > 1 else "")
    elif action == "scroll":
        direction = str(kw.get("scroll_direction") or "down")
        amount = int(kw.get("scroll_amount") or 5)
        await driver.scroll(direction, amount, coordinate=coordinate, ref=ref)
        did = f"Scrolled {direction} {amount}"
    elif action == "scroll_to":
        if not ref:
            raise ToolInputError("scroll_to needs a ref")
        ok = await driver.scroll_to(ref)
        did = f"Scrolled {ref} into view" if ok else f"Could not scroll {ref} into view"
    elif action == "hover":
        await driver.hover(ref=ref, coordinate=coordinate)
        did = f"Hovered {ref or coordinate}"
    else:  # left_click_drag
        start = _coordinate(kw.get("start_coordinate"), "start_coordinate")
        if start is None or coordinate is None:
            raise ToolInputError("left_click_drag needs start_coordinate and coordinate")
        await driver.drag(start, coordinate)
        did = f"Dragged from {start} to {coordinate}"

    changes = await driver.settle(marker)
    if any(c.startswith("Page navigated") or c.startswith("Opened tab") for c in changes):
        await _broadcast_nav(session)
        await _broadcast_tabs(session)
    return {"text": "\n".join([did, *changes])}


# --- forms / files / scripts ---------------------------------------------------------


async def form_input(
    manager: SessionManager, session_id: str, tab_id: int, ref: str, value: str | bool | float
) -> str:
    if not ref:
        raise ToolInputError("form_input needs a ref")
    session = await _on_tab(manager, session_id, tab_id, act=True)
    marker = session.driver.state_marker()
    note = await session.driver.form_input(ref, value)
    shown = "<bool>" if isinstance(value, bool) else str(value)
    did = f"Set {ref} to {shown!r}" if not isinstance(value, bool) else f"Set {ref} to {value}"
    if note != "ok":
        did += f" — {note.removeprefix('ok — ')}"
    changes = await session.driver.settle(marker)
    return "\n".join([did, *changes])


async def javascript(
    manager: SessionManager, session_id: str, tab_id: int, text: str
) -> dict[str, Any]:
    session = await _on_tab(manager, session_id, tab_id, act=True)  # powerful — pause-gated
    out = await session.driver.eval_js(text)
    return {k: _redact(session, str(v)) for k, v in out.items()}


async def upload(
    manager: SessionManager, session_id: str, tab_id: int, ref: str, paths: list[str]
) -> dict[str, Any]:
    session = await _on_tab(manager, session_id, tab_id, act=True)
    await session.driver.upload_files(ref, paths)
    return {"status": "attached", "paths": paths}


async def download(
    manager: SessionManager, session_id: str, tab_id: int, ref: str, dest_path: str
) -> dict[str, Any]:
    session = await _on_tab(manager, session_id, tab_id, act=True)
    return await session.driver.download(ref, dest_path)


async def resize(
    manager: SessionManager, session_id: str, tab_id: int, width: int, height: int
) -> dict[str, Any]:
    session = await _on_tab(manager, session_id, tab_id, act=False)
    await session.driver.set_viewport(int(width), int(height))
    return {"status": "resized", "width": int(width), "height": int(height)}


# --- observability ------------------------------------------------------------------


async def console_messages(
    manager: SessionManager,
    session_id: str,
    tab_id: int,
    *,
    pattern: str | None,
    only_errors: bool,
    limit: int,
    clear: bool,
) -> list[dict[str, Any]]:
    session = await _on_tab(manager, session_id, tab_id, act=False)
    entries = await session.driver.console_messages(
        pattern=pattern or None, only_errors=only_errors, limit=limit, clear=clear
    )
    return [{**e, "text": _redact(session, str(e.get("text", "")))} for e in entries]


async def network_requests(
    manager: SessionManager,
    session_id: str,
    tab_id: int,
    *,
    url_pattern: str | None,
    limit: int,
    clear: bool,
) -> list[dict[str, Any]]:
    session = await _on_tab(manager, session_id, tab_id, act=False)
    return await session.driver.network_requests(
        url_pattern=url_pattern or None, limit=limit, clear=clear
    )


async def network_request(
    manager: SessionManager,
    session_id: str,
    tab_id: int,
    index: int,
    *,
    part: str | None,
    raw_headers: bool,
    reason: str,
    write_file: Callable[[str], Awaitable[str]] | None = None,
) -> dict[str, Any]:
    """One request in full. Reading a BODY bypasses whatever the page chose to
    render — decision 4 of the plan — so it is logged with the stated reason
    and the result is tagged ``source: "network"`` so the agent tells the
    user the value was not on the page."""
    from src.browser_driver import _redact_headers  # the driver owns the header rule

    session = await _on_tab(manager, session_id, tab_id, act=False)
    entry = await session.driver.network_request(int(index))
    if entry is None:
        return {"status": "unknown_index", "index": index}
    out: dict[str, Any] = {
        "status": "ok",
        "index": entry["index"],
        "method": entry["method"],
        "url": entry["url"],
        "http_status": entry["status"],
        "resource_type": entry["resource_type"],
        "content_type": entry.get("content_type", ""),
        "size": entry.get("size", 0),
        "request_headers": entry["request_headers"]
        if raw_headers
        else _redact_headers(entry["request_headers"]),
        "response_headers": entry["response_headers"]
        if raw_headers
        else _redact_headers(entry["response_headers"]),
        "source": "network",
    }
    if part:
        if part not in ("request_body", "response_body"):
            raise ToolInputError("part must be request_body or response_body")
        log.info(
            "network body read chat=%s tab=%s index=%s part=%s reason=%r",
            session.session_id,
            tab_id,
            index,
            part,
            (reason or "")[:200],
        )
        body = entry.get(part)
        if body is None:
            out["body"] = None
            out["note"] = (
                f"no {part.replace('_', ' ')} was captured (not textual, too large, or none sent)"
            )
        else:
            text = _redact(session, str(body))
            out["body_truncated"] = bool(entry.get("body_truncated"))
            if len(text) > BODY_INLINE_CAP and write_file is not None:
                out["body"] = None
                out["path"] = await write_file(text)
                out["note"] = f"body is {len(text)} chars — written to the file at path"
            else:
                out["body"] = text
    return out


# --- extras (not in the extension) -----------------------------------------------------


async def login(
    manager: SessionManager,
    session_id: str,
    portal_id: str,
    portals: dict[str, PortalCred],
    ref: str | None = None,
    tab_id: int | None = None,
) -> dict[str, Any]:
    """Authenticate to a stored portal. The agent passes only ``portal_id`` (and
    optionally where to type); the credential is resolved HERE and injected via
    the driver — it never enters the agent's context, and the returned dict never
    carries it.

    Two modes, and the caller picks:

    * ``ref=None`` — open the portal's stored login URL and fill the form there.
    * ``ref=<username field>`` — fill the form containing that ref on the page
      already open, with no navigation. This is how a caller reaches a login form
      that is not AT the stored URL: many sites keep the form behind a menu, or
      redirect to a separate identity provider, so "always navigate to the stored
      URL first" cannot reach them at all.

    The credential can therefore be typed somewhere other than the stored URL.
    That is a deliberate trade — reachability over a fixed-destination guarantee —
    which is why every injection logs the origin it was typed into, so where a
    stored credential has been used is answerable after the fact."""
    cred = portals.get(portal_id)
    if cred is None:
        return {"status": "unknown_portal", "portal_id": portal_id}
    if not cred.password:
        # A portal CAN be configured with metadata and no stored password: the
        # SPA never gets the password back, so a blank one means "keep what is
        # stored" — and when nothing is stored, ``build_env`` renders the key as
        # "". Attempting the login anyway submits an EMPTY password, which the
        # site rejects while this function reports ``submitted`` — the agent then
        # believes it is signed in, and a real account has eaten a failed-login
        # attempt it did not need to. Report the actual fault instead, before
        # touching the site.
        return {"status": "no_stored_password", "portal_id": portal_id}
    if tab_id is None:
        session = await _session(manager, session_id, act=True)
    else:
        session = await _on_tab(manager, session_id, tab_id, act=True)
    # Every value injected this session is redacted from every later result.
    session.injected_secrets.add(cred.password)
    if ref is None:
        await session.driver.open(cred.login_url)
        filled = await session.driver.fill_login(cred.username, cred.password)
    else:
        filled = await session.driver.fill_login_at(ref, cred.username, cred.password)
    nav = await session.driver.nav_state()
    if not filled:
        # Say which URL was actually tried. "No login form" on its own sends the
        # caller looking at the wrong page — most often the stored URL is a site's
        # home page and the form lives behind a menu.
        return {
            "status": "no_login_form",
            "portal_id": portal_id,
            "url": nav.get("url", ""),
            "tried": "stored_login_url" if ref is None else f"ref:{ref}",
        }
    _log_injection(portal_id, nav.get("url", ""))
    return {"status": "submitted", "portal_id": portal_id, "url": nav.get("url", "")}


def _log_injection(portal_id: str, url: str) -> None:
    """Record WHERE a stored credential was typed — origin only, never the path
    or query (those carry per-session tokens), and never the credential."""
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else "unknown"
    log.info("portal credential injected: portal_id=%s origin=%s", portal_id, origin)


async def wait_for(
    manager: SessionManager,
    session_id: str,
    tab_id: int,
    *,
    text: str | None = None,
    selector: str | None = None,
    url: str | None = None,
    response: str | None = None,
    timeout_ms: int = 8000,
) -> dict[str, Any]:
    session = await _on_tab(manager, session_id, tab_id, act=False)
    ready = await session.driver.wait_for(
        text=text or None,
        selector=selector or None,
        url=url or None,
        response=response or None,
        timeout_ms=int(timeout_ms),
    )
    return {"ready": ready}


async def list_frames(
    manager: SessionManager, session_id: str, tab_id: int
) -> list[dict[str, Any]]:
    session = await _on_tab(manager, session_id, tab_id, act=False)
    return await session.driver.list_frames()


async def switch_frame(
    manager: SessionManager, session_id: str, tab_id: int, target: str
) -> dict[str, Any]:
    session = await _on_tab(manager, session_id, tab_id, act=False)
    is_reset = (target or "").strip().lower() in ("", "main", "0")
    ok = await session.driver.switch_frame(target)
    if is_reset:
        return {"status": "reset", "target": target}
    return {"status": "switched" if ok else "unknown_frame", "target": target}


async def set_dialog_mode(manager: SessionManager, session_id: str, mode: str) -> dict[str, Any]:
    session = await _session(manager, session_id, act=True)  # mutating control
    norm = "accept" if mode == "accept" else "dismiss"
    await session.driver.set_dialog_mode(norm)
    return {"status": "set", "mode": norm}


async def last_dialog(manager: SessionManager, session_id: str, tab_id: int) -> dict[str, Any]:
    session = await _on_tab(manager, session_id, tab_id, act=False)
    info = await session.driver.last_dialog()
    if info is None:
        return {"status": "none"}
    return {"status": "handled", **{k: v for k, v in info.items() if k != "seq"}}


# --- batch + recipes: several calls behind ONE round trip --------------------------


def _bare(name: str) -> str:
    return name.rsplit("__", 1)[-1]


async def run_batch(actions: list[dict[str, Any]], dispatch: Dispatch) -> list[dict[str, Any]]:
    """Run ``actions`` in order through the tool handlers; stop at the first
    error. Every item's result is returned as ``{name, status, result|error}``
    so the model sees exactly where a sequence stopped. Nesting is refused."""
    results: list[dict[str, Any]] = []
    for i, item in enumerate(actions):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            results.append(
                {"name": "?", "status": "error", "error": f"item {i} must be {{name, input}}"}
            )
            break
        name = _bare(item["name"])
        raw = item.get("input")
        inp: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if name == "browser_batch":
            results.append(
                {"name": name, "status": "error", "error": "browser_batch cannot be nested"}
            )
            break
        handler = dispatch.get(name)
        if handler is None:
            results.append({"name": name, "status": "error", "error": f"unknown tool {name!r}"})
            break
        try:
            out = await handler(**inp)
        except TypeError as exc:
            results.append({"name": name, "status": "error", "error": f"bad arguments: {exc}"})
            break
        except Exception as exc:
            results.append(
                {"name": name, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
            )
            break
        results.append({"name": name, "status": "ok", "result": out})
    return results


def _step_refusal(name: str, output: Any) -> str | None:
    """The reason this step says it did not work, or None if it is fine.

    Three tools answer with a value rather than an exception: ``wait_for``
    returns ``ready: False`` on a swallowed timeout, ``login`` a status, and
    ``navigate`` a ``page_state``. With no model in the loop nothing else would
    notice, so the rest of the click-path would run against the wrong page."""
    if name == "wait_for" and isinstance(output, dict) and output.get("ready") is False:
        return "waited for the page to change and it did not"
    if name == "login" and isinstance(output, dict) and output.get("status") != "submitted":
        return f"login did not complete: {output.get('status')}"
    if (
        name == "navigate"
        and isinstance(output, dict)
        and output.get("page_state") not in (None, "ok")
    ):
        return f"navigation landed on a wall: {output.get('page_state')}"
    return None


async def run_recipe(
    recipe: dict[str, Any],
    params: dict[str, str],
    *,
    tab_id: int,
    dispatch: Dispatch,
) -> dict[str, Any]:
    """Execute a saved click-path with NO model between the steps.

    A recipe is a stored batch (see :mod:`src.recipes`): each step runs
    through the same handler a ``browser_batch`` item would, on the tab the
    run started on. A step with a ``target`` is resolved against the tree the
    recipe's own most recent ``read_page`` step returned.

    Returns ``{status, steps_run, extracted, ...}``. On failure it returns the
    step index and reason rather than raising, because a recipe stops being
    valid the moment a site changes, and the caller's next move (fall back to
    driving the browser itself) needs to know WHERE it broke.
    """
    missing = recipes.missing_params(recipe, params)
    if missing:
        return {"status": "missing_params", "missing": missing, "steps_run": 0}

    last_tree: str | None = None
    extracted: list[dict[str, Any]] = []
    for index, step in enumerate(recipe["steps"]):
        name = step["name"]
        try:
            inp = dict(recipes.substitute(step.get("input", {}), params))
            inp.setdefault("tabId", tab_id)
            if "target" in step:
                if last_tree is None:
                    raise recipes.RecipeError(
                        f"step {index} has a target but no read_page step ran before it"
                    )
                inp["ref"] = recipes.resolve_target(last_tree, step["target"])
            handler = dispatch.get(name)
            if handler is None:
                raise recipes.RecipeError(f"recipe step '{name}' has no executor")
            output = await handler(**inp)
        except recipes.RecipeError as exc:
            # A stale descriptor is the EXPECTED end of a recipe's life, not a
            # crash: sites change. Report it precisely so the caller can
            # re-record just this step.
            return _failed(index, name, str(exc), extracted)
        except Exception as exc:
            return _failed(index, name, f"{type(exc).__name__}: {exc}", extracted)

        refusal = _step_refusal(name, output)
        if refusal is not None:
            return _failed(index, name, refusal, extracted)
        if name == "read_page" and isinstance(output, str):
            last_tree = output
        if name in recipes.EXTRACTION_TOOLS:
            extracted.append({"tool": name, "output": output})

    return {"status": "ok", "steps_run": len(recipe["steps"]), "extracted": extracted}


def _failed(index: int, name: str, reason: str, extracted: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "step_failed",
        "failed_at": index,
        "tool": name,
        "reason": reason,
        "steps_run": index,
        "extracted": extracted,
    }

"""Agent-plane operations, independent of the MCP transport.

The MCP tools in :mod:`src.server` are thin handlers; the real work — resolve
the session, honour the human's pause, act on the driver, redact — lives here so
it's unit-testable without spinning up FastMCP. Each function takes a
:class:`SessionManager` and a ``session_id`` and returns plain data / raises a
domain error the handler maps.
"""

from __future__ import annotations

from typing import Any

from src.portal_creds import PortalCred
from src.protocol import BrowserAgentState, BrowserNav, BrowserTabs, BrowserTakeoverRequest
from src.sessions import SessionManager
from src.snapshot import redact_snapshot, snapshot_to_json


class AgentPaused(Exception):
    """Raised when the human has paused the agent on this session. The tool maps
    it to a benign message so the agent waits rather than fighting the human."""


async def _active_session(manager: SessionManager, session_id: str) -> Any:
    session = await manager.get_or_create(session_id)
    if session.agent_paused:
        raise AgentPaused("The human has taken over this browser; wait for them to hand back.")
    session.touch(actor="agent")
    return session


async def _observe_session(manager: SessionManager, session_id: str) -> Any:
    """Like :func:`_active_session` but WITHOUT the pause gate — pure observation
    (read/screenshot/inspect) is safe while the human is driving."""
    session = await manager.get_or_create(session_id)
    session.touch()
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


async def open_url(
    manager: SessionManager, session_id: str, url: str, *, new_tab: bool = False
) -> dict[str, Any]:
    session = await manager.get_or_create(session_id)
    # A fresh navigation is the user redirecting the agent — it RESUMES control.
    # Clear any pause (e.g. a stale takeover request the human never handed back
    # from) so "open <url>" always works instead of the agent locking itself out.
    was_paused = session.agent_paused
    session.agent_paused = False
    session.touch(actor="agent")
    await session.driver.open(url, new_tab=new_tab)
    nav = await _broadcast_nav(session)
    await _broadcast_tabs(session)  # a new tab (or title change) updates the strip
    if was_paused:
        # Tell viewers the agent has control again so the Resume banner clears.
        await session.broadcast(BrowserAgentState(state="idle", last_actor="agent").to_json())
    return nav


async def list_tabs(manager: SessionManager, session_id: str) -> list[dict[str, Any]]:
    session = await manager.get_or_create(session_id)
    session.touch(actor="agent")
    return await session.driver.list_tabs()


async def switch_tab(manager: SessionManager, session_id: str, tab_id: str) -> dict[str, Any]:
    session = await _active_session(manager, session_id)
    ok = await session.driver.switch_tab(tab_id)
    if not ok:
        return {"status": "unknown_tab", "tab_id": tab_id}
    await _broadcast_nav(session)
    await _broadcast_tabs(session)
    return {"status": "switched", "tab_id": tab_id}


async def close_tab(manager: SessionManager, session_id: str, tab_id: str) -> dict[str, Any]:
    session = await _active_session(manager, session_id)
    ok = await session.driver.close_tab(tab_id)
    if not ok:
        return {"status": "unknown_tab", "tab_id": tab_id}
    await _broadcast_nav(session)
    tabs = await _broadcast_tabs(session)
    return {"status": "closed", "tab_id": tab_id, "tabs": tabs}


async def snapshot(
    manager: SessionManager, session_id: str, *, secret_refs: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """Return the redacted, agent-visible element list. Redaction is applied
    HERE, unconditionally — no code path returns a raw password value."""
    session = await _active_session(manager, session_id)
    raw = await session.driver.snapshot()
    return snapshot_to_json(redact_snapshot(raw, secret_refs=secret_refs))


async def login(
    manager: SessionManager,
    session_id: str,
    portal_id: str,
    portals: dict[str, PortalCred],
) -> dict[str, Any]:
    """Authenticate to a stored portal. The agent passes only ``portal_id``; the
    credential is resolved HERE and injected via the driver — it never enters the
    agent's context (and the returned dict never carries it)."""
    cred = portals.get(portal_id)
    if cred is None:
        return {"status": "unknown_portal", "portal_id": portal_id}
    session = await _active_session(manager, session_id)
    await session.driver.open(cred.login_url)
    filled = await session.driver.fill_login(cred.username, cred.password)
    if not filled:
        return {"status": "no_login_form", "portal_id": portal_id}
    nav = await session.driver.nav_state()
    return {"status": "submitted", "portal_id": portal_id, "url": nav.get("url", "")}


async def request_takeover(manager: SessionManager, session_id: str, reason: str) -> dict[str, Any]:
    """The agent hit a challenge (MFA/CAPTCHA/unknown login) and hands control to
    the human: pause the agent and push a take-over request to the viewer(s). Does
    NOT go through ``_active_session`` — asking for help must work even if already
    paused."""
    session = await manager.get_or_create(session_id)
    session.agent_paused = True
    session.touch()
    await session.broadcast(BrowserTakeoverRequest(reason=reason).to_json())
    await session.broadcast(
        BrowserAgentState(state="paused", last_actor=session.last_actor).to_json()
    )
    return {"status": "handed_to_human", "reason": reason}


async def click(manager: SessionManager, session_id: str, ref: str) -> None:
    session = await _active_session(manager, session_id)
    await session.driver.click(ref)


async def type_text(manager: SessionManager, session_id: str, ref: str, text: str) -> None:
    session = await _active_session(manager, session_id)
    await session.driver.type_text(ref, text)


async def scroll(manager: SessionManager, session_id: str, direction: str, amount: int) -> None:
    session = await _active_session(manager, session_id)
    await session.driver.scroll(direction, amount)


# --- reading / understanding (no pause gate) --------------------------------


async def read(manager: SessionManager, session_id: str) -> str:
    session = await _observe_session(manager, session_id)
    return await session.driver.read()


async def find_text(manager: SessionManager, session_id: str, query: str) -> dict[str, Any]:
    session = await _active_session(manager, session_id)  # scrolls to the hit
    return await session.driver.find_text(query)


async def screenshot(manager: SessionManager, session_id: str) -> bytes:
    session = await _observe_session(manager, session_id)
    return await session.driver.screenshot()


async def inspect(manager: SessionManager, session_id: str, ref: str) -> dict[str, Any]:
    session = await _observe_session(manager, session_id)
    return await session.driver.inspect(ref)


async def get_table(
    manager: SessionManager, session_id: str, ref: str | None = None
) -> list[list[list[str]]]:
    session = await _observe_session(manager, session_id)
    return await session.driver.get_table(ref)


async def wait_for(
    manager: SessionManager,
    session_id: str,
    *,
    text: str | None = None,
    selector: str | None = None,
    timeout_ms: int = 8000,
) -> bool:
    session = await _observe_session(manager, session_id)
    return await session.driver.wait_for(text=text, selector=selector, timeout_ms=timeout_ms)


# --- history / reliability (pause-gated navigation) -------------------------


async def go_back(manager: SessionManager, session_id: str) -> None:
    session = await _active_session(manager, session_id)
    await session.driver.go_back()


async def go_forward(manager: SessionManager, session_id: str) -> None:
    session = await _active_session(manager, session_id)
    await session.driver.go_forward()


async def reload(manager: SessionManager, session_id: str) -> None:
    session = await _active_session(manager, session_id)
    await session.driver.reload()


# --- extra actions (pause-gated; approved in chat) --------------------------


async def press_key(manager: SessionManager, session_id: str, key: str) -> None:
    session = await _active_session(manager, session_id)
    await session.driver.press_key(key)


async def select_option(manager: SessionManager, session_id: str, ref: str, value: str) -> None:
    session = await _active_session(manager, session_id)
    await session.driver.select_option(ref, value)


async def upload_file(manager: SessionManager, session_id: str, ref: str, path: str) -> None:
    session = await _active_session(manager, session_id)
    await session.driver.upload_file(ref, path)


async def download(
    manager: SessionManager, session_id: str, ref: str, dest_path: str
) -> dict[str, Any]:
    session = await _active_session(manager, session_id)
    return await session.driver.download(ref, dest_path)


# --- frames (viewing/navigation between open frames — no pause gate) ---------


async def list_frames(manager: SessionManager, session_id: str) -> list[dict[str, Any]]:
    session = await _observe_session(manager, session_id)
    return await session.driver.list_frames()


async def switch_frame(manager: SessionManager, session_id: str, target: str) -> dict[str, Any]:
    session = await _observe_session(manager, session_id)
    is_reset = (target or "").strip().lower() in ("", "main", "0")
    ok = await session.driver.switch_frame(target)
    if is_reset:
        return {"status": "reset", "target": target}
    return {"status": "switched" if ok else "unknown_frame", "target": target}


# --- native dialogs ---------------------------------------------------------


async def set_dialog_mode(manager: SessionManager, session_id: str, mode: str) -> dict[str, Any]:
    session = await _active_session(manager, session_id)  # mutating control
    norm = "accept" if mode == "accept" else "dismiss"
    await session.driver.set_dialog_mode(norm)
    return {"status": "set", "mode": norm}


async def last_dialog(manager: SessionManager, session_id: str) -> dict[str, Any]:
    session = await _observe_session(manager, session_id)
    info = await session.driver.last_dialog()
    if info is None:
        return {"status": "none"}
    return {"status": "handled", **info}


# --- console / network observability (no pause gate) ------------------------


async def console_log(manager: SessionManager, session_id: str) -> list[dict[str, Any]]:
    session = await _observe_session(manager, session_id)
    return await session.driver.console_log()


async def network_log(
    manager: SessionManager,
    session_id: str,
    *,
    url_substring: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    session = await _observe_session(manager, session_id)
    return await session.driver.network_log(url_substring=url_substring, limit=limit)


async def wait_for_response(
    manager: SessionManager, session_id: str, url_substring: str, timeout_ms: int
) -> dict[str, Any]:
    session = await _observe_session(manager, session_id)
    return await session.driver.wait_for_response(url_substring, timeout_ms)


# --- interaction extras -----------------------------------------------------


async def hover(manager: SessionManager, session_id: str, ref: str) -> None:
    session = await _active_session(manager, session_id)
    await session.driver.hover(ref)


async def drag(manager: SessionManager, session_id: str, from_ref: str, to_ref: str) -> None:
    session = await _active_session(manager, session_id)
    await session.driver.drag(from_ref, to_ref)


async def scroll_to(manager: SessionManager, session_id: str, ref: str) -> dict[str, Any]:
    # Pause-gated like scroll()/find_text(): it moves the SHARED viewport, so a
    # paused agent must not yank the view from a human who took over. (Still
    # auto-approved — no chat prompt — since it's benign positioning.)
    session = await _active_session(manager, session_id)
    ok = await session.driver.scroll_to(ref)
    return {"status": "scrolled" if ok else "not_found", "ref": ref}


async def get_options(manager: SessionManager, session_id: str, ref: str) -> list[dict[str, Any]]:
    session = await _observe_session(manager, session_id)
    return await session.driver.get_options(ref)


async def get_links(manager: SessionManager, session_id: str) -> list[dict[str, str]]:
    session = await _observe_session(manager, session_id)
    return await session.driver.get_links()


async def eval_js(manager: SessionManager, session_id: str, js: str) -> dict[str, Any]:
    session = await _active_session(manager, session_id)  # powerful — pause-gated
    return await session.driver.eval_js(js)

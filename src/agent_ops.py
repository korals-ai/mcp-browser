"""Agent-plane operations, independent of the MCP transport.

The MCP tools in :mod:`src.server` are thin handlers; the real work — resolve
the session, honour the human's pause, act on the driver, redact — lives here so
it's unit-testable without spinning up FastMCP. Each function takes a
:class:`SessionManager` and a ``session_id`` and returns plain data / raises a
domain error the handler maps.
"""

from __future__ import annotations

from typing import Any

from src import recipes
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


async def fill_form(
    manager: SessionManager, session_id: str, fields: list[dict[str, str]]
) -> dict[str, Any]:
    """Fill several fields in ONE call, reporting per field.

    Exists because a form costs one model round-trip PER FIELD otherwise, and a
    round-trip is ~2 s — far more than the browser work it wraps. Measured: the
    same form took six calls one field at a time and two when batched.

    Deliberately NOT all-or-nothing. A partial fill is the honest outcome — the
    fields that landed have really landed, and the browser cannot roll them
    back — so each field reports its own result and the caller decides. Failing
    the whole call would hide which half of the form is now populated.
    """
    session = await _active_session(manager, session_id)
    results: list[dict[str, str]] = []
    for field in fields:
        ref, value = field["ref"], field["value"]
        kind = field.get("kind", "text")
        try:
            if kind == "select":
                await session.driver.select_option(ref, value)
            else:
                await session.driver.type_text(ref, value)
        except Exception as exc:
            # The ref, not the value: a value can be a credential, and this
            # string goes into the model's context.
            results.append({"ref": ref, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
            continue
        results.append({"ref": ref, "status": "ok"})
    filled = sum(1 for r in results if r["status"] == "ok")
    return {"filled": filled, "requested": len(fields), "fields": results}


def _step_refusal(tool: str, output: Any) -> str | None:
    """The reason this step says it did not work, or None if it is fine.

    Two tools answer with a value rather than an exception: ``browser_wait_for``
    returns False on a swallowed timeout, and ``browser_login`` returns a status
    dict. Both were previously discarded."""
    if tool == "browser_wait_for" and output is False:
        return "waited for the page to change and it did not"
    if tool == "browser_login" and isinstance(output, dict):
        status = output.get("status")
        if status != "submitted":
            return f"login did not complete: {status}"
    return None


async def run_recipe(
    manager: SessionManager,
    session_id: str,
    recipe: dict[str, Any],
    params: dict[str, str],
    *,
    portals: dict[str, PortalCred] | None = None,
) -> dict[str, Any]:
    """Execute a saved click-path with NO model between the steps.

    This is the whole point of recipes: a model round-trip costs ~2 s and
    re-sends the entire conversation, so a 9-step task pays for its own history
    nine times. Executing the steps here collapses that to one call, measured
    at roughly 20x cheaper on a live site.

    Returns ``{status, steps_run, extracted, ...}``. On failure it returns the
    step index and reason rather than raising, because a recipe stops being
    valid the moment a site changes, and the caller's next move (fall back to
    driving the browser itself) needs to know WHERE it broke.
    """
    missing = recipes.missing_params(recipe, params)
    if missing:
        return {"status": "missing_params", "missing": missing, "steps_run": 0}

    session = await _active_session(manager, session_id)
    elements: list[dict[str, Any]] = []
    extracted: list[dict[str, Any]] = []

    for index, step in enumerate(recipe["steps"]):
        tool = step["tool"]
        try:
            # Inside the try: an undeclared "param:" reference raises here, and
            # missing_params above cannot catch it (it only checks DECLARED
            # names). Outside, it escaped as a bare exception with no step index
            # after earlier steps had already navigated and clicked.
            args = {k: recipes.substitute(v, params) for k, v in step.get("args", {}).items()}
            if "target" in step:
                args["ref"] = recipes.resolve_target(elements, step["target"])
            if tool == "browser_fill_form":
                args["fields"] = [
                    {
                        "ref": recipes.resolve_target(elements, field["target"]),
                        "value": recipes.substitute(field.get("value", ""), params),
                        "kind": field.get("kind", "text"),
                    }
                    for field in step.get("fields", [])
                ]
            output = await _run_recipe_step(manager, session_id, session, tool, args, portals or {})
        except recipes.RecipeError as exc:
            # A stale descriptor is the EXPECTED end of a recipe's life, not a
            # crash: sites change. Report it precisely so the caller can
            # re-record just this step.
            return {
                "status": "step_failed",
                "failed_at": index,
                "tool": tool,
                "reason": str(exc),
                "steps_run": index,
                "extracted": extracted,
            }
        except Exception as exc:
            return {
                "status": "step_failed",
                "failed_at": index,
                "tool": tool,
                "reason": f"{type(exc).__name__}: {exc}",
                "steps_run": index,
                "extracted": extracted,
            }

        # A step that REPORTS failure instead of raising it. With no model in
        # the loop nothing else notices, so the rest of the click-path would run
        # against the wrong page and the recipe would still return "ok" —
        # handing back a sign-in wall, or a form submitted while logged out.
        refusal = _step_refusal(tool, output)
        if refusal is not None:
            return {
                "status": "step_failed",
                "failed_at": index,
                "tool": tool,
                "reason": refusal,
                "steps_run": index,
                "extracted": extracted,
            }

        if tool == "browser_snapshot":
            elements = output if isinstance(output, list) else []
        if tool in recipes.EXTRACTION_TOOLS:
            extracted.append({"tool": tool, "output": output})

    return {
        "status": "ok",
        "steps_run": len(recipe["steps"]),
        "extracted": extracted,
    }


async def _run_recipe_step(
    manager: SessionManager,
    session_id: str,
    session: Any,
    tool: str,
    args: dict[str, Any],
    portals: dict[str, PortalCred],
) -> Any:
    """Dispatch one allowlisted recipe step to the driver.

    An explicit mapping, not ``getattr(driver, name)``: the allowlist in
    :mod:`src.recipes` is only a real boundary if nothing here can reach a
    method it does not name."""
    driver = session.driver
    if tool == "browser_open":
        await driver.open(args["url"])
        return None
    if tool == "browser_snapshot":
        raw = await driver.snapshot()
        return snapshot_to_json(redact_snapshot(raw))
    if tool == "browser_click":
        await driver.click(args["ref"])
        return None
    if tool == "browser_type":
        await driver.type_text(args["ref"], args["text"])
        return None
    if tool == "browser_fill_form":
        for field in args["fields"]:
            if field.get("kind") == "select":
                await driver.select_option(field["ref"], field["value"])
            else:
                await driver.type_text(field["ref"], field["value"])
        return None
    if tool == "browser_select_option":
        await driver.select_option(args["ref"], args["value"])
        return None
    if tool == "browser_press_key":
        await driver.press_key(args["key"])
        return None
    if tool == "browser_scroll":
        await driver.scroll(args.get("direction", "down"), int(args.get("amount", 600)))
        return None
    if tool == "browser_wait_for":
        return await driver.wait_for(
            text=args.get("text") or None,
            selector=args.get("selector") or None,
            timeout_ms=int(args.get("timeout_ms", 8000)),
        )
    if tool == "browser_read":
        return await driver.read()
    if tool == "browser_get_table":
        return await driver.get_table(args.get("ref") or None)
    if tool == "browser_get_links":
        return await driver.get_links()
    if tool == "browser_find":
        return await driver.find_text(args["query"])
    if tool == "browser_login":
        # Reuse the audited login op rather than re-implementing it here: it is
        # what keeps the password out of the agent's context, and a second copy
        # of that path is a second place for it to leak.
        return await login(manager, session_id, args["portal_id"], portals)
    if tool == "browser_back":
        await driver.go_back()
        return None
    if tool == "browser_forward":
        await driver.go_forward()
        return None
    if tool == "browser_reload":
        await driver.reload()
        return None
    # Unreachable while the allowlist and this mapping agree; a loud failure if
    # someone adds a tool to one and forgets the other.
    raise recipes.RecipeError(f"recipe step '{tool}' has no executor")


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

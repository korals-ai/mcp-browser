"""Wire types for the co-browse plane (browser pod <-> SPA viewer).

The co-browse WebSocket is the HUMAN view+input plane — distinct from the agent
control plane (the MCP tools in :mod:`src.server`, dialed by the in-pod broker).
Both act on ONE shared Chromium session; this module only declares the frames
the two ends of the ``/cobrowse`` socket exchange.

Kept as plain dataclasses with an explicit ``type`` discriminator (mirrors the
SPA's ``agentBridge`` union) so both the encode side here and the TS decoder in
``apps/chat-ui`` narrow on the same string. See
docs/plan/20260706T014659Z-cobrowse-agent-poc.md §5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# --- browser pod -> viewer ------------------------------------------------


@dataclass(frozen=True)
class BrowserFrame:
    """One screencast frame (CDP ``Page.screencastFrame``), JPEG as base64.

    ``frame_id`` is echoed back in :class:`BrowserFrameAck` so the pod can ack
    the CDP frame (CDP requires an ack before it sends the next one — that ack
    IS the backpressure) and the viewer can drop stale frames.
    """

    frame_id: int
    data: str  # base64 JPEG
    width: int
    height: int
    device_scale: float
    type: Literal["browser_frame"] = "browser_frame"

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "frame_id": self.frame_id,
            "data": self.data,
            "w": self.width,
            "h": self.height,
            "deviceScale": self.device_scale,
        }


@dataclass(frozen=True)
class BrowserNav:
    """Navigation state, pushed on load so the viewer can show the URL/title."""

    url: str
    title: str
    can_go_back: bool
    can_go_forward: bool
    type: Literal["browser_nav"] = "browser_nav"

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "url": self.url,
            "title": self.title,
            "canGoBack": self.can_go_back,
            "canGoForward": self.can_go_forward,
        }


@dataclass(frozen=True)
class BrowserTakeoverRequest:
    """The agent hit a challenge it can't handle (MFA / CAPTCHA / unknown login)
    and is asking the human to take over. The pod pauses the agent and pushes
    this so the viewer raises a take-over banner."""

    reason: str
    type: Literal["browser_takeover_request"] = "browser_takeover_request"

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type, "reason": self.reason}


@dataclass(frozen=True)
class BrowserAgentState:
    """Whether the agent is currently acting / paused / idle on this session.

    Drives the "who acted last" hint and the pause indicator. ``last_actor`` is
    "agent" or "human" (or None before either has acted).
    """

    state: Literal["acting", "paused", "idle"]
    last_actor: str | None = None
    type: Literal["browser_agent_state"] = "browser_agent_state"

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type, "state": self.state, "lastActor": self.last_actor}


@dataclass(frozen=True)
class BrowserTabs:
    """The open tabs and which is active — drives the viewer's tab strip. Pushed
    on connect and whenever the tab set / active tab changes."""

    tabs: list[dict[str, Any]]  # [{id, title, url, active}]
    type: Literal["browser_tabs"] = "browser_tabs"

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type, "tabs": self.tabs}


# The CSS cursor keywords the pod is allowed to mirror to a viewer. Deliberately
# a closed allowlist (defense in depth — the viewer validates it again): the pod
# only forwards a known-safe keyword, never an arbitrary string from a page's
# stylesheet. "auto"/"default"/"none"/unknown collapse to "" (the viewer keeps
# its resting crosshair), so a hostile or exotic cursor can never blank or hijack
# the human's pointer.
CURSOR_ALLOWLIST: frozenset[str] = frozenset(
    {
        "pointer",
        "text",
        "wait",
        "progress",
        "help",
        "move",
        "grab",
        "grabbing",
        "crosshair",
        "not-allowed",
        "ns-resize",
        "ew-resize",
        "nesw-resize",
        "nwse-resize",
        "col-resize",
        "row-resize",
        "zoom-in",
        "zoom-out",
        "all-scroll",
    }
)


def normalize_cursor(raw: str) -> str:
    """Reduce a computed CSS ``cursor`` value to a single allowlisted keyword, or
    ``""`` if it isn't one we mirror.

    getComputedStyle can return a fallback list with url() images
    (``url(x) 4 4, pointer``); we take the final keyword (the CSS fallback) and
    drop anything with a custom image or outside the allowlist — the viewer then
    keeps its default crosshair rather than trusting a page-supplied cursor."""
    keyword = raw.split(",")[-1].strip().lower()
    return keyword if keyword in CURSOR_ALLOWLIST else ""


@dataclass(frozen=True)
class BrowserCursor:
    """The CSS cursor the remote page shows at a viewer's pointer, pushed so the
    viewer can mirror it (a hand over links, an I-beam over text, …). Best-effort
    and PER-VIEWER — a missing or failed probe simply leaves the last cursor up,
    never affecting the screencast or input. ``cursor`` is always an allowlisted
    keyword (see :func:`normalize_cursor`)."""

    cursor: str
    type: Literal["browser_cursor"] = "browser_cursor"

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type, "cursor": self.cursor}


# --- viewer -> browser pod ------------------------------------------------

# The CDP input payload the viewer sends is passed through to the driver mostly
# verbatim; we only validate the discriminator + the coarse event family here
# and let the driver map fields to ``Input.dispatch*`` (v1). Keeping it a loose
# dict avoids re-declaring CDP's whole input schema on the wire.
InputEvent = Literal["mouse", "key", "wheel", "paste"]


@dataclass(frozen=True)
class BrowserInput:
    """A human input event to replay onto the shared session (v1 uses this)."""

    event: InputEvent
    fields: dict[str, Any] = field(default_factory=dict)
    type: Literal["browser_input"] = "browser_input"


@dataclass(frozen=True)
class BrowserFrameAck:
    """Viewer confirms a frame was painted; unblocks the next CDP frame."""

    frame_id: int
    type: Literal["browser_frame_ack"] = "browser_frame_ack"


ControlAction = Literal["pause_agent", "resume_agent", "takeover", "release"]


@dataclass(frozen=True)
class BrowserControl:
    """Human control signal (pause the agent, take over, hand back)."""

    action: ControlAction
    type: Literal["browser_control"] = "browser_control"


@dataclass(frozen=True)
class BrowserSwitchTab:
    """Human clicked a tab in the strip — make it active."""

    tab_id: str
    type: Literal["browser_switch_tab"] = "browser_switch_tab"


@dataclass(frozen=True)
class BrowserCloseTab:
    """Human clicked the close (x) button on a tab."""

    tab_id: str
    type: Literal["browser_close_tab"] = "browser_close_tab"


@dataclass(frozen=True)
class BrowserNewTab:
    """Human clicked "new tab" — open a blank tab and make it active."""

    type: Literal["browser_new_tab"] = "browser_new_tab"


@dataclass(frozen=True)
class BrowserNavigate:
    """Human typed a URL in the address bar — navigate the ACTIVE tab there."""

    url: str
    type: Literal["browser_navigate"] = "browser_navigate"


@dataclass(frozen=True)
class BrowserEndSession:
    """Human clicked "End" — close the Chromium session on the pod (free the
    browser). The agent's next browser tool call starts a fresh session."""

    type: Literal["browser_end_session"] = "browser_end_session"


@dataclass(frozen=True)
class BrowserResize:
    """The human's viewer panel changed size — resize the remote viewport to match
    so the page fills the panel instead of letterboxing. ``width``/``height`` are
    CSS pixels (the device-scale multiplier stays server-side)."""

    width: int
    height: int
    type: Literal["browser_resize"] = "browser_resize"


ClientMessage = (
    BrowserInput
    | BrowserFrameAck
    | BrowserControl
    | BrowserSwitchTab
    | BrowserCloseTab
    | BrowserNewTab
    | BrowserNavigate
    | BrowserEndSession
    | BrowserResize
)


def parse_client_message(msg: dict[str, Any]) -> ClientMessage:
    """Decode one viewer->pod JSON message into its dataclass.

    Raises ``ValueError`` on an unknown / malformed type so the WS handler can
    reject the frame instead of silently mis-routing it.
    """
    kind = msg.get("type")
    if kind == "browser_input":
        event = msg.get("event")
        if event not in ("mouse", "key", "wheel", "paste"):
            raise ValueError(f"bad input event: {event!r}")
        fields = msg.get("fields")
        return BrowserInput(event=event, fields=fields if isinstance(fields, dict) else {})
    if kind == "browser_frame_ack":
        fid = msg.get("frame_id")
        if not isinstance(fid, int):
            raise ValueError("frame_ack missing int frame_id")
        return BrowserFrameAck(frame_id=fid)
    if kind == "browser_control":
        action = msg.get("action")
        if action not in ("pause_agent", "resume_agent", "takeover", "release"):
            raise ValueError(f"bad control action: {action!r}")
        return BrowserControl(action=action)
    if kind == "browser_switch_tab":
        tid = msg.get("tab_id")
        if not isinstance(tid, str) or not tid:
            raise ValueError("switch_tab missing tab_id")
        return BrowserSwitchTab(tab_id=tid)
    if kind == "browser_close_tab":
        tid = msg.get("tab_id")
        if not isinstance(tid, str) or not tid:
            raise ValueError("close_tab missing tab_id")
        return BrowserCloseTab(tab_id=tid)
    if kind == "browser_new_tab":
        return BrowserNewTab()
    if kind == "browser_end_session":
        return BrowserEndSession()
    if kind == "browser_resize":
        width, height = msg.get("width"), msg.get("height")
        # bool is an int subclass — exclude it so a stray true/false can't slip
        # through as 1/0. The driver clamps to sane bounds; here we only reject a
        # non-positive or non-integer size outright.
        if (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
        ):
            raise ValueError(f"resize needs positive int width/height: {width!r}x{height!r}")
        return BrowserResize(width=width, height=height)
    if kind == "browser_navigate":
        url = msg.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("navigate missing url")
        return BrowserNavigate(url=normalize_url(url))
    raise ValueError(f"unknown client message type: {kind!r}")


def normalize_url(raw: str) -> str:
    """Turn what a human types in the address bar into a loadable URL: add a
    scheme when there's none (``example.com`` -> ``https://example.com``), leaving
    already-schemed inputs (http/https/about/file) untouched."""
    url = raw.strip()
    if "://" in url or url.startswith("about:"):
        return url
    return "https://" + url

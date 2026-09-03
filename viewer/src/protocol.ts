// Co-browse WS protocol (browser tab <-> dispatcher <-> browser tool pod).
//
// Mirrors the dataclasses in apps/workspace-tools/browser/src/protocol.py
// (the pod's `to_json` shapes) and the passthrough in
// apps/dispatcher/src/services/cobrowse_proxy.py (a transparent tunnel — every
// frame here lands on / arrives from the browser tool pod unchanged).
//
// v0 is watch-only: the pod streams `browser_frame` / `browser_nav`; the viewer
// only sends `browser_frame_ack` (CDP backpressure). Input/control frames are
// declared for v1.

// --- browser pod -> viewer ---------------------------------------------------

export interface BrowserFrame {
  type: "browser_frame";
  frame_id: number;
  data: string; // base64 JPEG
  w: number;
  h: number;
  deviceScale: number;
}

export interface BrowserNav {
  type: "browser_nav";
  url: string;
  title: string;
  canGoBack: boolean;
  canGoForward: boolean;
}

export interface BrowserAgentState {
  type: "browser_agent_state";
  state: "acting" | "paused" | "idle";
  lastActor: string | null;
}

export interface BrowserTakeoverRequest {
  type: "browser_takeover_request";
  reason: string;
}

export interface BrowserTabInfo {
  id: string;
  title: string;
  url: string;
  active: boolean;
}

export interface BrowserTabs {
  type: "browser_tabs";
  tabs: BrowserTabInfo[];
}

// The CSS cursor the remote page shows under this viewer's pointer, so the
// canvas can mirror it (hand over links, I-beam over text, …). Best-effort and
// cosmetic — `cursor` is one of the pod's allowlisted keywords (empty means "no
// specific cursor", i.e. keep the resting default). See the pod's
// normalize_cursor + CURSOR_ALLOWLIST in protocol.py.
export interface BrowserCursor {
  type: "browser_cursor";
  cursor: string;
}

// The dispatcher forwards its own transport errors (e.g. the tool pod is still
// warming) as the same `{type:"error"}` shape the SDK bridge uses.
export interface CoBrowseError {
  type: "error";
  code: string;
  message: string;
  fatal: boolean;
}

export type CoBrowseServerFrame =
  | BrowserFrame
  | BrowserNav
  | BrowserAgentState
  | BrowserTakeoverRequest
  | BrowserTabs
  | BrowserCursor
  | CoBrowseError;

// --- viewer -> browser pod ---------------------------------------------------

export interface BrowserFrameAck {
  type: "browser_frame_ack";
  frame_id: number;
}

// v1 — human input + control (declared now so the viewer/pod agree on shape).
// `paste` carries THIS tab's clipboard text in `fields.text`; the pod replays it
// with CDP `Input.insertText`. Replaying Ctrl+V as key events instead would paste
// the REMOTE Chromium's clipboard, which is always empty. The pod drops text over
// MAX_PASTE_CHARS (apps/workspace-tools/browser/src/input_map.py) — kept in sync
// here so the viewer never sends a frame it knows will be refused.
export interface BrowserInput {
  type: "browser_input";
  event: "mouse" | "key" | "wheel" | "paste";
  fields: Record<string, unknown>;
}

export const MAX_PASTE_CHARS = 100_000;

export interface BrowserControl {
  type: "browser_control";
  action: "pause_agent" | "resume_agent" | "takeover" | "release";
}

export interface BrowserSwitchTab {
  type: "browser_switch_tab";
  tab_id: string;
}

export interface BrowserCloseTab {
  type: "browser_close_tab";
  tab_id: string;
}

export interface BrowserNewTab {
  type: "browser_new_tab";
}

export interface BrowserNavigate {
  type: "browser_navigate";
  url: string;
}

export interface BrowserEndSession {
  type: "browser_end_session";
}

// The viewer panel changed size — ask the pod to resize the remote viewport to
// match so the page fills the panel (no letterbox). CSS pixels; the pod keeps
// its own device-scale multiplier for the screencast.
export interface BrowserResize {
  type: "browser_resize";
  width: number;
  height: number;
}

export type CoBrowseClientFrame =
  | BrowserFrameAck
  | BrowserInput
  | BrowserControl
  | BrowserSwitchTab
  | BrowserCloseTab
  | BrowserNewTab
  | BrowserNavigate
  | BrowserEndSession
  | BrowserResize;

// Narrowing decoder: returns the typed frame or null for anything unrecognised,
// so the viewer can skip a malformed message instead of throwing in onmessage.
export function decodeServerFrame(raw: unknown): CoBrowseServerFrame | null {
  if (typeof raw !== "object" || raw === null) return null;
  const msg = raw as Record<string, unknown>;
  switch (msg.type) {
    case "browser_frame":
      if (typeof msg.data !== "string" || typeof msg.frame_id !== "number")
        return null;
      return {
        type: "browser_frame",
        frame_id: msg.frame_id,
        data: msg.data,
        w: typeof msg.w === "number" ? msg.w : 0,
        h: typeof msg.h === "number" ? msg.h : 0,
        deviceScale: typeof msg.deviceScale === "number" ? msg.deviceScale : 1,
      };
    case "browser_nav":
      return {
        type: "browser_nav",
        url: typeof msg.url === "string" ? msg.url : "",
        title: typeof msg.title === "string" ? msg.title : "",
        canGoBack: Boolean(msg.canGoBack),
        canGoForward: Boolean(msg.canGoForward),
      };
    case "browser_agent_state":
      return {
        type: "browser_agent_state",
        state:
          msg.state === "acting" || msg.state === "paused" ? msg.state : "idle",
        lastActor: typeof msg.lastActor === "string" ? msg.lastActor : null,
      };
    case "browser_takeover_request":
      return {
        type: "browser_takeover_request",
        reason: typeof msg.reason === "string" ? msg.reason : "",
      };
    case "browser_tabs":
      return {
        type: "browser_tabs",
        tabs: Array.isArray(msg.tabs)
          ? (msg.tabs as unknown[]).map((t) => {
              const o = (t ?? {}) as Record<string, unknown>;
              return {
                id: typeof o.id === "string" ? o.id : "",
                title: typeof o.title === "string" ? o.title : "",
                url: typeof o.url === "string" ? o.url : "",
                active: Boolean(o.active),
              };
            })
          : [],
      };
    case "browser_cursor":
      return {
        type: "browser_cursor",
        cursor: typeof msg.cursor === "string" ? msg.cursor : "",
      };
    case "error":
      return {
        type: "error",
        code: typeof msg.code === "string" ? msg.code : "error",
        message: typeof msg.message === "string" ? msg.message : "",
        fatal: Boolean(msg.fatal),
      };
    default:
      return null;
  }
}

// The agent's co-browse MCP tools arrive in the chat timeline as tool items
// named `mcp__workspace-tool-browser__<tool>` — MCP tools are namespaced by the
// roster/server name (the sidecar name), not the FastMCP internal name. The SPA
// keys off this to auto-open the viewer, so the prefix MUST match the sidecar
// name exactly or the Co-browse tab never appears.
export function isCoBrowseToolName(name: string): boolean {
  return name.startsWith("mcp__workspace-tool-browser__");
}

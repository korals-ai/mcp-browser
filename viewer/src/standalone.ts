// A viewer for the co-browse plane that needs no framework and no build step at
// run time: the server serves the bundle, the browser opens it, and you watch
// the agent work.
//
// It is deliberately thin. Everything that decides what a frame MEANS lives in
// protocol.ts / paint.ts / input.ts, which this app shares byte-for-byte with
// the React viewer in the platform's own UI — so the two cannot disagree about
// the wire format, the object-contain input mapping, or the stale-frame guard.
// This file is only the shell: a canvas, an address bar, and the event wiring.

import {
  clientToRemote,
  isPasteChord,
  isPrintable,
  keyFrame,
  mouseButton,
  mouseFrame,
  pasteFrame,
  resizeFrame,
  wheelFrame,
} from "./input";
import { paintFrame } from "./paint";
import { type CoBrowseClientFrame, decodeServerFrame } from "./protocol";

const $ = <T extends HTMLElement>(id: string): T => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing element #${id}`);
  return el as T;
};

const canvas = $<HTMLCanvasElement>("screen");
const addressBar = $<HTMLInputElement>("address");
const statusEl = $<HTMLSpanElement>("status");

// The session id must match the ``?chat_id=`` the agent's MCP client sends: the
// agent plane REJECTS a call without one rather than merging every caller into a
// single profile, so there is no default that silently pairs them up. "local" is
// simply the value the README tells both sides to use.
const sessionId =
  new URLSearchParams(location.search).get("session") ?? "local";

// The frame METADATA size, not the JPEG's pixel size — CDP input coordinates are
// CSS pixels. paint.ts explains why the two differ under deviceScaleFactor.
let frameW = 0;
let frameH = 0;
let ws: WebSocket | null = null;
let reconnectDelay = 500;

function setStatus(text: string, live: boolean): void {
  statusEl.textContent = text;
  statusEl.dataset.live = String(live);
}

function send(frame: CoBrowseClientFrame): void {
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(frame));
}

function connect(): void {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/cobrowse/${sessionId}`);
  setStatus("connecting…", false);

  ws.onopen = () => {
    reconnectDelay = 500;
    setStatus("live", true);
    pushSize();
  };

  ws.onmessage = (ev) => {
    let raw: unknown;
    try {
      raw = JSON.parse(ev.data as string);
    } catch {
      return;
    }
    const frame = decodeServerFrame(raw);
    if (!frame) return;

    switch (frame.type) {
      case "browser_frame":
        frameW = frame.w;
        frameH = frame.h;
        paintFrame(canvas, frame.data, frame.w, frame.h);
        // Acking is backpressure, not bookkeeping: the pod withholds the next
        // screencast frame until the current one is acked, so skipping this
        // stalls the stream after one frame.
        send({ type: "browser_frame_ack", frame_id: frame.frame_id });
        break;
      case "browser_nav":
        if (document.activeElement !== addressBar) addressBar.value = frame.url;
        document.title = frame.title || "co-browse";
        break;
      case "browser_cursor":
        canvas.style.cursor = frame.cursor || "default";
        break;
      case "error":
        setStatus(frame.message, false);
        break;
    }
  };

  // Both paths reconnect: a co-browse session outliving a viewer reload is the
  // normal case, and the agent keeps working while nobody is watching.
  ws.onclose = () => {
    setStatus("reconnecting…", false);
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 10_000);
  };
  ws.onerror = () => ws?.close();
}

function pushSize(): void {
  const r = canvas.getBoundingClientRect();
  if (r.width > 0 && r.height > 0) send(resizeFrame(r.width, r.height));
}

function remote(e: MouseEvent): { x: number; y: number } {
  return clientToRemote(
    e.clientX,
    e.clientY,
    canvas.getBoundingClientRect(),
    frameW,
    frameH,
  );
}

canvas.addEventListener("mousemove", (e) => {
  const { x, y } = remote(e);
  send(mouseFrame("move", x, y));
});
canvas.addEventListener("mousedown", (e) => {
  canvas.focus();
  const { x, y } = remote(e);
  send(mouseFrame("down", x, y, mouseButton(e.button), e.detail || 1));
});
canvas.addEventListener("mouseup", (e) => {
  const { x, y } = remote(e);
  send(mouseFrame("up", x, y, mouseButton(e.button), e.detail || 1));
});
canvas.addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    const { x, y } = remote(e);
    send(wheelFrame(x, y, e.deltaX, e.deltaY));
  },
  { passive: false },
);
// The remote page owns the context menu; a local one would cover the canvas.
canvas.addEventListener("contextmenu", (e) => e.preventDefault());

canvas.addEventListener("keydown", (e) => {
  // Let the browser's own paste event carry the clipboard — see isPasteChord.
  if (isPasteChord(e)) return;
  e.preventDefault();
  send(keyFrame("down", e.key, e.code, isPrintable(e.key) ? e.key : undefined));
});
canvas.addEventListener("keyup", (e) => {
  if (isPasteChord(e)) return;
  e.preventDefault();
  send(keyFrame("up", e.key, e.code));
});
canvas.addEventListener("paste", (e) => {
  const text = e.clipboardData?.getData("text") ?? "";
  const frame = pasteFrame(text);
  if (frame) send(frame);
  e.preventDefault();
});

// No back/forward/reload controls: the protocol has no frame for them
// (BrowserControl carries agent pause/takeover only, and browser_navigate takes
// a URL). Inventing them here would mean inventing wire format the server does
// not implement. The address bar is the whole navigation surface, same as the
// platform's own viewer.
addressBar.addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const url = addressBar.value.trim();
  if (url) send({ type: "browser_navigate", url });
  canvas.focus();
});

new ResizeObserver(pushSize).observe(canvas);
connect();

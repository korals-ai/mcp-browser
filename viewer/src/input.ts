// Pure helpers for the co-browse input plane (v1): map a browser pointer event
// to the remote Chromium's viewport coordinates, and build the browser_input
// frames the pod maps to CDP. Kept out of the component so the fiddly
// object-contain inverse is unit-testable.

import {
  MAX_PASTE_CHARS,
  type BrowserControl,
  type BrowserInput,
  type BrowserResize,
} from "./protocol";

export interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
}

// Inverse of CSS `object-contain`: the canvas paints a frameW×frameH image
// scaled to fit inside its element rect (preserving aspect, letterboxed and
// centered). Given a client point, recover the point in remote-viewport pixels,
// clamped to the frame so an off-image drag doesn't send out-of-bounds coords.
export function clientToRemote(
  clientX: number,
  clientY: number,
  rect: Rect,
  frameW: number,
  frameH: number,
): { x: number; y: number } {
  if (frameW <= 0 || frameH <= 0 || rect.width <= 0 || rect.height <= 0) {
    return { x: 0, y: 0 };
  }
  const scale = Math.min(rect.width / frameW, rect.height / frameH);
  const contentW = frameW * scale;
  const contentH = frameH * scale;
  const offsetX = rect.left + (rect.width - contentW) / 2;
  const offsetY = rect.top + (rect.height - contentH) / 2;
  const x = (clientX - offsetX) / scale;
  const y = (clientY - offsetY) / scale;
  return {
    x: clamp(x, 0, frameW),
    y: clamp(y, 0, frameH),
  };
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

const BUTTONS = ["left", "middle", "right"] as const;

export function mouseButton(button: number): "left" | "middle" | "right" {
  return BUTTONS[button] ?? "left";
}

export function mouseFrame(
  kind: "move" | "down" | "up",
  x: number,
  y: number,
  button: "left" | "middle" | "right" = "left",
  clickCount = 1,
): BrowserInput {
  return {
    type: "browser_input",
    event: "mouse",
    fields: { kind, x, y, button, clickCount },
  };
}

export function wheelFrame(
  x: number,
  y: number,
  deltaX: number,
  deltaY: number,
): BrowserInput {
  return {
    type: "browser_input",
    event: "wheel",
    fields: { x, y, deltaX, deltaY },
  };
}

export function keyFrame(
  kind: "down" | "up" | "char",
  key: string,
  code: string,
  text?: string,
): BrowserInput {
  const fields: Record<string, unknown> = { kind, key, code };
  if (text) fields.text = text;
  return { type: "browser_input", event: "key", fields };
}

export function controlFrame(action: BrowserControl["action"]): BrowserControl {
  return { type: "browser_control", action };
}

// Round to whole CSS pixels — CDP viewport dims are integers, and sub-pixel
// jitter from a ResizeObserver would otherwise send a stream of no-op resizes.
export function resizeFrame(width: number, height: number): BrowserResize {
  return {
    type: "browser_resize",
    width: Math.round(width),
    height: Math.round(height),
  };
}

// A printable key produces a CDP `char` event (inserts the text); non-printable
// keys (Enter, Backspace, arrows, modifiers) are keyDown/keyUp only.
export function isPrintable(key: string): boolean {
  return key.length === 1;
}

// The paste chord is handled by the browser's own `paste` event (which is what
// carries the clipboard text), so the keydown must NOT be preventDefault'd or
// forwarded — a forwarded Ctrl+V would paste the remote's empty clipboard, and
// preventing the default suppresses the paste event we actually want.
export function isPasteChord(e: {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
}): boolean {
  return (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v";
}

// Returns null when there is nothing safe to send: an empty clipboard, or text
// past the limit the pod enforces (dropping it here keeps the two ends agreeing
// instead of sending a frame the pod will refuse).
export function pasteFrame(text: string): BrowserInput | null {
  if (!text || text.length > MAX_PASTE_CHARS) return null;
  return { type: "browser_input", event: "paste", fields: { text } };
}

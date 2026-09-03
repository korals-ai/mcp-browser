// Paint one base64 JPEG screencast frame onto the co-browse canvas.
//
// Decoding runs OFF the main thread via createImageBitmap: at 15 fps (see
// _MIN_FRAME_INTERVAL_S in browser_driver.py) a synchronous main-thread JPEG
// decode per frame stalls the UI thread and janks scrolling. createImageBitmap
// decodes on a browser-managed worker thread and hands back a GPU-uploadable
// bitmap that drawImage blits cheaply. Browsers without it (very old Safari)
// fall back to the classic <img> decode.
//
// The canvas BACKING STORE is sized to the JPEG's native pixel size, NOT the
// frame metadata size: the remote browser renders at deviceScaleFactor 2, so
// the JPEG carries 2x the pixels of the CSS-pixel metadata (2560x1600 vs
// 1280x800). Sizing the backing store to the metadata would silently downscale
// that resolution away and the picture stays blurry on HiDPI displays. Input
// mapping must KEEP using the metadata size (CoBrowseViewer's frameSizeRef) —
// CDP input coordinates are CSS pixels, and clientToRemote only needs the
// aspect ratio to match, which it does at any device scale.
//
// Kept out of the component so the sizing invariant is unit-testable
// (tests/dom/cobrowsePaint.test.ts) and the WS effect stays lean.

// Per-canvas paint sequence. Decoding is async, so a frame whose decode
// finishes AFTER a newer frame's must not overwrite it — a stale-frame rewind
// is visible jitter at 15 fps. We stamp each paint with a monotonic call-order
// sequence and drop any decode that lands after a newer one already painted.
// Keyed by the canvas element so it survives a reconnect (frame_ids may restart
// on a new session; the call-order counter never does).
const paintSeq = new WeakMap<
  HTMLCanvasElement,
  { next: number; painted: number }
>();

export function paintFrame(
  canvas: HTMLCanvasElement | null,
  base64Jpeg: string,
  metaW: number,
  metaH: number,
): void {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  let state = paintSeq.get(canvas);
  if (!state) {
    state = { next: 0, painted: 0 };
    paintSeq.set(canvas, state);
  }
  const seq = ++state.next;
  const seqState = state;

  decodeJpeg(base64Jpeg)
    .then((src) => {
      // A newer frame already painted while this one decoded — drop the stale one.
      if (seq <= seqState.painted) {
        release(src);
        return;
      }
      seqState.painted = seq;
      const { w, h } = intrinsicSize(src);
      const cw = w > 0 ? w : metaW;
      const ch = h > 0 ? h : metaH;
      if (cw > 0 && ch > 0) {
        canvas.width = cw;
        canvas.height = ch;
      }
      ctx.drawImage(src, 0, 0, canvas.width, canvas.height);
      release(src);
    })
    .catch(() => {
      // Decode failed (corrupt frame) — skip it, keep the last good frame up.
    });
}

type DecodedFrame = ImageBitmap | HTMLImageElement;

async function decodeJpeg(base64Jpeg: string): Promise<DecodedFrame> {
  if (typeof createImageBitmap === "function") {
    return createImageBitmap(base64ToJpegBlob(base64Jpeg));
  }
  return decodeViaImage(base64Jpeg);
}

// Fallback for browsers without createImageBitmap: the classic hidden-<img>
// decode. Still async, so it flows through the same ordering guard.
function decodeViaImage(base64Jpeg: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("image decode failed"));
    img.src = `data:image/jpeg;base64,${base64Jpeg}`;
  });
}

function intrinsicSize(src: DecodedFrame): { w: number; h: number } {
  // HTMLImageElement exposes naturalWidth/Height; ImageBitmap uses width/height.
  return "naturalWidth" in src
    ? { w: src.naturalWidth, h: src.naturalHeight }
    : { w: src.width, h: src.height };
}

// ImageBitmap holds decoded pixels off-heap; close() frees them promptly instead
// of waiting for GC. HTMLImageElement has no close() and is GC'd normally.
function release(src: DecodedFrame): void {
  if ("close" in src && typeof src.close === "function") src.close();
}

function base64ToJpegBlob(base64Jpeg: string): Blob {
  const binary = atob(base64Jpeg);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], { type: "image/jpeg" });
}

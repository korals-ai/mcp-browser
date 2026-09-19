"""What a ``find`` miss tells the agent about content scrolled out of view.

A list that renders only the rows scrolled into view (a real app's file
sidebar: 443 px shown of 9,512 px, 2026-09-18) holds its target nowhere in the
accessibility tree, so both ``find`` tiers miss it — and a bare "no match"
reads as "not on this page". The fix is a FACT, not a decision: the driver
measures every scroll box in the page, and the reply names the ones with
content hidden, where to scroll them, and what they show now. The calling
model decides whether to scroll; the small ``find`` model stays a picker.
Shape adapted from browser-use (MIT), which marks every scroll container in
the model's page state with "N pages above/below".

Pure module: formats what :data:`SCROLL_REGIONS_JS` measured.
"""

from __future__ import annotations

from typing import Any

# Less than this many screens hidden is a rounding edge, not content.
_MIN_PAGES = 0.05
_MAX_REGIONS = 5
_MAX_LABEL = 40

# Runs in the page (active frame). Returns {page: {...}, regions: [...]}.
# A box counts when its content is taller than it AND its style lets it scroll
# (browser-use's rule: an `overflow: visible` box is taller but does not
# scroll). Nested boxes are NOT suppressed — the inner list is usually the one
# holding the target. Coordinates are viewport CSS px, the frame `computer`
# scrolls in; `in_main_frame` says whether they are usable as-is.
SCROLL_REGIONS_JS = """
() => {
  const vh = window.innerHeight, vw = window.innerWidth;
  const scrolls = (el) => {
    if (el.scrollHeight <= el.clientHeight + 1) return false;
    return /(auto|scroll|overlay)/.test(getComputedStyle(el).overflowY);
  };
  const visibleText = (el, r) => {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let first = "", last = "", seen = 0, node;
    const top = Math.max(r.top, 0), bottom = Math.min(r.bottom, vh);
    while ((node = walker.nextNode()) && seen < 3000) {
      seen++;
      const t = (node.textContent || "").trim();
      if (!t) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      const b = range.getBoundingClientRect();
      if (b.height === 0 || b.bottom <= top || b.top >= bottom) continue;
      if (!first) first = t;
      last = t;
    }
    return [first, last];
  };
  const regions = [];
  for (const el of document.querySelectorAll("body *")) {
    if (!scrolls(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 20) continue;
    const onScreen = r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
    const h = el.clientHeight || 1;
    const [first, last] = onScreen ? visibleText(el, r) : ["", ""];
    const cx = Math.round((Math.max(r.left, 0) + Math.min(r.right, vw)) / 2);
    const cy = Math.round((Math.max(r.top, 0) + Math.min(r.bottom, vh)) / 2);
    regions.push({
      label: el.getAttribute("aria-label") || el.getAttribute("role") || el.tagName.toLowerCase(),
      pages_above: el.scrollTop / h,
      pages_below: (el.scrollHeight - el.clientHeight - el.scrollTop) / h,
      first, last, on_screen: onScreen,
      x: onScreen ? cx : null, y: onScreen ? cy : null,
    });
  }
  const se = document.scrollingElement || document.documentElement;
  const ph = se.clientHeight || 1;
  return {
    in_main_frame: window === window.top,
    page: {
      pages_above: se.scrollTop / ph,
      pages_below: (se.scrollHeight - se.clientHeight - se.scrollTop) / ph,
    },
    regions,
  };
}
"""


def _short(text: object) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= _MAX_LABEL else s[: _MAX_LABEL - 1] + "…"


def _hidden(region: dict[str, Any]) -> float:
    return float(region.get("pages_above") or 0) + float(region.get("pages_below") or 0)


def _where(region: dict[str, Any]) -> str:
    above = float(region.get("pages_above") or 0)
    below = float(region.get("pages_below") or 0)
    parts = []
    if above >= _MIN_PAGES:
        parts.append(f"{above:.1f} pages above")
    if below >= _MIN_PAGES:
        parts.append(f"{below:.1f} pages below")
    return ", ".join(parts)


def _region_line(region: dict[str, Any], *, coords_usable: bool) -> str:
    line = f"  - {region.get('label') or 'box'}"
    first, last = _short(region.get("first")), _short(region.get("last"))
    if first:
        line += f' showing "{first}"' + (f' … "{last}"' if last and last != first else "")
    line += f": {_where(region)}"
    x, y = region.get("x"), region.get("y")
    if not region.get("on_screen"):
        line += " (the box itself is off screen — scroll the page first)"
    elif coords_usable and x is not None and y is not None:
        line += f" — computer scroll at coordinate [{x}, {y}]"
    return line


def format_scroll_hint(measured: dict[str, Any] | None) -> str:
    """The lines a ``find`` miss appends. Always says SOMETHING: a measurement
    that could not run is stated, never read as "nothing hidden"."""
    if not measured:
        return "Could not measure what is scrolled out of view on this page."
    regions = [r for r in measured.get("regions") or [] if _hidden(r) >= _MIN_PAGES]
    regions.sort(key=_hidden, reverse=True)
    page = measured.get("page") or {}
    page_where = _where(page)
    if not regions and not page_where:
        return "Nothing on this page is scrolled out of view — the target is not rendered here."
    coords_usable = bool(measured.get("in_main_frame"))
    lines = ["Content is scrolled out of view — the target may be there. Scroll, then find again:"]
    lines += [_region_line(r, coords_usable=coords_usable) for r in regions[:_MAX_REGIONS]]
    if len(regions) > _MAX_REGIONS:
        lines.append(f"  (+{len(regions) - _MAX_REGIONS} smaller scroll boxes)")
    lines.append(
        f"  - the page itself: {page_where}" if page_where else "  - the page itself: all in view"
    )
    return "\n".join(lines)

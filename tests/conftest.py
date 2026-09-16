"""Shared test doubles: a FakeDriver so no test launches real Chromium."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from src.browser_driver import BrowserDriver
from src.sessions import SessionManager

# A small page the agent-plane tests read: two refs, a password field whose
# typed value the driver blanks, and a link with a /url property.
DEFAULT_TREE = """- banner:
  - link "Home" [ref=e1] [cursor=pointer]:
    - /url: /
  - searchbox "Search products" [ref=e2]
- main:
  - heading "Welcome" [level=1]
  - textbox "Password" [ref=e3]
  - button "Sign in" [ref=e4] [cursor=pointer]
  - text: Some prose on the page
"""


class FakeDriver(BrowserDriver):
    """In-memory driver recording calls and returning canned data.

    Screencast frames are delivered by calling :meth:`emit_frame` from the
    test, so streaming is deterministic (no timers, no real browser)."""

    def __init__(self, tree: str = DEFAULT_TREE) -> None:
        self.tree = tree
        self.page_text_value = "fake page text"
        self.page_title = "Fake"
        self.opened: list[str] = []
        self.clicks: list[dict[str, Any]] = []
        self.typed: list[tuple[str, str | None]] = []  # (text, ref)
        self.keys: list[tuple[str, int]] = []
        self.scrolls: list[dict[str, Any]] = []
        self.scrolled_to: list[str] = []
        self.scroll_to_result = True
        self.hovers: list[dict[str, Any]] = []
        self.drags: list[tuple[tuple[int, int], tuple[int, int]]] = []
        self.waits: list[float] = []
        self.form_inputs: list[tuple[str, Any]] = []
        self.uploads: list[tuple[str, list[str]]] = []
        self.downloads: list[tuple[str, str]] = []
        self.nav_ops: list[str] = []  # back/forward
        self.waited: list[dict[str, Any]] = []
        self.wait_for_result = True  # a False is a real timeout
        self.logins: list[tuple[str, str]] = []
        self.login_result = True  # fill_login return value; tests can flip
        self.logins_at: list[tuple[str, str, str]] = []  # (ref, username, password)
        self.login_at_result = True  # fill_login_at return value; tests can flip
        self.evals: list[str] = []
        self.eval_result: dict[str, Any] = {"result": ""}
        self.page_state = "ok"
        self.type_note = "ok"
        self.form_note = "ok"
        # What settle() reports for the next mutating action ("Page navigated
        # to …", "Opened tab 2 (…)", 'Dialog confirm "…" was dismissed').
        self.changes: list[str] = []
        self.markers: list[dict[str, Any]] = []
        self.settled = 0
        self.nav_waits: list[tuple[float, int]] = []  # await_navigation(window_s, timeout_ms)
        self.secrets: list[str] = []  # password values typed on the page
        self.screenshot_png = b"\x89PNG\r\n\x1a\n-fake"
        self.screenshots: list[dict[str, Any]] = []
        self.read_pages: list[dict[str, Any]] = []
        self.inputs: list[tuple[str, dict[str, Any]]] = []
        self.viewports: list[tuple[int, int]] = []  # set_viewport calls
        # Cursor probe: recorded (x, y) calls + the value returned (per-test).
        self.cursor_calls: list[tuple[float, float]] = []
        self.cursor_value = "pointer"
        self.acked: list[int] = []
        self.closed = False
        self.screencast_started = False
        self.screencast_stopped = False
        # Fan-out sink set, mirroring PlaywrightDriver: frames go to EVERY viewer.
        self._sinks: set[Callable[[str, dict[str, Any]], Awaitable[None]]] = set()
        # The driver's cached last frame, replayed to a LATE sink on attach. None
        # = nothing captured yet, which is the "no_cached_frame" attach outcome.
        self.cached_frame: tuple[str, dict[str, Any]] | None = None
        # Minimal in-memory tab model mirroring PlaywrightDriver's semantics:
        # string ids for the human plane, numbers for the agent plane.
        self._tab_seq = 1
        self._tabs: list[dict[str, Any]] = [
            {"id": "t1", "tabId": 1, "url": "about:blank", "title": "Fake", "loaded": True}
        ]
        self._active = "t1"
        self.activations: list[int] = []
        # --- frames (per-tab active-frame pin) ---
        self.frame_switches: list[str] = []
        self._frames: dict[str, list[dict[str, Any]]] = {
            "t1": [{"index": 0, "name": "", "url": "about:blank"}]
        }
        self._active_frame_idx: dict[str, int] = {"t1": 0}
        # --- native dialogs ---
        self.dialog_mode = "dismiss"
        self.last_dialog_info: dict[str, Any] | None = None
        # --- console/network observability ---
        self.console_ring: list[dict[str, Any]] = []
        self.network_entries: list[dict[str, Any]] = []
        self._net_seq = 0

    # --- tabs ---

    def _tab(self, tab_id: str) -> dict[str, Any]:
        return next(t for t in self._tabs if t["id"] == tab_id)

    async def open(self, url: str, *, new_tab: bool = False) -> str:
        self.opened.append(url)
        if new_tab:
            await self.new_tab()
        self._tab(self._active)["url"] = url
        # A configurable page_state (default "ok") mirrors the real driver's
        # classify-on-open contract; tests set fake.page_state to simulate walls.
        return self.page_state

    async def list_tabs(self) -> list[dict[str, Any]]:
        return [{**t, "active": t["id"] == self._active} for t in self._tabs]

    async def switch_tab(self, tab_id: str) -> bool:
        if not any(t["id"] == tab_id for t in self._tabs):
            return False
        self._active = tab_id
        self._active_frame_idx[tab_id] = 0  # a tab switch resets to the main frame
        return True

    async def close_tab(self, tab_id: str) -> bool:
        if not any(t["id"] == tab_id for t in self._tabs):
            return False
        was_active = tab_id == self._active
        self._tabs = [t for t in self._tabs if t["id"] != tab_id]
        if not self._tabs:
            await self.new_tab()
        elif was_active:
            self._active = self._tabs[0]["id"]
        return True

    async def activate_num(self, tab_num: int) -> bool:
        tab = next((t for t in self._tabs if t["tabId"] == tab_num), None)
        if tab is None:
            return False
        self.activations.append(tab_num)
        if tab["id"] != self._active:
            self._active = tab["id"]
            self._active_frame_idx[tab["id"]] = 0
        return True

    async def new_tab(self) -> int:
        self._tab_seq += 1
        tid = f"t{self._tab_seq}"
        self._tabs.append(
            {
                "id": tid,
                "tabId": self._tab_seq,
                "url": "about:blank",
                "title": "Fake",
                "loaded": True,
            }
        )
        self._frames[tid] = [{"index": 0, "name": "", "url": "about:blank"}]
        self._active_frame_idx[tid] = 0
        self._active = tid
        return self._tab_seq

    async def close_tab_num(self, tab_num: int) -> bool:
        tab = next((t for t in self._tabs if t["tabId"] == tab_num), None)
        return False if tab is None else await self.close_tab(tab["id"])

    def active_num(self) -> int:
        return int(self._tab(self._active)["tabId"])

    # --- the tree + refs ---

    async def read_page(
        self, *, depth: int | None = None, boxes: bool = False, ref_id: str | None = None
    ) -> str:
        self.read_pages.append({"depth": depth, "boxes": boxes, "ref_id": ref_id})
        return self.tree

    async def secret_values(self) -> list[str]:
        return list(self.secrets)

    async def page_text(self) -> dict[str, Any]:
        return {
            "title": self.page_title,
            "url": self._tab(self._active)["url"],
            "source": "main",
            "text": self.page_text_value,
        }

    async def screenshot(
        self, *, scale: float = 1.0, region: tuple[int, int, int, int] | None = None
    ) -> tuple[bytes, dict[str, Any]]:
        self.screenshots.append({"scale": scale, "region": region})
        if region:
            x0, y0, x1, y1 = region
            w, h, x, y = x1 - x0, y1 - y0, x0, y0
        else:
            w, h, x, y = 1280, 800, 0, 0
        meta = {
            "width": w,
            "height": h,
            "x": x,
            "y": y,
            "image_width": int(w * scale),
            "image_height": int(h * scale),
            "scale": scale,
            "region": list(region) if region else None,
        }
        return self.screenshot_png, meta

    # --- `computer` actions ---

    async def click(
        self,
        *,
        ref: str | None = None,
        coordinate: tuple[int, int] | None = None,
        button: str = "left",
        count: int = 1,
        modifiers: list[str] | None = None,
    ) -> str:
        self.clicks.append(
            {
                "ref": ref,
                "coordinate": coordinate,
                "button": button,
                "count": count,
                "modifiers": modifiers or [],
            }
        )
        return f"Clicked {ref}" if ref else f"Clicked at {coordinate}"

    async def type_text(self, text: str, *, ref: str | None = None) -> str:
        self.typed.append((text, ref))
        return self.type_note

    async def press_keys(self, combo: str, *, repeat: int = 1) -> None:
        self.keys.append((combo, repeat))

    async def scroll(
        self,
        direction: str,
        amount: int,
        *,
        coordinate: tuple[int, int] | None = None,
        ref: str | None = None,
    ) -> None:
        self.scrolls.append(
            {"direction": direction, "amount": amount, "coordinate": coordinate, "ref": ref}
        )

    async def scroll_to(self, ref: str) -> bool:
        self.scrolled_to.append(ref)
        return self.scroll_to_result

    async def hover(
        self, *, ref: str | None = None, coordinate: tuple[int, int] | None = None
    ) -> None:
        self.hovers.append({"ref": ref, "coordinate": coordinate})

    async def drag(self, start: tuple[int, int], end: tuple[int, int]) -> None:
        self.drags.append((start, end))

    async def wait(self, seconds: float) -> None:
        self.waits.append(seconds)

    async def form_input(self, ref: str, value: str | bool | float) -> str:
        self.form_inputs.append((ref, value))
        return self.form_note

    async def upload_files(self, ref: str, paths: list[str]) -> None:
        self.uploads.append((ref, list(paths)))

    async def download(self, ref: str, dest_path: str) -> dict[str, Any]:
        self.downloads.append((ref, dest_path))
        return {"filename": "report.pdf", "saved": True}

    async def eval_js(self, js: str) -> dict[str, Any]:
        self.evals.append(js)
        return dict(self.eval_result)

    # --- settling ---

    def state_marker(self) -> dict[str, Any]:
        marker = {"url": self._tab(self._active)["url"], "tabs": {t["tabId"] for t in self._tabs}}
        self.markers.append(marker)
        return marker

    async def settle(self, marker: dict[str, Any]) -> list[str]:
        self.settled += 1
        changes, self.changes = list(self.changes), []
        return changes

    async def await_navigation(self, *, window_s: float, timeout_ms: int) -> bool:
        self.nav_waits.append((window_s, timeout_ms))
        return False

    # --- history / waiting ---

    async def go_back(self) -> None:
        self.nav_ops.append("back")

    async def go_forward(self) -> None:
        self.nav_ops.append("forward")

    async def wait_for(
        self,
        *,
        text: str | None,
        selector: str | None,
        url: str | None = None,
        response: str | None = None,
        timeout_ms: int,
    ) -> bool:
        self.waited.append(
            {
                "text": text,
                "selector": selector,
                "url": url,
                "response": response,
                "timeout_ms": timeout_ms,
            }
        )
        return self.wait_for_result

    # --- frames ---

    async def list_frames(self) -> list[dict[str, Any]]:
        return list(
            self._frames.get(self._active, [{"index": 0, "name": "", "url": "about:blank"}])
        )

    async def switch_frame(self, target: str) -> bool:
        self.frame_switches.append(target)
        t = (target or "").strip()
        if t == "" or t.lower() == "main" or t == "0":
            self._active_frame_idx[self._active] = 0
            return True
        frames = self._frames.get(self._active, [])
        if t.isdigit():
            idx = int(t)
            if 0 <= idx < len(frames):
                self._active_frame_idx[self._active] = idx
                return True
            return False
        for f in frames:
            if f.get("name") and f["name"] == t:
                self._active_frame_idx[self._active] = int(f["index"])
                return True
        return False

    def active_frame_idx(self) -> int:
        """Test helper: the currently-pinned frame index for the active tab."""
        return self._active_frame_idx.get(self._active, 0)

    # --- native dialogs ---

    async def set_dialog_mode(self, mode: str) -> None:
        self.dialog_mode = "accept" if mode == "accept" else "dismiss"

    async def last_dialog(self) -> dict[str, Any] | None:
        return dict(self.last_dialog_info) if self.last_dialog_info is not None else None

    async def fire_dialog(
        self, dtype: str = "confirm", message: str = "", default_value: str = ""
    ) -> None:
        """Test helper: simulate a dialog firing, recorded like the real handler."""
        self.last_dialog_info = {
            "type": dtype,
            "message": message,
            "default_value": default_value,
            "action": self.dialog_mode,
            "url": self.opened[-1] if self.opened else "about:blank",
            "seq": 1,
        }

    # --- console/network observability ---

    def record_console(self, msg_type: str, text: str) -> None:
        self.console_ring.append({"type": msg_type, "text": text})
        self.console_ring = self.console_ring[-50:]

    def record_network(
        self,
        method: str,
        url: str,
        status: int,
        resource_type: str = "xhr",
        *,
        response_body: str | None = None,
        request_body: str | None = None,
        request_headers: dict[str, str] | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> int:
        self._net_seq += 1
        self.network_entries.append(
            {
                "index": self._net_seq,
                "method": method,
                "url": url,
                "status": status,
                "resource_type": resource_type,
                "size": len(response_body or ""),
                "request_headers": dict(request_headers or {}),
                "response_headers": dict(response_headers or {}),
                "request_body": request_body,
                "response_body": response_body,
                "body_truncated": False,
                "content_type": "application/json",
            }
        )
        self.network_entries = self.network_entries[-100:]
        return self._net_seq

    async def console_messages(
        self, *, pattern: str | None, only_errors: bool, limit: int, clear: bool
    ) -> list[dict[str, Any]]:
        import re

        entries = list(self.console_ring)
        if only_errors:
            entries = [e for e in entries if e["type"] in ("error", "warning")]
        if pattern:
            rx = re.compile(pattern, re.IGNORECASE)
            entries = [e for e in entries if rx.search(e["text"])]
        if limit > 0:
            entries = entries[-limit:]
        if clear:
            self.console_ring = []
        return entries

    async def network_requests(
        self, *, url_pattern: str | None, limit: int, clear: bool
    ) -> list[dict[str, Any]]:
        import re

        entries = list(self.network_entries)
        if url_pattern:
            rx = re.compile(url_pattern, re.IGNORECASE)
            entries = [e for e in entries if rx.search(e["url"])]
        if limit > 0:
            entries = entries[-limit:]
        if clear:
            self.network_entries = []
        keys = ("index", "method", "url", "status", "resource_type", "size")
        return [{k: e[k] for k in keys} for e in entries]

    async def network_request(self, index: int) -> dict[str, Any] | None:
        for e in self.network_entries:
            if e["index"] == index:
                return dict(e)
        return None

    # --- login ---

    async def fill_login(self, username: str, password: str) -> bool:
        self.logins.append((username, password))
        return self.login_result

    async def fill_login_at(self, ref: str, username: str, password: str) -> bool:
        self.logins_at.append((ref, username, password))
        return self.login_at_result

    async def nav_state(self) -> dict[str, Any]:
        return {
            "url": self.opened[-1] if self.opened else "about:blank",
            "title": self.page_title,
            "loaded": True,
            "can_go_back": False,
            "can_go_forward": False,
        }

    # --- the human plane ---

    async def add_frame_sink(self, sink: Callable[[str, dict[str, Any]], Awaitable[None]]) -> str:
        # Mirrors the real driver's contract: the first sink starts the capture,
        # a later one is served from the cached frame when there is one. Tests
        # that need a specific outcome set `cached_frame` before attaching.
        first = not self._sinks
        self.screencast_started = True
        self._sinks.add(sink)
        if first:
            return "first"
        if self.cached_frame is None:
            return "no_cached_frame"
        data, meta = self.cached_frame
        await sink(data, meta)
        return "replayed"

    async def emit_frame(self, data: str, meta: dict[str, Any]) -> None:
        assert self._sinks, "screencast not started"
        self.cached_frame = (data, meta)  # the real driver caches every frame
        for sink in list(self._sinks):
            await sink(data, meta)

    async def ack_frame(self, frame_id: int) -> None:
        self.acked.append(frame_id)

    async def remove_frame_sink(
        self, sink: Callable[[str, dict[str, Any]], Awaitable[None]]
    ) -> None:
        self._sinks.discard(sink)
        if not self._sinks:
            self.screencast_stopped = True

    async def send_input(self, event: str, fields: dict[str, Any]) -> None:
        self.inputs.append((event, fields))

    async def cursor_at(self, x: float, y: float) -> str:
        # Tests drive cursor_value (and can set it to raise / hang) to prove the
        # cosmetic side-channel is isolated from the input/stream paths.
        self.cursor_calls.append((x, y))
        return self.cursor_value

    async def set_viewport(self, width: int, height: int) -> None:
        self.viewports.append((width, height))

    async def close(self) -> None:
        self.closed = True


def make_manager(
    driver: FakeDriver | None = None, **kwargs: Any
) -> tuple[SessionManager, list[FakeDriver]]:
    """A manager whose factory hands out FakeDrivers, plus the list of drivers
    it created (so tests can assert on lifecycle)."""
    created: list[FakeDriver] = []

    async def factory(_session_id: str) -> BrowserDriver:
        d = driver or FakeDriver()
        created.append(d)
        return d

    return SessionManager(factory, **kwargs), created

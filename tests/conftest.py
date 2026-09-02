"""Shared test doubles: a FakeDriver so no test launches real Chromium."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from src.browser_driver import BrowserDriver
from src.sessions import SessionManager
from src.snapshot import Element


class FakeDriver(BrowserDriver):
    """In-memory driver recording calls and returning canned data.

    Screencast frames are delivered by calling :meth:`emit_frame` from the test,
    so streaming is deterministic (no timers, no real browser)."""

    def __init__(self, snapshot: list[Element] | None = None) -> None:
        self._snapshot = snapshot or []
        self.opened: list[str] = []
        self.clicks: list[str] = []
        self.typed: list[tuple[str, str]] = []
        self.scrolls: list[tuple[str, int]] = []
        self.keys: list[str] = []
        self.selects: list[tuple[str, str]] = []
        self.uploads: list[tuple[str, str]] = []
        self.downloads: list[tuple[str, str]] = []
        self.nav_ops: list[str] = []  # back/forward/reload
        self.waited: list[dict[str, Any]] = []
        self.page_text = "fake page text"
        self.tables: list[list[list[str]]] = [[["a", "b"], ["1", "2"]]]
        self.logins: list[tuple[str, str]] = []
        self.login_result = True  # fill_login return value; tests can flip
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
        # Minimal in-memory tab model mirroring PlaywrightDriver's semantics.
        self._tab_seq = 1
        self._tabs: list[dict[str, Any]] = [{"id": "t1", "url": "about:blank", "title": "Fake"}]
        self._active = "t1"
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
        self.network_ring: list[dict[str, Any]] = []
        self.next_wait_response: dict[str, Any] | None = None
        self.waited_response: list[tuple[str, int]] = []
        # --- interaction extras ---
        self.hovers: list[str] = []
        self.drags: list[tuple[str, str]] = []
        self.scrolled_to: list[str] = []
        self.scroll_to_result = True
        self.options: list[dict[str, Any]] = []
        self.links: list[dict[str, str]] = []
        self.evals: list[str] = []
        self.eval_result: dict[str, Any] = {"result": ""}

    async def open(self, url: str, *, new_tab: bool = False) -> None:
        self.opened.append(url)
        if new_tab:
            self._tab_seq += 1
            tid = f"t{self._tab_seq}"
            self._tabs.append({"id": tid, "url": url, "title": "Fake"})
            self._active = tid
        else:
            for t in self._tabs:
                if t["id"] == self._active:
                    t["url"] = url

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
            self._tab_seq += 1
            self._tabs = [{"id": f"t{self._tab_seq}", "url": "about:blank", "title": "Fake"}]
            self._active = self._tabs[0]["id"]
        elif was_active:
            self._active = self._tabs[0]["id"]
        return True

    async def snapshot(self) -> list[Element]:
        return list(self._snapshot)

    async def click(self, ref: str) -> None:
        self.clicks.append(ref)

    async def type_text(self, ref: str, text: str) -> None:
        self.typed.append((ref, text))

    async def scroll(self, direction: str, amount: int) -> None:
        self.scrolls.append((direction, amount))

    async def read(self) -> str:
        return self.page_text

    async def find_text(self, query: str) -> dict[str, Any]:
        count = self.page_text.lower().count(query.lower())
        return {"count": count, "snippet": query if count else ""}

    async def screenshot(self) -> bytes:
        return b"\x89PNG\r\n\x1a\n-fake"

    async def inspect(self, ref: str) -> dict[str, Any]:
        return {"found": True, "ref": ref, "tag": "button", "text": "Fake"}

    async def get_table(self, ref: str | None = None) -> list[list[list[str]]]:
        return self.tables

    async def go_back(self) -> None:
        self.nav_ops.append("back")

    async def go_forward(self) -> None:
        self.nav_ops.append("forward")

    async def reload(self) -> None:
        self.nav_ops.append("reload")

    async def wait_for(self, *, text: str | None, selector: str | None, timeout_ms: int) -> bool:
        self.waited.append({"text": text, "selector": selector, "timeout_ms": timeout_ms})
        return True

    async def press_key(self, key: str) -> None:
        self.keys.append(key)

    async def select_option(self, ref: str, value: str) -> None:
        self.selects.append((ref, value))

    async def upload_file(self, ref: str, path: str) -> None:
        self.uploads.append((ref, path))

    async def download(self, ref: str, dest_path: str) -> dict[str, Any]:
        self.downloads.append((ref, dest_path))
        return {"filename": "report.pdf", "saved": True}

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
        }

    # --- console/network observability ---
    def record_console(self, msg_type: str, text: str) -> None:
        self.console_ring.append({"type": msg_type, "text": text})
        self.console_ring = self.console_ring[-50:]

    def record_network(
        self, method: str, url: str, status: int, resource_type: str = "xhr"
    ) -> None:
        self.network_ring.append(
            {"method": method, "url": url, "status": status, "resource_type": resource_type}
        )
        self.network_ring = self.network_ring[-100:]

    async def console_log(self) -> list[dict[str, Any]]:
        return list(self.console_ring)

    async def network_log(
        self, *, url_substring: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        entries = list(self.network_ring)
        if url_substring:
            needle = url_substring.lower()
            entries = [e for e in entries if needle in e["url"].lower()]
        if limit > 0:
            entries = entries[-limit:]
        return entries

    async def wait_for_response(self, url_substring: str, timeout_ms: int) -> dict[str, Any]:
        self.waited_response.append((url_substring, timeout_ms))
        if self.next_wait_response is not None:
            return self.next_wait_response
        return {"matched": False, "status": 0, "url": ""}

    # --- interaction extras ---
    async def hover(self, ref: str) -> None:
        self.hovers.append(ref)

    async def drag(self, from_ref: str, to_ref: str) -> None:
        self.drags.append((from_ref, to_ref))

    async def scroll_to(self, ref: str) -> bool:
        self.scrolled_to.append(ref)
        return self.scroll_to_result

    async def get_options(self, ref: str) -> list[dict[str, Any]]:
        return list(self.options)

    async def get_links(self, cap: int = 200) -> list[dict[str, str]]:
        return list(self.links[:cap])

    async def eval_js(self, js: str) -> dict[str, Any]:
        self.evals.append(js)
        return dict(self.eval_result)

    async def fill_login(self, username: str, password: str) -> bool:
        self.logins.append((username, password))
        return self.login_result

    async def nav_state(self) -> dict[str, Any]:
        return {
            "url": self.opened[-1] if self.opened else "about:blank",
            "title": "Fake",
            "can_go_back": False,
            "can_go_forward": False,
        }

    async def add_frame_sink(self, sink: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
        self.screencast_started = True
        self._sinks.add(sink)

    async def emit_frame(self, data: str, meta: dict[str, Any]) -> None:
        assert self._sinks, "screencast not started"
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

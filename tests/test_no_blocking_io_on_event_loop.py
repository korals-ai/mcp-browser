"""This tool pod's event loop must stay free of filesystem work.

One process serves this tenant's MCP tool calls, the human's co-browse
WebSocket and the screencast pump on a single loop, over an EFS profile dir.
Every finding fixed here came from ``scripts/lint-blocking-io.sh``, whose first
run on this file reported fourteen — nobody had audited the tool servers.

Two clusters, and they needed different fixes:

* **Session start.** ``PlaywrightDriver.start`` reached ``os.makedirs``, an
  ``flock``, ``os.listdir``, a ``tar -xzf`` subprocess and an ``shutil.rmtree``
  of up to nineteen cache directories — all before Chromium launched, all on
  the loop, and the archive migration unbounded in the profile's size. Grouped
  into ``_prepare_profile_dir`` and offloaded in one hop, because the steps are
  strictly ordered (the flock must be held before anything touches the profile)
  and five hops would buy nothing that one buys.

* **Open-tab persistence.** ``_persist_open_tabs`` had four call sites and
  **one of them cannot await**: Playwright invokes ``on_frame_navigated``
  synchronously from its own loop, and that is the busiest of the four — every
  main-frame navigation. Offloading only the three awaitable sites would have
  turned the ratchet green while leaving the hottest path blocking. So the
  function was split instead: a loop-side snapshot (it reads Playwright
  objects, which are not thread-safe) feeding a single background writer that
  does every write on a thread. Single writer, so a burst coalesces and a stale
  snapshot can never overtake a fresh one.

These tests assert the runtime property. The ratchet reads the source; it
cannot see that the writer is actually running, that a queued write lands, or
that a failed write stays retryable.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from src import browser_driver
from src.browser_driver import _OPEN_TABS_FILE, PlaywrightDriver, _Tab


class _FakePage:
    def __init__(self, url: str = "about:blank") -> None:
        self.url = url
        self.main_frame = object()

    def on(self, _event: str, _cb: Any) -> None:
        pass


class _FakeCDP:
    async def send(self, _method: str, _params: dict[str, Any]) -> None:
        pass


def _tab(tab_id: str, url: str) -> _Tab:
    return _Tab(tab_id, _FakePage(url), _FakeCDP())


def _driver_with_writer(tmp_path: Path) -> PlaywrightDriver:
    """A driver whose background writer is running, as ``start()`` leaves it."""
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._tab_persist_wake = asyncio.Event()
    d._tab_persist_task = asyncio.create_task(d._tab_persist_writer())
    return d


async def _settle() -> None:
    """Yield until the writer has had a turn. The write itself goes to a thread,
    so one loop tick is not enough."""
    for _ in range(50):
        await asyncio.sleep(0.01)


async def test_queued_snapshot_is_written_without_an_explicit_flush(tmp_path: Path) -> None:
    """The property ``on_frame_navigated`` depends on.

    That callback is synchronous — it can queue, but it cannot await. If the
    writer did not drain on its own, every navigation-triggered persist would
    be silently dropped and tab restore would only ever record what a tab
    open/close happened to flush.
    """
    d = _driver_with_writer(tmp_path)
    try:
        d._tabs = [_tab("t1", "https://a.example")]
        d._active_id = "t1"

        d._persist_open_tabs()  # exactly what the sync callback does
        await _settle()

        assert d._read_saved_tabs() == (["https://a.example"], 0, 0)
    finally:
        await d._stop_tab_persist_writer()


async def test_a_burst_collapses_to_the_latest_snapshot(tmp_path: Path) -> None:
    """Last-write-wins by construction, not by luck.

    A redirect chain fires several main-frame navigations back to back. With one
    writer reading the latest pending snapshot there is one EFS write and it is
    the current state; with a thread per call the file could end up holding
    whichever write finished last.
    """
    d = _driver_with_writer(tmp_path)
    try:
        d._tabs = [_tab("t1", "https://one.example")]
        d._active_id = "t1"
        for url in ("https://two.example", "https://three.example", "https://four.example"):
            d._persist_open_tabs()
            d._tabs[0].page.url = url
        d._persist_open_tabs()
        await _settle()

        saved = d._read_saved_tabs()
        assert saved is not None
        assert saved[0] == ["https://four.example"], f"stale snapshot won: {saved}"
    finally:
        await d._stop_tab_persist_writer()


async def test_close_flushes_the_pending_write(tmp_path: Path) -> None:
    """The last thing a session does is close its tabs; losing that write is
    losing the restore. So the stop path flushes BEFORE it cancels."""
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._tab_persist_wake = asyncio.Event()  # no writer task: only the flush can save this
    d._tabs = [_tab("t1", "https://last.example")]
    d._active_id = "t1"

    d._persist_open_tabs()
    assert not (tmp_path / _OPEN_TABS_FILE).exists(), "nothing should have written yet"

    await d._stop_tab_persist_writer()

    assert d._read_saved_tabs() == (["https://last.example"], 0, 0)


async def test_a_failed_write_stays_retryable(tmp_path: Path) -> None:
    """``_last_persisted`` advances only on success.

    Marking it on the attempt would make a failed write look persisted and
    suppress every retry after it — the file would then be stale until the tab
    set changed again, which on a settled session is never.
    """
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._tabs = [_tab("t1", "https://a.example")]
    d._active_id = "t1"

    calls: list[int] = []

    def boom(*_a: Any, **_k: Any) -> None:
        calls.append(1)
        raise OSError("EFS is having a day")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(browser_driver, "_write_open_tabs", boom)
        d._persist_open_tabs()
        await d._do_persist_open_tabs()

    assert calls, "the write was never attempted — the test proves nothing"
    assert d._last_persisted is None, "a failed write must not count as persisted"

    # The retry now succeeds and lands.
    await d._do_persist_open_tabs()
    assert d._read_saved_tabs() == (["https://a.example"], 0, 0)


async def test_the_write_itself_runs_off_the_event_loop(tmp_path: Path) -> None:
    """Thread identity, not latency: on a tmp_path the filesystem is always fast
    enough for a latency assertion to pass against the broken code."""
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._tabs = [_tab("t1", "https://a.example")]
    d._active_id = "t1"

    seen: dict[str, int] = {"loop": threading.get_ident()}
    real = browser_driver._write_open_tabs

    def spy(*a: Any, **k: Any) -> None:
        seen["write"] = threading.get_ident()
        return real(*a, **k)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(browser_driver, "_write_open_tabs", spy)
        d._persist_open_tabs()
        await d._do_persist_open_tabs()

    assert "write" in seen, "the write never ran"
    assert seen["write"] != seen["loop"], "the saved-tabs write ran on the event loop"


async def test_session_start_prepares_the_profile_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole pre-launch profile step leaves the loop in one hop.

    This is the cold-start path a human waits on, and its heaviest member is a
    ``tar -xzf`` subprocess whose runtime is the profile's size.
    """
    profile = tmp_path / "profile" / "chat-a"
    d = PlaywrightDriver(profile_dir=str(profile))

    seen: dict[str, int] = {"loop": threading.get_ident()}
    real = d._prepare_profile_dir

    def spy() -> int:
        seen["prepare"] = threading.get_ident()
        return real()

    monkeypatch.setattr(d, "_prepare_profile_dir", spy)

    async def fake_launch(_profile_dir: str, _w: int, _h: int) -> Any:
        raise RuntimeError("stop here")  # end before tab adoption

    monkeypatch.setattr(d, "_launch_persistent", fake_launch)

    with pytest.raises(RuntimeError, match="stop here"):
        await d.start()

    assert "prepare" in seen, "the profile step never ran — the test proves nothing"
    assert seen["prepare"] != seen["loop"], "the profile prep ran on the event loop"
    assert d._profile_lock_fd is None, "a failed launch must still release the flock"

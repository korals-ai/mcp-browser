"""Open-tab record/replay: a session's tabs survive a pod/container restart.

No Chromium — exercises the pure persistence + restore logic with fakes: recording
the open-tab set to a file in the profile dir, reading it back, and replaying it
onto a fresh persistent context (first URL on the adopted tab, the rest as new
tabs, active restored). Real cookie/tab survival is the scratchpad + stg check.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.browser_driver import _OPEN_TABS_FILE, PlaywrightDriver, _Tab


class _FakePage:
    def __init__(self, url: str = "about:blank") -> None:
        self.url = url
        self.main_frame = object()  # identity target for the framenavigated guard

    def on(self, _event: str, _cb: Any) -> None:
        pass

    async def set_viewport_size(self, _size: dict[str, int]) -> None:
        pass

    async def goto(self, url: str, **_kw: Any) -> None:
        self.url = url


class _FakeCDP:
    async def send(self, _method: str, _params: dict[str, Any]) -> None:
        pass


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []

    async def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page

    async def new_cdp_session(self, _page: _FakePage) -> _FakeCDP:
        return _FakeCDP()


def _tab(tab_id: str, url: str) -> _Tab:
    return _Tab(tab_id, _FakePage(url), _FakeCDP())


def test_persist_and_read_round_trip(tmp_path: Path) -> None:
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._tabs = [_tab("t1", "https://a.example"), _tab("t2", "https://b.example")]
    d._active_id = "t2"

    d._persist_open_tabs()

    assert d._read_saved_tabs() == (["https://a.example", "https://b.example"], 1)
    on_disk = json.loads((tmp_path / _OPEN_TABS_FILE).read_text())
    assert on_disk == {"tabs": ["https://a.example", "https://b.example"], "active": 1}


def test_persist_skips_redundant_write_when_unchanged(tmp_path: Path) -> None:
    # A main-frame framenavigated can re-fire with the same URL (SPA churn); an
    # unchanged set must not rewrite the file (EFS write amplification).
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._tabs = [_tab("t1", "https://a.example")]
    d._active_id = "t1"

    d._persist_open_tabs()  # first write
    (tmp_path / _OPEN_TABS_FILE).unlink()  # remove it out from under the driver
    d._persist_open_tabs()  # same set → deduped, must NOT recreate

    assert not (tmp_path / _OPEN_TABS_FILE).exists()


def test_persist_is_noop_while_replaying(tmp_path: Path) -> None:
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._tabs = [_tab("t1", "https://a.example")]
    d._replaying = True

    d._persist_open_tabs()

    assert not (tmp_path / _OPEN_TABS_FILE).exists()  # a mid-replay write can't clobber


def test_persist_removes_file_when_only_blank_tabs(tmp_path: Path) -> None:
    (tmp_path / _OPEN_TABS_FILE).write_text('{"tabs": ["https://old"], "active": 0}')
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._tabs = [_tab("t1", "about:blank")]
    d._active_id = "t1"

    d._persist_open_tabs()

    # Nothing worth restoring → the stale set is cleared, not left to reopen "old".
    assert not (tmp_path / _OPEN_TABS_FILE).exists()
    assert d._read_saved_tabs() is None


def test_read_saved_tabs_none_without_profile_or_file(tmp_path: Path) -> None:
    assert PlaywrightDriver()._read_saved_tabs() is None  # no profile dir
    assert PlaywrightDriver(profile_dir=str(tmp_path))._read_saved_tabs() is None  # no file


def test_read_saved_tabs_tolerates_garbled_file(tmp_path: Path) -> None:
    (tmp_path / _OPEN_TABS_FILE).write_text("{not json")
    assert PlaywrightDriver(profile_dir=str(tmp_path))._read_saved_tabs() is None


async def test_restore_reopens_saved_tabs_and_active(tmp_path: Path) -> None:
    (tmp_path / _OPEN_TABS_FILE).write_text(
        json.dumps({"tabs": ["https://a.example", "https://b.example"], "active": 1})
    )
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._context = _FakeContext()
    first = _tab("t1", "about:blank")  # the page the persistent context opens with
    d._tabs = [first]
    d._active_id = first.id

    await d._restore_saved_tabs(first)

    assert [t.page.url for t in d._tabs] == ["https://a.example", "https://b.example"]
    assert d._active_id == d._tabs[1].id  # active index restored
    assert d._replaying is False  # flag cleared after restore


async def test_restore_is_noop_without_a_saved_file(tmp_path: Path) -> None:
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._context = _FakeContext()
    first = _tab("t1", "about:blank")
    d._tabs = [first]
    d._active_id = first.id

    await d._restore_saved_tabs(first)

    assert d._tabs == [first]  # nothing to restore → the single adopted tab stands

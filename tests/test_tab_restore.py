"""Open-tab record/replay: a session's tabs survive a pod/container restart.

No Chromium — exercises the pure persistence + restore logic with fakes: recording
the open-tab set to a file in the profile dir, reading it back, and replaying it
onto a fresh persistent context (first URL on the adopted tab, the rest as new
tabs, active restored). Real cookie/tab survival is the scratchpad + stg check.

Persistence is TWO-PHASE: ``_persist_open_tabs`` snapshots on the event loop
(it reads Playwright objects, which are not thread-safe) and
``_do_persist_open_tabs`` writes on a thread. The tests below drive both,
because a test that only snapshotted would assert against a file nobody wrote
— and would have stayed green if the writer were deleted entirely.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.browser_driver import _MAX_RESTORE_TABS, _OPEN_TABS_FILE, PlaywrightDriver, _Tab


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


async def test_persist_and_read_round_trip(tmp_path: Path) -> None:
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._tabs = [_tab("t1", "https://a.example"), _tab("t2", "https://b.example")]
    d._active_id = "t2"

    d._persist_open_tabs()  # snapshot on the loop...
    await d._do_persist_open_tabs()  # ...write on a thread

    assert d._read_saved_tabs() == (["https://a.example", "https://b.example"], 1, 0)
    on_disk = json.loads((tmp_path / _OPEN_TABS_FILE).read_text())
    assert on_disk == {
        "tabs": ["https://a.example", "https://b.example"],
        "active": 1,
        "restore_attempts": 0,  # a normal persist always clears the guard's mark
    }


async def test_persist_skips_redundant_write_when_unchanged(tmp_path: Path) -> None:
    # A main-frame framenavigated can re-fire with the same URL (SPA churn); an
    # unchanged set must not rewrite the file (EFS write amplification).
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._tabs = [_tab("t1", "https://a.example")]
    d._active_id = "t1"

    d._persist_open_tabs()
    await d._do_persist_open_tabs()  # first write
    (tmp_path / _OPEN_TABS_FILE).unlink()  # remove it out from under the driver
    d._persist_open_tabs()  # same set → deduped, must NOT recreate
    await d._do_persist_open_tabs()

    assert not (tmp_path / _OPEN_TABS_FILE).exists()


async def test_persist_is_noop_while_replaying(tmp_path: Path) -> None:
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._tabs = [_tab("t1", "https://a.example")]
    d._replaying = True

    d._persist_open_tabs()
    await d._do_persist_open_tabs()

    assert not (tmp_path / _OPEN_TABS_FILE).exists()  # a mid-replay write can't clobber


async def test_persist_removes_file_when_only_blank_tabs(tmp_path: Path) -> None:
    (tmp_path / _OPEN_TABS_FILE).write_text('{"tabs": ["https://old"], "active": 0}')
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._tabs = [_tab("t1", "about:blank")]
    d._active_id = "t1"

    d._persist_open_tabs()
    await d._do_persist_open_tabs()

    # Nothing worth restoring → the stale set is cleared, not left to reopen "old".
    assert not (tmp_path / _OPEN_TABS_FILE).exists()
    assert d._read_saved_tabs() is None


def test_read_saved_tabs_none_without_profile_or_file(tmp_path: Path) -> None:
    assert PlaywrightDriver()._read_saved_tabs() is None  # no profile dir
    assert PlaywrightDriver(profile_dir=str(tmp_path))._read_saved_tabs() is None  # no file


def test_read_saved_tabs_tolerates_garbled_file(tmp_path: Path) -> None:
    (tmp_path / _OPEN_TABS_FILE).write_text("{not json")
    assert PlaywrightDriver(profile_dir=str(tmp_path))._read_saved_tabs() is None


async def test_restore_recreates_every_tab_but_loads_only_the_active_one(tmp_path: Path) -> None:
    # The 2026-09-09 OOM loop in one assertion: the tab SET comes back whole,
    # the LOADS do not. Ten restored pages at once is what exceeded the pod's
    # memory limit; ten restored tabs with one loaded page does not.
    (tmp_path / _OPEN_TABS_FILE).write_text(
        json.dumps({"tabs": ["https://a.example", "https://b.example"], "active": 1})
    )
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._context = _FakeContext()
    first = _tab("t1", "about:blank")  # the page the persistent context opens with
    d._tabs = [first]
    d._active_id = first.id

    await d._restore_saved_tabs(first)

    assert len(d._tabs) == 2  # the whole set is back
    assert d._active_id == d._tabs[1].id  # active index restored
    assert d._tabs[1].page.url == "https://b.example"  # ...and IT loaded
    assert d._tabs[1].pending_url is None
    assert d._tabs[0].page.url == "about:blank"  # the other did NOT
    assert d._tabs[0].pending_url == "https://a.example"  # but knows where it goes
    assert d._replaying is False  # flag cleared after restore


async def test_parked_tab_hydrates_on_first_activation_and_only_once(tmp_path: Path) -> None:
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._context = _FakeContext()
    parked = _tab("t1", "about:blank")
    parked.pending_url = "https://a.example"
    d._tabs = [parked]
    d._active_id = None

    await d._activate("t1")
    assert parked.page.url == "https://a.example"
    assert parked.pending_url is None

    parked.page.url = "https://moved-since.example"  # the human navigated on
    await d._activate("t1")
    assert parked.page.url == "https://moved-since.example"  # not re-restored


async def test_parked_tab_persists_its_destination_not_about_blank(tmp_path: Path) -> None:
    # The failure this guards: a parked tab's PAGE is genuinely about:blank, so
    # persisting page.url would blank the saved set one write after a restore —
    # losing the tabs by the very mechanism meant to keep them.
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    loaded = _tab("t1", "https://a.example")
    parked = _tab("t2", "about:blank")
    parked.pending_url = "https://b.example"
    d._tabs = [loaded, parked]
    d._active_id = "t1"

    d._persist_open_tabs()
    await d._do_persist_open_tabs()

    assert d._read_saved_tabs() == (["https://a.example", "https://b.example"], 0, 0)


async def test_unfinished_previous_attempt_loads_nothing(tmp_path: Path) -> None:
    # The dirty bit is set: the last restore of this set never reported success,
    # so it is the suspect. Recreate the tabs, load none of them.
    (tmp_path / _OPEN_TABS_FILE).write_text(
        json.dumps(
            {"tabs": ["https://a.example", "https://b.example"], "active": 1, "restore_attempts": 1}
        )
    )
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._context = _FakeContext()
    first = _tab("t1", "about:blank")
    d._tabs = [first]
    d._active_id = first.id

    await d._restore_saved_tabs(first)

    assert len(d._tabs) == 2  # the human still gets their tabs
    assert [t.page.url for t in d._tabs] == ["about:blank", "about:blank"]  # nothing loaded
    assert [t.pending_url for t in d._tabs] == ["https://a.example", "https://b.example"]


async def test_exhausted_guard_keeps_every_url(tmp_path: Path) -> None:
    # Loading nothing did not stop the crash, so the tabs are exonerated. The
    # guard must NOT escalate to deleting them — that would be destroying a
    # human's tabs to chase a cause that is somewhere else.
    saved = {"tabs": ["https://a.example", "https://b.example"], "active": 0, "restore_attempts": 9}
    (tmp_path / _OPEN_TABS_FILE).write_text(json.dumps(saved))
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._context = _FakeContext()
    first = _tab("t1", "about:blank")
    d._tabs = [first]
    d._active_id = first.id

    await d._restore_saved_tabs(first)

    assert [t.pending_url for t in d._tabs] == ["https://a.example", "https://b.example"]
    assert d._read_saved_tabs() is not None  # still on disk for the next start


async def test_successful_restore_clears_the_attempt_mark(tmp_path: Path) -> None:
    (tmp_path / _OPEN_TABS_FILE).write_text(
        json.dumps({"tabs": ["https://a.example"], "active": 0, "restore_attempts": 2})
    )
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._context = _FakeContext()
    first = _tab("t1", "about:blank")
    d._tabs = [first]
    d._active_id = first.id

    await d._restore_saved_tabs(first)

    on_disk = json.loads((tmp_path / _OPEN_TABS_FILE).read_text())
    assert on_disk["restore_attempts"] == 0  # a completed restore exonerates the set


async def test_attempt_mark_is_on_disk_while_the_page_loads(tmp_path: Path) -> None:
    """The whole guard rests on ordering: an OOM kill is a SIGKILL, so the mark
    has to already be on disk when the navigation that might die begins. A mark
    written afterwards would be written by exactly the process that didn't die."""
    seen: list[int] = []

    class _WatchingPage(_FakePage):
        async def goto(self, url: str, **_kw: Any) -> None:
            on_disk = json.loads((tmp_path / _OPEN_TABS_FILE).read_text())
            seen.append(on_disk["restore_attempts"])
            self.url = url

    (tmp_path / _OPEN_TABS_FILE).write_text(
        json.dumps({"tabs": ["https://a.example"], "active": 0})
    )
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._context = _FakeContext()
    first = _Tab("t1", _WatchingPage(), _FakeCDP())
    d._tabs = [first]
    d._active_id = first.id

    await d._restore_saved_tabs(first)

    assert seen == [1]  # the attempt was recorded BEFORE the load, not after


async def test_restore_caps_the_tab_count(tmp_path: Path) -> None:
    urls = [f"https://{i}.example" for i in range(_MAX_RESTORE_TABS + 3)]
    (tmp_path / _OPEN_TABS_FILE).write_text(json.dumps({"tabs": urls, "active": 0}))
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._context = _FakeContext()
    first = _tab("t1", "about:blank")
    d._tabs = [first]
    d._active_id = first.id

    await d._restore_saved_tabs(first)

    assert len(d._tabs) == _MAX_RESTORE_TABS


async def test_restore_is_noop_without_a_saved_file(tmp_path: Path) -> None:
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._context = _FakeContext()
    first = _tab("t1", "about:blank")
    d._tabs = [first]
    d._active_id = first.id

    await d._restore_saved_tabs(first)

    assert d._tabs == [first]  # nothing to restore → the single adopted tab stands

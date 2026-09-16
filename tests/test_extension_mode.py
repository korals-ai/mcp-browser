"""Extension mode: the driver attached to the user's own browser via the relay.

No Chromium, no extension: the relay and Playwright are fakes, so what is
pinned is the driver's SHAPE in this mode — the handshake order (relay up →
connect page opened → extension awaited → ``connect_over_cdp``), what it
skips (no profile, no forced viewport, never closing the user's context),
the loud degradations (``download``), and the server's ``BROWSER_ATTACH`` /
``BROWSER_EXTENSION_TOKEN`` contract.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from typing import Any, ClassVar

import pytest

from src import browser_driver
from src.browser_driver import NotInThisRuntimeError, PlaywrightDriver
from src.cdp_relay import RelayError


class _FakePage:
    def __init__(self, url: str = "chrome-extension://x/connect.html") -> None:
        self.url = url
        self.viewport_calls = 0
        self.closed = False

    def on(self, _event: str, _cb: object) -> None:
        pass

    async def set_viewport_size(self, _size: dict[str, int]) -> None:
        self.viewport_calls += 1

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages
        self.closed = False
        self.new_pages = 0

    def on(self, _event: str, _cb: object) -> None:
        pass

    async def new_page(self) -> _FakePage:
        self.new_pages += 1
        page = _FakePage("about:blank")
        self.pages.append(page)
        return page

    async def new_cdp_session(self, _page: _FakePage) -> object:
        return object()

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, ctx: _FakeContext) -> None:
        self.contexts = [ctx]
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser) -> None:
        self._browser = browser
        self.connect_calls: list[tuple[str, dict[str, Any]]] = []
        self.launch_calls = 0

    async def connect_over_cdp(self, url: str, **kw: Any) -> _FakeBrowser:
        self.connect_calls.append((url, kw))
        return self._browser

    async def launch_persistent_context(self, *_a: Any, **_kw: Any) -> None:
        self.launch_calls += 1
        raise AssertionError("extension mode must not launch")

    async def launch(self, *_a: Any, **_kw: Any) -> None:
        self.launch_calls += 1
        raise AssertionError("extension mode must not launch")


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _FakeRelay:
    """Records the driver's calls; ``wait_outcome`` scripts the extension."""

    instances: ClassVar[list[_FakeRelay]] = []
    wait_outcome: ClassVar[Exception | None] = None

    def __init__(self, *, client_name: str, token: str) -> None:
        self.client_name = client_name
        self.token = token
        self.started = False
        self.stopped = False
        self.waited_s: float | None = None
        self.cdp_url = "ws://127.0.0.1:1/cdp/u"
        _FakeRelay.instances.append(self)

    async def start(self) -> None:
        self.started = True

    def connect_url(self, extension_id: str) -> str:
        return f"chrome-extension://{extension_id}/connect.html?token={self.token}"

    async def wait_for_extension(self, timeout_s: float) -> None:
        self.waited_s = timeout_s
        if _FakeRelay.wait_outcome is not None:
            raise _FakeRelay.wait_outcome

    def unanswered(self) -> list[str]:
        return ["Page.getFrameTree (tab 7)"]

    # What chrome.downloads.onCreated/onChanged reported, as the real relay
    # records it — this is what tells the click's own download from the rest.
    reported: ClassVar[dict[int, dict[str, Any]]] = {}
    # Per download id, the states a search returns, one per call (last repeats).
    states: ClassVar[dict[int, list[dict[str, Any]]]] = {}
    searched: ClassVar[list[Any]] = []
    call_outcome: ClassVar[Exception | None] = None

    @property
    def downloads(self) -> dict[int, dict[str, Any]]:
        return _FakeRelay.reported

    async def call_extension(self, method: str, params: list[Any]) -> Any:
        assert method == "chrome.downloads.search", method
        if _FakeRelay.call_outcome is not None:
            raise _FakeRelay.call_outcome
        _FakeRelay.searched.append(params[0])
        want = int(params[0]["id"])
        seq = _FakeRelay.states.get(want) or []
        if len(seq) > 1:
            return [seq.pop(0)]
        return [seq[0]] if seq else []

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def fake_relay(monkeypatch: pytest.MonkeyPatch) -> type[_FakeRelay]:
    _FakeRelay.instances = []
    _FakeRelay.wait_outcome = None
    _FakeRelay.reported = {}
    _FakeRelay.states = {}
    _FakeRelay.searched = []
    _FakeRelay.call_outcome = None
    monkeypatch.setattr(browser_driver, "CdpRelay", _FakeRelay)
    return _FakeRelay


def _wire(
    driver: PlaywrightDriver, monkeypatch: pytest.MonkeyPatch
) -> tuple[_FakeChromium, _FakeContext, _FakePage]:
    page = _FakePage()
    ctx = _FakeContext([page])
    chromium = _FakeChromium(_FakeBrowser(ctx))
    pw = _FakePlaywright(chromium)

    class _Starter:
        async def start(self) -> _FakePlaywright:
            return pw

    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: _Starter())
    return chromium, ctx, page


def test_attach_mode_is_validated_at_construction() -> None:
    with pytest.raises(ValueError, match="attach must be one of"):
        PlaywrightDriver(attach="chrome", headless=True, executable_path="")


async def test_start_in_extension_mode_relays_then_connects_over_cdp(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay]
) -> None:
    opened: list[str] = []

    async def opener(url: str) -> None:
        opened.append(url)

    d = PlaywrightDriver(
        attach="extension",
        extension_token="tok",
        extension_id="e" * 32,
        connect_opener=opener,
        headless=True,
        executable_path="",
    )
    chromium, ctx, page = _wire(d, monkeypatch)

    await d.start()

    relay = fake_relay.instances[0]
    assert relay.started and relay.token == "tok"
    assert opened == [f"chrome-extension://{'e' * 32}/connect.html?token=tok"]
    assert relay.waited_s == browser_driver._EXTENSION_CONNECT_TIMEOUT_S  # token → short bound
    assert chromium.connect_calls == [(relay.cdp_url, {"timeout": 0})]
    assert chromium.launch_calls == 0
    # The extension's tab is adopted as tab 1 — no second page, no forced viewport.
    assert len(d._tabs) == 1 and d._tabs[0].page is page
    assert ctx.new_pages == 0
    assert page.viewport_calls == 0


async def test_no_token_waits_the_longer_human_approval_bound(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay]
) -> None:
    async def opener(_url: str) -> None:
        pass

    d = PlaywrightDriver(
        attach="extension",
        extension_token="",
        connect_opener=opener,
        headless=True,
        executable_path="",
    )
    _wire(d, monkeypatch)
    await d.start()
    assert fake_relay.instances[0].waited_s == browser_driver._EXTENSION_APPROVE_TIMEOUT_S


async def test_extension_never_arriving_stops_the_relay_and_raises(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay]
) -> None:
    async def opener(_url: str) -> None:
        pass

    fake_relay.wait_outcome = RelayError("browser extension did not connect within 30s")
    d = PlaywrightDriver(
        attach="extension",
        extension_token="t",
        connect_opener=opener,
        headless=True,
        executable_path="",
    )
    chromium, _ctx, _page = _wire(d, monkeypatch)
    with pytest.raises(RelayError, match="did not connect"):
        await d.start()
    assert fake_relay.instances[0].stopped
    assert chromium.connect_calls == []


async def test_no_opener_and_no_browser_binary_fails_loud(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay]
) -> None:
    d = PlaywrightDriver(attach="extension", extension_token="t", headless=True, executable_path="")
    _wire(d, monkeypatch)
    with pytest.raises(RelayError, match="BROWSER_EXECUTABLE_PATH"):
        await d.start()


async def test_default_opener_spawns_the_browser_with_the_connect_url(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay]
) -> None:
    spawned: list[list[str]] = []

    class _Proc:
        def __init__(self, argv: list[str], **_kw: Any) -> None:
            spawned.append(argv)

    monkeypatch.setattr(browser_driver.subprocess, "Popen", _Proc)
    d = PlaywrightDriver(
        attach="extension", extension_token="t", headless=True, executable_path="/Apps/Chrome"
    )
    _wire(d, monkeypatch)
    await d.start()
    assert spawned == [
        [
            "/Apps/Chrome",
            fake_relay.instances[0].connect_url(browser_driver.MCP_BROWSER_EXTENSION_ID),
        ]
    ]


async def test_close_leaves_the_users_context_open_and_stops_the_relay(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay]
) -> None:
    async def opener(_url: str) -> None:
        pass

    d = PlaywrightDriver(
        attach="extension",
        extension_token="t",
        connect_opener=opener,
        headless=True,
        executable_path="",
    )
    chromium, ctx, _page = _wire(d, monkeypatch)
    await d.start()
    await d.close()
    assert ctx.closed is False  # the context is the user's profile
    assert chromium._browser.closed is True  # our connection to it is dropped
    assert fake_relay.instances[0].stopped
    assert d._playwright is not None and d._playwright.stopped


async def test_tab_close_events_during_close_do_not_spawn_replacement_tabs(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay]
) -> None:
    async def opener(_url: str) -> None:
        pass

    d = PlaywrightDriver(
        attach="extension",
        extension_token="t",
        connect_opener=opener,
        headless=True,
        executable_path="",
    )
    _chromium, ctx, _page = _wire(d, monkeypatch)
    await d.start()
    tab = d._tabs[0]
    await d.close()
    await d._on_page_closed(tab)  # the extension detaching fires this late
    assert ctx.new_pages == 0


async def test_connect_failing_after_the_extension_arrived_releases_the_relay(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay]
) -> None:
    """Every failure after relay.start() must stop the relay: each retried
    tool call mints a new driver, and a listener left behind per attempt
    would accumulate — and keep the user's tab group held."""

    async def opener(_url: str) -> None:
        pass

    d = PlaywrightDriver(
        attach="extension",
        extension_token="t",
        connect_opener=opener,
        headless=True,
        executable_path="",
    )
    chromium, _ctx, _page = _wire(d, monkeypatch)

    async def refuse(_url: str, **_kw: Any) -> _FakeBrowser:
        raise RuntimeError("Protocol error (Target.setAutoAttach): refused")

    monkeypatch.setattr(chromium, "connect_over_cdp", refuse)
    with pytest.raises(RuntimeError, match="refused"):
        await d.start()
    assert fake_relay.instances[0].stopped
    assert d._relay is None


async def test_an_attach_that_never_finishes_is_bounded_and_releases_the_relay(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay]
) -> None:
    """An unresponsive tab hung connect_over_cdp forever (real Chrome,
    2026-09-16): start() sat under the session lock, every later call
    queued behind it, the relay stayed up. Now it is a RelayError naming
    the tab, and the relay is gone with it."""

    async def opener(_url: str) -> None:
        pass

    d = PlaywrightDriver(
        attach="extension",
        extension_token="t",
        connect_opener=opener,
        headless=True,
        executable_path="",
    )
    chromium, _ctx, _page = _wire(d, monkeypatch)

    async def hang(_url: str, **_kw: Any) -> _FakeBrowser:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(chromium, "connect_over_cdp", hang)
    monkeypatch.setattr(browser_driver, "_EXTENSION_ATTACH_TIMEOUT_S", 0.05)
    with pytest.raises(RelayError, match="did not finish within 0s") as info:
        await d.start()
    assert "Page.getFrameTree (tab 7)" in str(info.value)  # what it was stuck on
    assert fake_relay.instances[0].stopped
    assert d._relay is None


async def test_no_context_from_the_bridge_closes_the_browser_link_and_the_relay(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay]
) -> None:
    async def opener(_url: str) -> None:
        pass

    d = PlaywrightDriver(
        attach="extension",
        extension_token="t",
        connect_opener=opener,
        headless=True,
        executable_path="",
    )
    chromium, _ctx, _page = _wire(d, monkeypatch)
    chromium._browser.contexts = []
    with pytest.raises(RelayError, match="no browser context"):
        await d.start()
    assert chromium._browser.closed is True
    assert fake_relay.instances[0].stopped


async def test_no_browser_binary_releases_the_relay_it_already_started(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay]
) -> None:
    d = PlaywrightDriver(attach="extension", extension_token="t", headless=True, executable_path="")
    _wire(d, monkeypatch)
    with pytest.raises(RelayError, match="BROWSER_EXECUTABLE_PATH"):
        await d.start()
    assert fake_relay.instances[0].started and fake_relay.instances[0].stopped


async def test_extension_leaving_makes_the_driver_report_itself_gone(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay]
) -> None:
    """The relay's extension-closed callback is wired to the driver; the
    reason the extension gave is what gone() reports (the user's disconnect
    click, or 'All controlled tabs detached'). A close of our own is not
    the extension leaving."""

    async def opener(_url: str) -> None:
        pass

    d = PlaywrightDriver(
        attach="extension",
        extension_token="t",
        connect_opener=opener,
        headless=True,
        executable_path="",
    )
    _wire(d, monkeypatch)
    await d.start()
    relay = fake_relay.instances[0]
    assert d.gone() is None
    relay.on_extension_closed("User disconnected")
    assert d.gone() == "User disconnected"

    d2 = PlaywrightDriver(
        attach="extension",
        extension_token="t",
        connect_opener=opener,
        headless=True,
        executable_path="",
    )
    _wire(d2, monkeypatch)
    await d2.start()
    await d2.close()
    fake_relay.instances[1].on_extension_closed("Playwright client disconnected")
    assert d2.gone() is None


async def test_user_closing_the_last_tab_does_not_spawn_a_replacement(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay]
) -> None:
    """The extension ends the session ~150 ms after its last tab detaches;
    a replacement tab would race that — and win only by opening a blank tab
    in the user's browser. In launch mode the same event still replaces."""

    async def opener(_url: str) -> None:
        pass

    d = PlaywrightDriver(
        attach="extension",
        extension_token="t",
        connect_opener=opener,
        headless=True,
        executable_path="",
    )
    _chromium, ctx, _page = _wire(d, monkeypatch)
    await d.start()
    await d._on_page_closed(d._tabs[0])
    assert d._tabs == []
    assert ctx.new_pages == 0


def _stamp(offset_s: float) -> str:
    import time as _time
    from datetime import UTC, datetime

    return (
        datetime.fromtimestamp(_time.time() + offset_s, tz=UTC).isoformat().replace("+00:00", "Z")
    )


async def _driver_with_a_clickable_ref(
    monkeypatch: pytest.MonkeyPatch,
    on_click: Callable[[], None] | None = None,
) -> tuple[PlaywrightDriver, list[str]]:
    async def opener(_url: str) -> None:
        pass

    d = PlaywrightDriver(
        attach="extension",
        extension_token="t",
        connect_opener=opener,
        headless=True,
        executable_path="",
    )
    _wire(d, monkeypatch)
    await d.start()
    clicked: list[str] = []

    class _Loc:
        async def click(self, **_kw: Any) -> None:
            clicked.append("e1")
            if on_click is not None:
                on_click()  # chrome.downloads.onCreated arrives with the click

    monkeypatch.setattr(d, "_ref_locator", lambda _tab, ref: _Loc())
    monkeypatch.setattr(browser_driver, "_EXTENSION_DOWNLOAD_TIMEOUT_S", 1.0)
    return d, clicked


async def test_download_in_extension_mode_copies_what_the_browser_saved(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay], tmp_path: Any
) -> None:
    """The click saves the file where the user's browser saves files;
    chrome.downloads names it; the server (same machine) copies it in.
    An in-progress download is waited for."""
    saved = tmp_path / "report.pdf"
    saved.write_bytes(b"%PDF-1.4 fake")
    ours = {
        "id": 2,
        "state": "in_progress",
        "startTime": _stamp(0),
        "filename": str(saved),
        "referrer": "https://example.com/",
    }
    fake_relay.states = {2: [ours, {**ours, "state": "complete"}]}
    d, clicked = await _driver_with_a_clickable_ref(
        monkeypatch, on_click=lambda: fake_relay.reported.update({2: ours})
    )
    out = await d.download("e1", str(tmp_path / "dest.pdf"))
    assert clicked == ["e1"]
    assert out == {"filename": "report.pdf", "saved": True}
    assert (tmp_path / "dest.pdf").read_bytes() == b"%PDF-1.4 fake"
    # Only OUR id was ever asked about — never "the newest download".
    assert fake_relay.searched == [{"id": 2}, {"id": 2}]


async def test_download_ignores_a_download_the_click_did_not_start(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay], tmp_path: Any
) -> None:
    """The download list is the whole browser's. A file the user saved a moment
    before the click — already complete while ours is still downloading — must
    not be handed to the agent as the file it asked for, whether it was known
    before the click (id) or only announced after it (referrer)."""
    theirs = tmp_path / "their-taxes.pdf"
    theirs.write_bytes(b"not for the agent")
    ours = tmp_path / "report.pdf"
    ours.write_bytes(b"%PDF-1.4 fake")
    before = {
        "id": 8,
        "state": "complete",
        "startTime": _stamp(-0.3),
        "filename": str(theirs),
        "referrer": "https://bank.example/",
    }
    after = {**before, "id": 9, "startTime": _stamp(0.01), "referrer": ""}
    mine = {
        "id": 10,
        "state": "in_progress",
        "startTime": _stamp(0),
        "filename": str(ours),
        "referrer": "https://example.com/",
    }
    fake_relay.reported = {8: before}  # known before the click
    fake_relay.states = {
        8: [before],
        9: [after],
        10: [mine, {**mine, "state": "complete"}],
    }
    d, _clicked = await _driver_with_a_clickable_ref(
        monkeypatch,
        # Their second download and ours are announced together, ours last.
        on_click=lambda: fake_relay.reported.update({9: after, 10: mine}),
    )
    d._tabs[0].page.url = "https://example.com/"  # the page the agent clicked on
    out = await d.download("e1", str(tmp_path / "dest.pdf"))
    assert out == {"filename": "report.pdf", "saved": True}
    assert (tmp_path / "dest.pdf").read_bytes() == b"%PDF-1.4 fake"
    assert {int(q["id"]) for q in fake_relay.searched} == {10}


async def test_download_through_an_extension_without_downloads_refuses_before_the_click(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay], tmp_path: Any
) -> None:
    """The upstream Playwright Extension has no chrome.downloads: the search
    would fail AFTER the click had already saved a file the server cannot find.
    Fail first, and say where a click would put it."""
    d, clicked = await _driver_with_a_clickable_ref(monkeypatch)
    d._extension_id = "mmlmfjhmonkocbjadbfplnigmagldckm"
    with pytest.raises(NotInThisRuntimeError, match="no download control"):
        await d.download("e1", str(tmp_path / "dest.pdf"))
    assert clicked == []


async def test_download_in_extension_mode_reports_an_interrupted_or_missing_download(
    monkeypatch: pytest.MonkeyPatch, fake_relay: type[_FakeRelay], tmp_path: Any
) -> None:
    item = {"id": 3, "state": "interrupted", "startTime": _stamp(0), "error": "USER_CANCELED"}
    fake_relay.states = {3: [item]}
    d, _clicked = await _driver_with_a_clickable_ref(
        monkeypatch, on_click=lambda: fake_relay.reported.update({3: item})
    )
    with pytest.raises(RelayError, match="USER_CANCELED"):
        await d.download("e1", str(tmp_path / "x"))

    # The click started no download at all.
    fake_relay.reported = {}
    fake_relay.states = {}
    with pytest.raises(RelayError, match="no download finished within 1s"):
        await d.download("e1", str(tmp_path / "x"))

    # Complete on a machine this server cannot read: honest about where it is.
    gone = {"id": 4, "state": "complete", "startTime": _stamp(0), "filename": "/nope/gone.bin"}
    fake_relay.reported = {4: gone}
    fake_relay.states = {4: [gone]}
    monkeypatch.setattr(browser_driver, "_own_download_id", lambda *_a: 4)
    with pytest.raises(NotInThisRuntimeError, match="Downloads folder"):
        await d.download("e1", str(tmp_path / "x"))


# --- the server's contract ---------------------------------------------------------


@pytest.fixture
def reload_server(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Reload ``src.server`` under a patched env, and reload it back to the
    runner's env afterwards so later tests see the module they expect."""
    import src.server as server

    def _reload() -> Any:
        return importlib.reload(server)

    yield _reload
    monkeypatch.undo()
    importlib.reload(server)


def test_server_refuses_an_unknown_attach_mode(
    monkeypatch: pytest.MonkeyPatch, reload_server: Any
) -> None:
    monkeypatch.setenv("BROWSER_ATTACH", "chrome")
    with pytest.raises(SystemExit, match="BROWSER_ATTACH='chrome' is not one of"):
        reload_server()


def test_server_extension_mode_requires_the_token_var(
    monkeypatch: pytest.MonkeyPatch, reload_server: Any
) -> None:
    monkeypatch.setenv("BROWSER_ATTACH", "extension")
    monkeypatch.delenv("BROWSER_EXTENSION_TOKEN", raising=False)
    with pytest.raises(KeyError, match="BROWSER_EXTENSION_TOKEN"):
        reload_server()


def test_server_extension_mode_requires_a_non_empty_extension_id(
    monkeypatch: pytest.MonkeyPatch, reload_server: Any
) -> None:
    monkeypatch.setenv("BROWSER_ATTACH", "extension")
    monkeypatch.setenv("BROWSER_EXTENSION_TOKEN", "")
    monkeypatch.delenv("BROWSER_EXTENSION_ID", raising=False)
    with pytest.raises(KeyError, match="BROWSER_EXTENSION_ID"):
        reload_server()
    monkeypatch.setenv("BROWSER_EXTENSION_ID", "  ")
    with pytest.raises(SystemExit, match="non-empty BROWSER_EXTENSION_ID"):
        reload_server()


async def test_server_extension_mode_builds_an_extension_driver_without_a_profile(
    monkeypatch: pytest.MonkeyPatch, reload_server: Any
) -> None:
    monkeypatch.setenv("BROWSER_ATTACH", "extension")
    monkeypatch.setenv("BROWSER_EXTENSION_TOKEN", " tok ")
    monkeypatch.setenv("BROWSER_EXTENSION_ID", "ipjfogjeagnpojnjignlhfapffkdpahi")
    monkeypatch.setenv("BROWSER_PROFILE_DIR", "/work/.cobrowse/profile")
    server = reload_server()
    built: list[dict[str, Any]] = []

    class _Rec:
        def __init__(self, **kw: Any) -> None:
            built.append(kw)

        async def start(self) -> None:
            pass

    monkeypatch.setattr(server, "PlaywrightDriver", _Rec)
    monkeypatch.setattr(server, "_schedule_profile_gc", lambda *a, **k: None)
    await server._playwright_factory("local")
    assert built == [
        {
            "attach": "extension",
            "extension_token": "tok",
            "extension_id": "ipjfogjeagnpojnjignlhfapffkdpahi",
        }
    ]


async def test_server_launch_mode_keeps_the_profile_factory(
    monkeypatch: pytest.MonkeyPatch, reload_server: Any
) -> None:
    monkeypatch.setenv("BROWSER_ATTACH", "launch")
    monkeypatch.setenv("BROWSER_PROFILE_DIR", "/work/.cobrowse/profile")
    server = reload_server()
    built: list[dict[str, Any]] = []

    class _Rec:
        def __init__(self, **kw: Any) -> None:
            built.append(kw)

        async def start(self) -> None:
            pass

    monkeypatch.setattr(server, "PlaywrightDriver", _Rec)
    monkeypatch.setattr(server, "_schedule_profile_gc", lambda *a, **k: None)
    await server._playwright_factory("local")
    assert built[0]["profile_dir"] == "/work/.cobrowse/profile/local"
    assert "attach" not in built[0]

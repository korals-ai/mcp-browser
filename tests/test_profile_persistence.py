"""Persistent-profile (crash-survival) logic for the real driver.

No Chromium: exercises the pure branch logic — profile-dir normalization,
stale-lock clearing, the persistent launch call shape, and first-tab adoption —
with fakes. Real Chromium cookie-survival is covered by the scratchpad
real-browser verification, not the unit suite.
"""

from __future__ import annotations

from typing import Any

from src.browser_driver import PlaywrightDriver, _clear_singleton_locks


class _FakePage:
    def __init__(self, url: str = "about:blank") -> None:
        self.url = url

    def on(self, _event: str, _cb: object) -> None:
        # The driver installs dialog + observability listeners at tab creation;
        # accept and ignore them here.
        pass


class _FakeContext:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages
        self.new_page_calls = 0

    async def new_page(self) -> _FakePage:
        self.new_page_calls += 1
        page = _FakePage()
        self.pages.append(page)
        return page

    async def new_cdp_session(self, page: _FakePage) -> object:
        return object()


class _FakeChromium:
    def __init__(self, context: _FakeContext) -> None:
        self._context = context
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def launch_persistent_context(self, user_data_dir: str, **kw: Any) -> _FakeContext:
        self.calls.append((user_data_dir, kw))
        return self._context


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium


def test_empty_profile_dir_normalizes_to_none() -> None:
    assert PlaywrightDriver()._profile_dir is None
    assert PlaywrightDriver(profile_dir="")._profile_dir is None
    assert PlaywrightDriver(profile_dir="/home/agent/.cobrowse/profile")._profile_dir == (
        "/home/agent/.cobrowse/profile"
    )


def test_clear_singleton_locks_removes_stale_and_is_safe_on_missing(tmp_path: Any) -> None:
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (tmp_path / name).write_text("stale")
    _clear_singleton_locks(str(tmp_path))
    assert not any((tmp_path / n).exists() for n in ("SingletonLock", "SingletonCookie"))
    # A missing profile dir must not raise (first-ever launch).
    _clear_singleton_locks(str(tmp_path / "nope"))


async def test_launch_persistent_makes_dir_clears_locks_and_launches(
    tmp_path: Any, monkeypatch: Any
) -> None:
    monkeypatch.delenv("BROWSER_HEADLESS", raising=False)  # default → headless True
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "SingletonLock").write_text("stale")
    ctx = _FakeContext([_FakePage()])
    chromium = _FakeChromium(ctx)
    d = PlaywrightDriver(profile_dir=str(profile))
    d._playwright = _FakePlaywright(chromium)

    out = await d._launch_persistent(str(profile), 1280, 800)

    assert out is ctx
    assert not (profile / "SingletonLock").exists()  # stale lock cleared pre-launch
    user_data_dir, kw = chromium.calls[0]
    assert user_data_dir == str(profile)
    assert kw["headless"] is True
    assert kw["viewport"] == {"width": 1280, "height": 800}


async def test_adopt_first_tab_reuses_the_contexts_initial_page() -> None:
    # A persistent context opens with one blank page; adopt it, don't spawn a 2nd.
    existing = _FakePage()
    ctx = _FakeContext([existing])
    d = PlaywrightDriver(profile_dir="/x")
    d._context = ctx

    tab = await d._adopt_first_tab()

    assert tab.page is existing
    assert ctx.new_page_calls == 0
    assert d._tabs == [tab]


async def test_adopt_first_tab_opens_one_when_context_is_empty() -> None:
    ctx = _FakeContext([])
    d = PlaywrightDriver(profile_dir="/x")
    d._context = ctx

    tab = await d._adopt_first_tab()

    assert ctx.new_page_calls == 1
    assert d._tabs == [tab]


def test_defaults_to_headless_bundled_when_env_unset(monkeypatch: Any) -> None:
    # Tests/CI (no BROWSER_* env) → headless, Playwright's bundled Chromium, so
    # the unit suite needs no X server and no CfT binary.
    monkeypatch.delenv("BROWSER_HEADLESS", raising=False)
    monkeypatch.delenv("BROWSER_EXECUTABLE_PATH", raising=False)
    d = PlaywrightDriver()
    assert d._headless is True
    assert d._executable_path is None


async def test_launch_uses_headed_cft_binary_from_env(tmp_path: Any, monkeypatch: Any) -> None:
    # The pod image sets these → headed real Chrome (Chrome for Testing).
    monkeypatch.setenv("BROWSER_HEADLESS", "false")
    monkeypatch.setenv("BROWSER_EXECUTABLE_PATH", "/opt/chrome-linux64/chrome")
    chromium = _FakeChromium(_FakeContext([_FakePage()]))
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._playwright = _FakePlaywright(chromium)

    await d._launch_persistent(str(tmp_path), 1280, 800)

    _, kw = chromium.calls[0]
    assert kw["headless"] is False
    assert kw["executable_path"] == "/opt/chrome-linux64/chrome"


def test_explicit_ctor_args_override_env(monkeypatch: Any) -> None:
    # A non-None caller pin beats the env; None means "defer to env" (the sentinel
    # that lets the pod image drive the default).
    monkeypatch.setenv("BROWSER_HEADLESS", "false")
    monkeypatch.setenv("BROWSER_EXECUTABLE_PATH", "/opt/chrome-linux64/chrome")
    d = PlaywrightDriver(headless=True, executable_path="/custom/chrome")
    assert d._headless is True  # explicit True overrides env "false"
    assert d._executable_path == "/custom/chrome"  # explicit path overrides env

"""Automation-fingerprint hardening for the real driver.

No Chromium: exercises the pure logic — UA cleaning, that the stealth launch
args reach both launch paths, and that the cleaned UA is applied to every tab's
CDP session — with fakes. Real bot-wall behavior is covered by the scratchpad
real-browser verification, not the unit suite.
"""

from __future__ import annotations

from typing import Any

from src.browser_driver import (
    _CONTAINER_LAUNCH_ARGS,
    _STEALTH_LAUNCH_ARGS,
    PlaywrightDriver,
    _clean_ua,
    _launch_args,
)

_HEADLESS_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "HeadlessChrome/140.0.7339.16 Safari/537.36"
)


def test_clean_ua_strips_headless_token() -> None:
    cleaned = _clean_ua(_HEADLESS_UA)
    assert cleaned is not None
    assert "Headless" not in cleaned
    assert "Chrome/140.0.7339.16" in cleaned  # version preserved, no drift


def test_clean_ua_returns_none_when_already_clean() -> None:
    normal = "Mozilla/5.0 (X11; Linux x86_64) Chrome/151.0.0.0 Safari/537.36"
    assert _clean_ua(normal) is None


def test_stealth_arg_disables_automation_controlled() -> None:
    # The one flag that flips navigator.webdriver false — guard against a silent drop.
    assert "--disable-blink-features=AutomationControlled" in _STEALTH_LAUNCH_ARGS


def test_launch_args_include_stealth_and_container_flags() -> None:
    # A containerized real Chrome can't run its setuid sandbox as an unprivileged
    # uid; --no-sandbox must reach the launch or the browser won't start in-pod.
    args = _launch_args()
    assert all(a in args for a in _STEALTH_LAUNCH_ARGS)
    assert all(a in args for a in _CONTAINER_LAUNCH_ARGS)
    assert "--no-sandbox" in args


def test_cache_launch_args_empty_when_no_cache_dir() -> None:
    # CI/local (BROWSER_CACHE_DIR unset): Chrome keeps its default cache location.
    assert PlaywrightDriver()._cache_launch_args() == []


def test_cache_launch_args_redirect_and_cap() -> None:
    # Redirect the disk cache off the EFS profile onto this session's emptyDir dir,
    # with Chrome self-capping its own writes.
    d = PlaywrightDriver(cache_dir="/chromium-cache/chat-9", disk_cache_size="268435456")
    args = d._cache_launch_args()
    assert "--disk-cache-dir=/chromium-cache/chat-9" in args
    assert "--disk-cache-size=268435456" in args


def test_cache_launch_args_ignores_non_numeric_size(caplog: Any) -> None:
    # A misconfigured size must not feed Chrome a flag it silently drops — and the
    # drop must be LOGGED, not swallowed (diagnosability).
    d = PlaywrightDriver(cache_dir="/chromium-cache/chat-9", disk_cache_size="lots")
    with caplog.at_level("WARNING", logger="workspace-tool-browser"):
        args = d._cache_launch_args()
    assert "--disk-cache-dir=/chromium-cache/chat-9" in args
    assert not any(a.startswith("--disk-cache-size") for a in args)
    assert any("BROWSER_DISK_CACHE_SIZE" in r.message for r in caplog.records)


class _FakeCDP:
    def __init__(self) -> None:
        self.sends: list[tuple[str, dict[str, Any]]] = []

    async def send(self, method: str, params: dict[str, Any]) -> None:
        self.sends.append((method, params))


async def test_apply_ua_override_sends_when_ua_set() -> None:
    d = PlaywrightDriver()
    d._ua = "Mozilla/5.0 … Chrome/140.0.0.0 …"
    cdp = _FakeCDP()

    await d._apply_ua_override(cdp)

    assert cdp.sends == [("Network.setUserAgentOverride", {"userAgent": d._ua})]


async def test_apply_ua_override_is_noop_when_ua_unset() -> None:
    d = PlaywrightDriver()
    assert d._ua is None
    cdp = _FakeCDP()

    await d._apply_ua_override(cdp)

    assert cdp.sends == []


class _FakePage:
    def __init__(self, ua: str) -> None:
        self._ua = ua

    async def evaluate(self, _js: str) -> str:
        return self._ua


class _FakeTab:
    def __init__(self, ua: str) -> None:
        self.page = _FakePage(ua)
        self.cdp = _FakeCDP()


async def test_prime_ua_override_computes_and_applies_cleaned_ua() -> None:
    d = PlaywrightDriver()
    tab = _FakeTab(_HEADLESS_UA)

    await d._prime_ua_override(tab)  # type: ignore[arg-type]

    assert d._ua is not None and "Headless" not in d._ua
    assert tab.cdp.sends == [("Network.setUserAgentOverride", {"userAgent": d._ua})]


async def test_prime_ua_override_leaves_clean_ua_alone() -> None:
    d = PlaywrightDriver()
    tab = _FakeTab("Mozilla/5.0 … Chrome/151.0.0.0 Safari/537.36")

    await d._prime_ua_override(tab)  # type: ignore[arg-type]

    assert d._ua is None
    assert tab.cdp.sends == []  # nothing to override


class _RecordingChromium:
    def __init__(self) -> None:
        self.persistent_kw: dict[str, Any] = {}

    async def launch_persistent_context(self, _user_data_dir: str, **kw: Any) -> object:
        self.persistent_kw = kw
        return object()


class _RecordingPlaywright:
    def __init__(self, chromium: _RecordingChromium) -> None:
        self.chromium = chromium


async def test_persistent_launch_passes_stealth_args(tmp_path: Any) -> None:
    chromium = _RecordingChromium()
    d = PlaywrightDriver(profile_dir=str(tmp_path))
    d._playwright = _RecordingPlaywright(chromium)

    await d._launch_persistent(str(tmp_path), 1280, 800)

    assert chromium.persistent_kw["args"] == _launch_args()

"""Console + network observability + wait-for-response, and URL truncation."""

from __future__ import annotations

from src import agent_ops
from src.browser_driver import _URL_CAP, _truncate_url
from tests.conftest import FakeDriver, make_manager


async def test_console_log_returns_and_works_while_paused() -> None:
    driver = FakeDriver()
    driver.record_console("error", "Uncaught TypeError: x is undefined")
    driver.record_console("log", "submit clicked")
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("s")
    session.agent_paused = True  # observation must survive a takeover
    out = await agent_ops.console_log(manager, "s")
    assert out == [
        {"type": "error", "text": "Uncaught TypeError: x is undefined"},
        {"type": "log", "text": "submit clicked"},
    ]


async def test_network_log_filters_limits_and_marks_failed() -> None:
    driver = FakeDriver()
    driver.record_network("GET", "https://x/api/list", 200)
    driver.record_network("POST", "https://x/api/save", 0)  # blocked/aborted
    driver.record_network("GET", "https://x/static/app.js", 200, resource_type="script")
    manager, _ = make_manager(driver)
    only_save = await agent_ops.network_log(manager, "s", url_substring="/api/save")
    assert only_save == [
        {"method": "POST", "url": "https://x/api/save", "status": 0, "resource_type": "xhr"}
    ]
    limited = await agent_ops.network_log(manager, "s", limit=1)
    assert len(limited) == 1 and limited[0]["url"] == "https://x/static/app.js"


async def test_wait_for_response_matched_and_timeout() -> None:
    driver = FakeDriver()
    driver.next_wait_response = {"matched": True, "status": 201, "url": "https://x/api/save"}
    manager, _ = make_manager(driver)
    out = await agent_ops.wait_for_response(manager, "s", "/api/save", 5000)
    assert out == {"matched": True, "status": 201, "url": "https://x/api/save"}
    assert driver.waited_response == [("/api/save", 5000)]
    driver.next_wait_response = None  # → timeout shape
    assert await agent_ops.wait_for_response(manager, "s", "/never", 1000) == {
        "matched": False,
        "status": 0,
        "url": "",
    }


def test_truncate_url_collapses_data_uri_and_caps_long() -> None:
    big = "data:image/png;base64," + "A" * 50000
    out = _truncate_url(big)
    assert out.startswith("data:image/png") and len(out) < 100
    long_url = "https://x/api/save?token=" + "z" * 5000
    capped = _truncate_url(long_url)
    assert len(capped) <= _URL_CAP + 1 and capped.endswith("…")


def test_server_wait_for_response_guards_empty_and_caps_timeout() -> None:
    # The server tool guards an empty substring and caps the timeout at 60s.
    import inspect

    from src import server

    src = inspect.getsource(server)
    assert "url_substring is required" in src
    assert "min(int(timeout_ms), 60000)" in src

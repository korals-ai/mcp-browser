"""Portal creds reading + agent self-login: creds inject, never leak to agent."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from src import agent_ops
from src.portal_creds import PortalCred, read_portals
from tests.conftest import FakeDriver, make_manager


def _write_portals(tmp_path: Path, portals: list[dict[str, str]]) -> Path:
    p = tmp_path / "PORTAL_CREDENTIALS_JSON"
    p.write_text(json.dumps(portals))
    return p


def test_read_portals_indexes_by_id(tmp_path: Path) -> None:
    p = _write_portals(
        tmp_path,
        [
            {"portal_id": "acme", "login_url": "https://a", "username": "u", "password": "pw"},
        ],
    )
    portals = read_portals(p)
    assert portals["acme"] == PortalCred("acme", "https://a", "u", "pw")


def test_read_portals_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_portals(tmp_path / "nope.json") == {}


def test_read_portals_skips_malformed_entry(tmp_path: Path) -> None:
    p = _write_portals(
        tmp_path, [{"portal_id": "ok", "login_url": "u", "username": "x", "password": "y"}]
    )
    # append a broken entry by rewriting
    p.write_text(
        json.dumps(
            [{"portal_id": "ok", "login_url": "u", "username": "x", "password": "y"}, {"bad": 1}]
        )
    )
    portals = read_portals(p)
    assert list(portals) == ["ok"]


async def test_login_injects_creds_and_never_returns_password() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    portals = {"acme": PortalCred("acme", "https://acme/login", "user1", "s3cret")}
    result = await agent_ops.login(manager, "c1", "acme", portals)
    # credential reached the driver...
    assert driver.opened == ["https://acme/login"]
    assert driver.logins == [("user1", "s3cret")]
    # ...but NEVER the agent-facing result
    assert result["status"] == "submitted"
    assert "s3cret" not in json.dumps(result)


async def test_login_unknown_portal() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    result = await agent_ops.login(manager, "c1", "ghost", {})
    assert result["status"] == "unknown_portal"
    assert driver.opened == []  # never navigated


async def test_login_refuses_a_portal_with_no_stored_password() -> None:
    # The live shape this came from: a portal configured with a username and
    # login_url whose password was never stored, so build_env delivers "". The
    # old code typed the empty password and reported `submitted`, so the agent
    # read a failed login as a successful one — and the real account took a
    # failed-login attempt for nothing.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    portals = {"acme": PortalCred("acme", "https://acme/login", "user1", "")}
    result = await agent_ops.login(manager, "c1", "acme", portals)
    assert result["status"] == "no_stored_password"
    assert driver.opened == [], "must not navigate — the attempt is the harm"
    assert driver.logins == []


async def test_login_reports_no_form_found() -> None:
    driver = FakeDriver()
    driver.login_result = False  # no password field on the page
    manager, _ = make_manager(driver)
    portals = {"acme": PortalCred("acme", "https://acme/login", "u", "p")}
    result = await agent_ops.login(manager, "c1", "acme", portals)
    assert result["status"] == "no_login_form"
    # It must say WHERE it looked: "no login form" alone sends the caller to the
    # wrong page when the stored URL is a home page and the form is behind a menu.
    assert result["tried"] == "stored_login_url"
    assert result["url"] == "https://acme/login"


async def test_login_at_a_ref_fills_the_current_page_without_navigating() -> None:
    # The reachability case: the form is not at the stored URL (behind a menu, or
    # on a separate sign-in provider). The caller navigates there itself and names
    # the username field, so no selector guessing and no navigation.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    portals = {"acme": PortalCred("acme", "https://acme/", "user1", "s3cret")}

    result = await agent_ops.login(manager, "c1", "acme", portals, ref="e7")

    assert driver.opened == [], "must not navigate away from the form it was given"
    assert driver.logins == [], "must not fall back to selector-guessed filling"
    assert driver.logins_at == [("e7", "user1", "s3cret")]
    assert result["status"] == "submitted"
    assert "s3cret" not in json.dumps(result)


async def test_login_at_a_stale_ref_reports_no_form_and_names_the_ref() -> None:
    driver = FakeDriver()
    driver.login_at_result = False  # ref resolved to nothing
    manager, _ = make_manager(driver)
    portals = {"acme": PortalCred("acme", "https://acme/", "u", "p")}

    result = await agent_ops.login(manager, "c1", "acme", portals, ref="e99")

    assert result["status"] == "no_login_form"
    assert result["tried"] == "ref:e99"
    assert driver.opened == []


async def test_login_at_a_ref_still_refuses_a_portal_with_no_password() -> None:
    # The no-password guard must come BEFORE the ref path too — otherwise the
    # caller-chosen route around the stored URL is also a route around the guard.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    portals = {"acme": PortalCred("acme", "https://acme/", "user1", "")}

    result = await agent_ops.login(manager, "c1", "acme", portals, ref="e7")

    assert result["status"] == "no_stored_password"
    assert driver.logins_at == []
    assert driver.opened == []


async def test_login_logs_the_origin_it_typed_into(caplog: Any) -> None:
    # The audit half of allowing a caller-chosen destination. Origin only — a full
    # URL would put per-session tokens from the query string into the logs.
    driver = FakeDriver()
    driver.opened.append("https://acme.example/signin?state=abc123&token=SEKRIT")
    manager, _ = make_manager(driver)
    portals = {"acme": PortalCred("acme", "https://acme.example/", "user1", "s3cret")}

    with caplog.at_level(logging.INFO, logger="workspace-tool-browser"):
        await agent_ops.login(manager, "c1", "acme", portals, ref="e7")

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "origin=https://acme.example" in logged
    assert "s3cret" not in logged
    assert "SEKRIT" not in logged, "the query string must not reach the log"

"""Portal creds reading + agent self-login: creds inject, never leak to agent."""

from __future__ import annotations

import json
from pathlib import Path

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


async def test_login_reports_no_form_found() -> None:
    driver = FakeDriver()
    driver.login_result = False  # no password field on the page
    manager, _ = make_manager(driver)
    portals = {"acme": PortalCred("acme", "https://acme/login", "u", "p")}
    result = await agent_ops.login(manager, "c1", "acme", portals)
    assert result["status"] == "no_login_form"

"""Agent-plane ops: actions reach the driver, redaction holds, pause is honoured."""

from __future__ import annotations

import pytest

from src import agent_ops
from src.snapshot import REDACTED, Element
from tests.conftest import FakeDriver, make_manager


async def test_open_navigates_and_returns_nav_state() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    nav = await agent_ops.open_url(manager, "c1", "https://example.com/form")
    assert driver.opened == ["https://example.com/form"]
    assert nav["url"] == "https://example.com/form"


async def test_open_broadcasts_nav_to_viewers() -> None:
    # The viewer's address bar must update on navigation, not just on connect.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    sent: list[dict[str, object]] = []

    async def sink(frame: dict[str, object]) -> None:
        sent.append(frame)

    session.add_viewer_sink(sink)
    await agent_ops.open_url(manager, "c1", "https://example.com/next")
    navs = [f for f in sent if f["type"] == "browser_nav"]
    assert navs and navs[-1]["url"] == "https://example.com/next"


async def test_snapshot_is_redacted_at_the_op_boundary() -> None:
    snap: list[Element] = [{"ref": "e0", "tag": "input", "type": "password", "value": "pw"}]
    driver = FakeDriver(snapshot=snap)
    manager, _ = make_manager(driver)
    out = await agent_ops.snapshot(manager, "c1")
    assert out[0]["value"] == REDACTED
    assert "pw" not in str(out)


async def test_click_and_type_reach_driver() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    await agent_ops.click(manager, "c1", "e3")
    await agent_ops.type_text(manager, "c1", "e4", "hello")
    assert driver.clicks == ["e3"]
    assert driver.typed == [("e4", "hello")]


async def test_scroll_reaches_driver() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    await agent_ops.scroll(manager, "c1", "down", 600)
    await agent_ops.scroll(manager, "c1", "up", 300)
    assert driver.scrolls == [("down", 600), ("up", 300)]


async def test_paused_session_blocks_click_and_type() -> None:
    # Mid-task actions still respect a human pause (so a pause can interrupt the
    # agent) — but see test_open_clears_pause: a fresh navigation resumes.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    session.agent_paused = True  # human took over
    with pytest.raises(agent_ops.AgentPaused):
        await agent_ops.click(manager, "c1", "e0")
    with pytest.raises(agent_ops.AgentPaused):
        await agent_ops.type_text(manager, "c1", "e0", "x")
    assert driver.clicks == []


async def test_open_clears_pause_and_never_locks_out() -> None:
    # The lock-out fix: browser_open on a paused session (e.g. a stale takeover)
    # resumes the agent instead of raising, and tells viewers the agent is back.
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    sent: list[dict[str, object]] = []

    async def sink(frame: dict[str, object]) -> None:
        sent.append(frame)

    session.add_viewer_sink(sink)
    nav = await agent_ops.open_url(manager, "c1", "https://example.com")
    assert nav["url"] == "https://example.com"
    assert driver.opened == ["https://example.com"]
    assert session.agent_paused is False  # resumed
    states = [f for f in sent if f["type"] == "browser_agent_state"]
    assert states and states[-1]["state"] == "idle"


async def test_agent_action_records_last_actor() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    await agent_ops.click(manager, "c1", "e0")
    assert manager.get("c1").last_actor == "agent"


# --- browser_fill_form -------------------------------------------------------
# The tool exists to collapse one-model-round-trip-per-field into a single call,
# so what matters is that every field really reaches the driver, that a select is
# routed differently from a text input, and that a partial failure is REPORTED
# rather than swallowed or raised.


async def test_fill_form_types_every_field_in_order() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    result = await agent_ops.fill_form(
        manager,
        "c1",
        [
            {"ref": "e0", "value": "Northbay"},
            {"ref": "e1", "value": "53770.00"},
            {"ref": "e2", "value": "9"},
        ],
    )
    assert driver.typed == [("e0", "Northbay"), ("e1", "53770.00"), ("e2", "9")]
    assert result == {
        "filled": 3,
        "requested": 3,
        "fields": [
            {"ref": "e0", "status": "ok"},
            {"ref": "e1", "status": "ok"},
            {"ref": "e2", "status": "ok"},
        ],
    }


async def test_fill_form_routes_select_kind_to_select_option() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    await agent_ops.fill_form(
        manager,
        "c1",
        [{"ref": "e0", "value": "Acme"}, {"ref": "e3", "value": "36", "kind": "select"}],
    )
    assert driver.typed == [("e0", "Acme")]
    assert driver.selects == [("e3", "36")]


async def test_fill_form_reports_a_failed_field_and_keeps_going() -> None:
    # A form can partly succeed and the browser cannot roll it back, so the
    # caller must be told WHICH field failed rather than getting an exception
    # that hides the fields already filled.
    driver = FakeDriver()

    async def explode(ref: str, text: str) -> None:
        if ref == "e1":
            raise RuntimeError("element detached")
        driver.typed.append((ref, text))

    driver.type_text = explode  # type: ignore[method-assign]
    manager, _ = make_manager(driver)
    result = await agent_ops.fill_form(
        manager,
        "c1",
        [
            {"ref": "e0", "value": "Northbay"},
            {"ref": "e1", "value": "53770.00"},
            {"ref": "e2", "value": "9"},
        ],
    )
    assert result["filled"] == 2
    assert result["requested"] == 3
    assert result["fields"][1]["status"] == "error"
    assert "element detached" in result["fields"][1]["error"]
    # The field AFTER the failure still ran — a mid-form error must not silently
    # truncate the rest.
    assert driver.typed == [("e0", "Northbay"), ("e2", "9")]


async def test_fill_form_error_never_echoes_the_value() -> None:
    # Values are typed into forms — passwords among them — and this string goes
    # straight into the model's context.
    driver = FakeDriver()

    async def explode(ref: str, text: str) -> None:
        raise RuntimeError("boom")

    driver.type_text = explode  # type: ignore[method-assign]
    manager, _ = make_manager(driver)
    result = await agent_ops.fill_form(manager, "c1", [{"ref": "e0", "value": "hunter2-secret"}])
    assert "hunter2-secret" not in str(result)


async def test_fill_form_is_blocked_while_the_human_has_taken_over() -> None:
    driver = FakeDriver()
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    with pytest.raises(agent_ops.AgentPaused):
        await agent_ops.fill_form(manager, "c1", [{"ref": "e0", "value": "x"}])
    assert driver.typed == []


# --- run_recipe --------------------------------------------------------------
# The executor exists to remove the model from between the steps, so what
# matters is that every step really reaches the driver, that a stale descriptor
# stops the run instead of guessing, and that a partial run reports where it
# stopped so the caller can take over from there.


def _login_snapshot() -> list[Element]:
    return [
        Element(ref="e0", tag="input", type="text", name="Username", value=""),
        Element(ref="e1", tag="input", type="password", name="Password", value=""),
        Element(ref="e2", tag="button", type="submit", name="Sign in", value=""),
    ]


def _search_recipe() -> dict[str, object]:
    from src import recipes

    return recipes.parse(
        {
            "name": "search",
            "params": ["keyword"],
            "steps": [
                {"tool": "browser_open", "args": {"url": "https://portal.example/login"}},
                {"tool": "browser_snapshot"},
                {
                    "tool": "browser_fill_form",
                    "fields": [
                        {"target": {"name": "Username"}, "value": "estimator"},
                        {"target": {"name": "Password"}, "value": "param:keyword"},
                    ],
                },
                {"tool": "browser_click", "target": {"name": "Sign in"}},
                {"tool": "browser_get_table"},
            ],
        }
    )


async def test_run_recipe_drives_every_step_without_a_model() -> None:
    driver = FakeDriver(snapshot=_login_snapshot())
    manager, _ = make_manager(driver)
    result = await agent_ops.run_recipe(manager, "c1", _search_recipe(), {"keyword": "CCTV"})

    assert result["status"] == "ok"
    assert result["steps_run"] == 5
    assert driver.opened == ["https://portal.example/login"]
    assert driver.typed == [("e0", "estimator"), ("e1", "CCTV")]
    assert driver.clicks == ["e2"]
    # Only the extraction step's output comes back — a recipe returns the
    # answer, not a transcript of everything it touched.
    assert [e["tool"] for e in result["extracted"]] == ["browser_get_table"]


async def test_run_recipe_refuses_before_touching_the_site_when_a_param_is_missing() -> None:
    driver = FakeDriver(snapshot=_login_snapshot())
    manager, _ = make_manager(driver)
    result = await agent_ops.run_recipe(manager, "c1", _search_recipe(), {})
    assert result["status"] == "missing_params"
    assert result["missing"] == ["keyword"]
    # Nothing ran: a half-filled login form is worse than no attempt.
    assert driver.opened == [] and driver.typed == []


async def test_run_recipe_stops_and_says_where_when_the_page_changed() -> None:
    # The button was relabelled — the everyday way a recipe dies. The caller
    # needs the step index to take over from there rather than restarting.
    from src import recipes

    renamed = [e for e in _login_snapshot() if e["name"] != "Sign in"]
    driver = FakeDriver(snapshot=renamed)
    manager, _ = make_manager(driver)
    recipe = recipes.parse(
        {
            "steps": [
                {"tool": "browser_snapshot"},
                {"tool": "browser_click", "target": {"name": "Sign in"}},
                {"tool": "browser_get_table"},
            ]
        }
    )
    result = await agent_ops.run_recipe(manager, "c1", recipe, {})
    assert result["status"] == "step_failed"
    assert result["failed_at"] == 1
    assert result["tool"] == "browser_click"
    assert "no element matches" in result["reason"]
    assert driver.clicks == []
    # The step AFTER the failure must not run.
    assert result["steps_run"] == 1


async def test_run_recipe_honours_the_human_takeover() -> None:
    driver = FakeDriver(snapshot=_login_snapshot())
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")
    session.agent_paused = True
    with pytest.raises(agent_ops.AgentPaused):
        await agent_ops.run_recipe(manager, "c1", _search_recipe(), {"keyword": "x"})
    assert driver.opened == []


async def test_run_recipe_redacts_a_password_in_the_snapshot_it_targets_against() -> None:
    # The recipe's own snapshot step goes through the same redaction as
    # browser_snapshot: a recipe must not become a way to read a filled password.
    #
    # Asserted on the step's OWN return value, not on run_recipe's result:
    # browser_snapshot is not an extraction tool, so the snapshot never reaches
    # the result and the old `"hunter2" not in str(result)` was vacuous —
    # deleting redact_snapshot entirely left it green.
    driver = FakeDriver(snapshot=[Element(ref="e1", tag="input", type="password", value="hunter2")])
    manager, _ = make_manager(driver)
    session = await manager.get_or_create("c1")

    output = await agent_ops._run_recipe_step(manager, "c1", session, "browser_snapshot", {}, {})

    assert output, "the snapshot step returned nothing to assert on"
    assert "hunter2" not in str(output)
    assert any(el.get("value") == REDACTED for el in output)


async def test_run_recipe_stops_when_a_wait_for_times_out() -> None:
    # wait_for REPORTS failure (returns False) rather than raising. With no model
    # in the loop, discarding that let the rest of the click-path run against a
    # page that never changed — and still return "ok".
    from src import recipes

    driver = FakeDriver(snapshot=[Element(ref="e0", tag="button", type="submit", name="Go")])
    driver.wait_for_result = False
    manager, _ = make_manager(driver)

    recipe = recipes.parse(
        {
            "steps": [
                {"tool": "browser_snapshot"},
                {"tool": "browser_wait_for", "args": {"text": "Order confirmed"}},
                {"tool": "browser_click", "target": {"tag": "button", "type": "submit"}},
            ]
        }
    )
    result = await agent_ops.run_recipe(manager, "c1", recipe, {})

    assert result["status"] == "step_failed"
    assert result["failed_at"] == 1
    assert driver.clicks == [], "the click ran after the page never changed"


async def test_run_recipe_stops_when_the_login_did_not_complete() -> None:
    # login returns {"status": "unknown_portal"} rather than raising. Continuing
    # past it submits the rest of the path against a signed-out page.
    from src import recipes

    driver = FakeDriver(snapshot=[Element(ref="e0", tag="button", type="submit", name="Go")])
    manager, _ = make_manager(driver)

    recipe = recipes.parse(
        {
            "steps": [
                {"tool": "browser_login", "args": {"portal_id": "nope"}},
                {"tool": "browser_snapshot"},
                {"tool": "browser_click", "target": {"tag": "button", "type": "submit"}},
            ]
        }
    )
    result = await agent_ops.run_recipe(manager, "c1", recipe, {})

    assert result["status"] == "step_failed"
    assert result["failed_at"] == 0
    assert "unknown_portal" in result["reason"]
    assert driver.clicks == []


async def test_run_recipe_reports_an_undeclared_param_as_a_failed_step() -> None:
    # missing_params only checks DECLARED names, so a typo'd "param:" reference
    # inside args survives the up-front check. It used to escape run_recipe as a
    # bare exception — no step index, after earlier steps had already navigated.
    from src import recipes

    driver = FakeDriver()
    manager, _ = make_manager(driver)

    recipe = recipes.parse(
        {
            "params": ["keyword"],
            "steps": [
                {"tool": "browser_open", "args": {"url": "https://example.test/"}},
                {"tool": "browser_open", "args": {"url": "param:tpyo"}},
            ],
        }
    )
    result = await agent_ops.run_recipe(manager, "c1", recipe, {"keyword": "x"})

    assert result["status"] == "step_failed"
    assert result["failed_at"] == 1
    assert "tpyo" in result["reason"]
    assert driver.opened == ["https://example.test/"]

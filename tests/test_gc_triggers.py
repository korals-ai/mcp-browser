"""Server-side triggers for the profile GC: the throttle, the force-on-boot
bypass, and that a session-start schedule NEVER blocks the launch (fire-and-forget,
run in a worker thread). See server._sweep_profiles_bg / _schedule_profile_gc.
"""

from __future__ import annotations

import asyncio
from typing import Any

from src import server


def test_sweep_bg_runs_forced_then_throttles(monkeypatch: Any) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(server, "PROFILE_DIR", "/vol/.cobrowse/profile")
    monkeypatch.setattr(server, "PROJECTS_ROOT", "/vol/.claude/projects")
    monkeypatch.setattr(server, "_gc_last_ts", 0.0)
    monkeypatch.setattr(server.profile_gc, "run_sweep", lambda p, r: calls.append((p, r)))

    async def scenario() -> None:
        await server._sweep_profiles_bg(force=True)  # boot: runs
        await server._sweep_profiles_bg(force=False)  # within the interval: throttled

    asyncio.run(scenario())

    assert calls == [("/vol/.cobrowse/profile", "/vol/.claude/projects")]


def test_sweep_bg_force_bypasses_the_throttle(monkeypatch: Any) -> None:
    calls: list[int] = []
    monkeypatch.setattr(server, "PROFILE_DIR", "/vol/.cobrowse/profile")
    monkeypatch.setattr(server, "PROJECTS_ROOT", "/vol/.claude/projects")
    monkeypatch.setattr(server, "_gc_last_ts", 0.0)
    monkeypatch.setattr(server.profile_gc, "run_sweep", lambda p, r: calls.append(1))

    async def scenario() -> None:
        await server._sweep_profiles_bg(force=True)  # sets _gc_last_ts recent
        await server._sweep_profiles_bg(force=True)  # forced again → runs despite recent

    asyncio.run(scenario())

    assert len(calls) == 2


def test_sweep_bg_is_a_noop_without_a_profile_dir(monkeypatch: Any) -> None:
    calls: list[int] = []
    monkeypatch.setattr(server, "PROFILE_DIR", None)
    monkeypatch.setattr(server.profile_gc, "run_sweep", lambda p, r: calls.append(1))
    asyncio.run(server._sweep_profiles_bg(force=True))
    assert calls == []  # ephemeral/CI: nothing to sweep


def test_schedule_gc_is_fire_and_forget(monkeypatch: Any) -> None:
    # A session launch schedules the sweep WITHOUT awaiting it — the sweep must run
    # on a later loop tick, never blocking the launch path.
    ran: list[bool] = []

    async def fake_bg(force: bool = False) -> None:
        ran.append(force)

    monkeypatch.setattr(server, "_sweep_profiles_bg", fake_bg)

    async def scenario() -> None:
        server._schedule_profile_gc(force=True)  # returns immediately — no await
        assert ran == []  # proves the launch path didn't wait on the sweep
        await asyncio.sleep(0)  # let the scheduled task run
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert ran == [True]

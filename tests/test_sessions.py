"""Session lifecycle: lazy create, isolated-by-id, idle reaping, cap eviction."""

from __future__ import annotations

import asyncio

from src.sessions import SessionManager
from tests.conftest import FakeDriver, make_manager


async def test_get_or_create_launches_once_and_shares() -> None:
    manager, created = make_manager()
    a = await manager.get_or_create("chat1")
    b = await manager.get_or_create("chat1")
    assert a is b
    assert len(created) == 1  # one browser for the id


async def test_distinct_ids_get_distinct_sessions() -> None:
    manager, created = make_manager()
    await manager.get_or_create("chat1")
    await manager.get_or_create("chat2")
    assert len(created) == 2


async def test_concurrent_cold_create_launches_one_browser() -> None:
    # A racing tool-call + viewer-connect on a cold id must not launch two.
    manager, created = make_manager()
    results = await asyncio.gather(*(manager.get_or_create("chat1") for _ in range(8)))
    assert all(s is results[0] for s in results)
    assert len(created) == 1


async def test_reap_idle_closes_only_idle_viewerless_sessions() -> None:
    manager, created = make_manager(idle_timeout_s=0.0)
    session = await manager.get_or_create("chat1")
    session.viewers = 1  # a viewer is attached -> must survive even if idle
    assert await manager.reap_idle() == []

    session.viewers = 0
    closed = await manager.reap_idle()
    assert closed == ["chat1"]
    assert created[0].closed is True
    assert manager.get("chat1") is None


async def test_close_all_tears_down_every_browser() -> None:
    manager, created = make_manager()
    await manager.get_or_create("a")
    await manager.get_or_create("b")
    await manager.close_all()
    assert all(d.closed for d in created)
    assert manager.get("a") is None and manager.get("b") is None


async def test_factory_receives_the_session_id() -> None:
    # Prod points Chromium's profile at a per-chat subdir keyed on this id.
    seen: list[str] = []

    async def factory(session_id: str) -> FakeDriver:
        seen.append(session_id)
        return FakeDriver()

    manager = SessionManager(factory)
    await manager.get_or_create("chat-42")
    assert seen == ["chat-42"]


async def test_cap_evicts_least_recently_active_viewerless_session() -> None:
    # At the cap, a new chat evicts the LRU session that has no viewer.
    manager, _ = make_manager(max_sessions=2)
    await manager.get_or_create("a")
    b = await manager.get_or_create("b")
    b.touch()  # 'a' is now the least-recently-active

    await manager.get_or_create("c")  # over cap -> evict LRU viewerless ('a')

    assert manager.get("a") is None
    assert manager.get("b") is b
    assert manager.get("c") is not None


async def test_cap_never_evicts_a_watched_session() -> None:
    # If every session at the cap has a viewer, none is evicted (proceed over-cap):
    # dropping a browser out from under a live viewer is worse than +1 Chromium.
    manager, _ = make_manager(max_sessions=1)
    a = await manager.get_or_create("a")
    a.viewers = 1  # actively watched

    await manager.get_or_create("b")  # must NOT evict the watched 'a'

    assert manager.get("a") is a
    assert manager.get("b") is not None

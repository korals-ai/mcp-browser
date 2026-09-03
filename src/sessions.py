"""Session registry: one Chromium session per ``cobrowse_session_id`` (the chat id).

A session is the join point of the two planes — the agent's MCP tools and the
human's co-browse WS both resolve the SAME :class:`BrowserSession` by id and act
on its driver. The id is the chat id, so each chat gets its OWN isolated browser
(own cookies/window/tabs); a tenant with no chat id falls back to one shared id.

Sessions are created lazily (first agent tool call OR first viewer connect) and
closed when idle on BOTH planes past a timeout, so a tenant that opened one and
walked away doesn't pin a Chromium forever. Creation is serialized per id with a
lock so a racing tool-call + viewer-connect don't launch two browsers. A hard
``max_sessions`` cap bounds how many Chromiums one pod holds at once: opening a
new chat past the cap evicts the least-recently-active session that has NO viewer
(an actively-watched session is never evicted), so N concurrent chats can't OOM
the fixed-size pod.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from src.browser_driver import BrowserDriver

log = logging.getLogger("workspace-tool-browser")

# Factory the manager calls to mint a started driver for a new session, given the
# session id (so prod can point Chromium's profile at a per-chat subdir). Injected
# so tests pass a fake and prod passes the Playwright launcher.
DriverFactory = Callable[[str], Awaitable[BrowserDriver]]


class BrowserSession:
    """One shared browser + the bookkeeping both planes touch."""

    def __init__(self, session_id: str, driver: BrowserDriver) -> None:
        self.session_id = session_id
        self.driver = driver
        # Number of live co-browse WS viewers. Screencast runs iff > 0.
        self.viewers = 0
        # "agent" | "human" | None — drives the BrowserAgentState hint.
        self.last_actor: str | None = None
        # When True, the human has paused the agent (or the agent asked for a
        # take-over); MCP tools reject until resumed. Kept here (not on the
        # driver) so both planes see one flag.
        self.agent_paused = False
        self._last_activity = time.monotonic()
        # Live viewer sinks (a WS ``send_json``): lets an MCP tool push an
        # unsolicited frame to attached viewers (e.g. a take-over request).
        self._viewer_sinks: set[Callable[[dict[str, Any]], Awaitable[None]]] = set()

    def add_viewer_sink(self, sink: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._viewer_sinks.add(sink)

    def remove_viewer_sink(self, sink: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._viewer_sinks.discard(sink)

    async def broadcast(self, frame: dict[str, Any]) -> None:
        """Push one frame to every attached viewer, tolerating a dead socket."""
        for sink in list(self._viewer_sinks):
            try:
                await sink(frame)
            except Exception:
                log.debug("cobrowse session=%s viewer sink send failed", self.session_id)

    def touch(self, actor: str | None = None) -> None:
        """Mark activity (resets the idle clock); records the acting plane."""
        self._last_activity = time.monotonic()
        if actor is not None:
            self.last_actor = actor

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity


class SessionManager:
    """Owns the id -> :class:`BrowserSession` map and their lifecycle."""

    def __init__(
        self,
        driver_factory: DriverFactory,
        *,
        idle_timeout_s: float = 600.0,
        max_sessions: int = 4,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = driver_factory
        self._idle_timeout_s = idle_timeout_s
        self._max_sessions = max_sessions
        self._clock = clock
        self._sessions: dict[str, BrowserSession] = {}
        # Per-id creation lock so a tool-call and a viewer-connect racing on a
        # cold id don't both launch a browser.
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def get_or_create(self, session_id: str) -> BrowserSession:
        """Return the session for ``session_id``, launching Chromium on first
        use. Concurrent callers on a cold id serialize on the per-id lock and
        share the one session."""
        existing = self._sessions.get(session_id)
        if existing is not None:
            existing.touch()
            return existing
        async with self._lock_for(session_id):
            existing = self._sessions.get(session_id)  # re-check under lock
            if existing is not None:
                existing.touch()
                return existing
            await self._evict_for_headroom(session_id)
            log.info("cobrowse session=%s launching browser", session_id)
            driver = await self._factory(session_id)
            session = BrowserSession(session_id, driver)
            self._sessions[session_id] = session
            return session

    async def _evict_for_headroom(self, incoming_id: str) -> None:
        """Before launching a new session, if the pod is at the cap, close the
        least-recently-active session that has NO viewer to make room. A session
        being watched (viewers > 0) is never evicted — dropping a browser out from
        under a live viewer is worse than briefly exceeding the cap, so if EVERY
        session is watched we log and proceed over-cap (the idle reaper reclaims
        them once viewers leave)."""
        if len(self._sessions) < self._max_sessions:
            return
        evictable = [s for s in self._sessions.values() if s.viewers == 0]
        if not evictable:
            log.warning(
                "cobrowse at cap (%d) launching session=%s but all sessions have viewers; "
                "proceeding over-cap",
                self._max_sessions,
                incoming_id,
            )
            return
        victim = max(evictable, key=lambda s: s.idle_seconds())
        log.info(
            "cobrowse at cap (%d): evicting idle session=%s (idle %.0fs) for session=%s",
            self._max_sessions,
            victim.session_id,
            victim.idle_seconds(),
            incoming_id,
        )
        await self.close(victim.session_id)

    def get(self, session_id: str) -> BrowserSession | None:
        return self._sessions.get(session_id)

    async def close(self, session_id: str) -> None:
        """Tear down one session's browser and forget it."""
        session = self._sessions.pop(session_id, None)
        self._locks.pop(session_id, None)
        if session is None:
            return
        log.info("cobrowse session=%s closing browser", session_id)
        try:
            await session.driver.close()
        except Exception:
            log.exception("cobrowse session=%s error on driver.close", session_id)

    async def close_all(self) -> None:
        for sid in list(self._sessions):
            await self.close(sid)

    async def reap_idle(self) -> list[str]:
        """Close sessions idle past the timeout with NO viewers. Returns the ids
        closed. Called on a timer by the server's reaper loop."""
        doomed = [
            sid
            for sid, s in self._sessions.items()
            if s.viewers == 0 and s.idle_seconds() >= self._idle_timeout_s
        ]
        for sid in doomed:
            await self.close(sid)
        return doomed

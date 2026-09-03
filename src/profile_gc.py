"""Garbage-collect orphaned per-chat Chromium profiles off the tenant volume.

Every co-browse chat gets its own persistent Chromium user-data-dir under
``BROWSER_PROFILE_DIR`` (``$HOME/.cobrowse/profile/<chat_id>``). Nothing ever
deleted them, so a tenant that co-browsed many chats accumulates thousands of
profile files on the EFS PVC (they sync to S3), tripping the WorkspaceTenantBloat
/ WorkspaceSyncSlow alerts. This module reclaims a profile once its chat is gone.

Design + adversarial hazard audit:
``docs/plan/20260810T182738Z-cobrowse-profile-footprint.md`` (Pillar B). Deleting a
live-writable directory on a shared NFS volume is hazardous; each guard below
closes a specific audited failure mode — do NOT drop one without re-reading the
plan.

Runs IN the browser pod (the uid that authored the profile), off two triggers:
container boot and opportunistically at session start — no periodic timer, no
cross-pod RPC. The keep-set ("which chats still exist") is read straight off the
shared tenant PVC that both the workspace and browser pods mount; the workspace
pod is never called (its egress netpol blocks it anyway).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field

from src import metrics
from src.browser_driver import profile_lock_path

log = logging.getLogger("workspace-tool-browser")

# A chat id is an SDK UUIDv4 (apps/workspace/src/sdk_session.py always passes one).
# The GC classifies orphans POSITIVELY — a dir is deletable only if its name is
# UUID-shaped AND absent from the keep-set — so a set-complement / degrade-to-empty
# bug can never sweep a reserved dir, a stray file, or (the worst case) every
# profile when the keep-set read fails. See B.5.
_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

# Never GC these even if UUID classification were bypassed: "shared" is the
# SHARED_SESSION cross-version fallback profile (a LIVE profile source until
# Pillar C lands); "Default" is the legacy single-profile artifact. Neither is a
# UUID, so the positive classifier already skips them — this is the explicit
# second guard the plan asks for (B.5).
_RESERVED = frozenset({"shared", "Default"})

# Temp-name prefix for the rename-then-rm: a profile is renamed to a sibling
# ``.gc-<id>`` (same parent dir → same mount → atomic rename; a cross-mount rename
# would EXDEV-degrade to a non-atomic copy and reopen the delete-vs-launch race)
# and then rmtree'd. Not UUID-shaped, so the classifier never re-sweeps it; a
# leftover from a crashed rm is cleaned at the next sweep.
_TOMB_PREFIX = ".gc-"

# Only sweep a profile whose dir mtime is older than this — covers the async
# chat-create window (R6 BACKGROUND_BUDGET_S=180 + agent spawn + first-turn
# latency) where a profile can exist a moment before its chat's meta/jsonl lands.
GRACE_S = 300.0

# Blast-radius clamp per sweep (mirrors the AIP R9 reaper). A classification bug —
# or a genuinely huge backlog — drains over several sweeps, loudly, not in one
# unbounded rm storm.
MAX_DELETES = 50


@dataclass
class SweepResult:
    """Outcome of one sweep. ``aborted`` is set (and everything else empty) when
    the sweep fail-closed on an unusable keep-set — a no-op, never a mass delete."""

    deleted: list[str] = field(default_factory=list)
    skipped_live: int = 0  # a live Chrome holds the profile lock (EWOULDBLOCK)
    skipped_grace: int = 0  # too new — inside the create / first-turn window
    denied_perm: int = 0  # EPERM/EACCES — foreign-owned (per-user isolation)
    errors: int = 0
    hit_cap: bool = False
    aborted: str | None = None


def live_chat_ids(projects_root: str) -> set[str]:
    """Chat ids that still EXIST, read straight off the shared PVC.

    Mirrors ``apps/workspace/src/chats.py:list_chats``: a chat exists iff its
    ``<id>.jsonl`` OR ``<id>.meta.json`` is present under the SDK projects dir
    (``$HOME/.claude/projects/<escaped-cwd>/``). Reads the ACTUAL files (not a
    materialized copy), so the keep-set can never be stale. Soft-deleted chats
    live under ``<escaped-cwd>/.trash/`` and are intentionally NOT counted — a
    deleted chat's profile is meant to be reclaimed (its logins signed out).

    ALL-OR-NOTHING: any read error yields the EMPTY set, never a partial one. A
    partial keep-set would omit live chats and delete their profiles; the caller
    treats empty as "unusable → do not sweep" (fail closed, B.5). So a transient
    EFS hiccup skips a sweep rather than risking a live profile.

    NOTE: this duplicates the SDK's on-disk chat layout in the browser image.
    Keep in sync with ``chats.py:list_chats``. When the harness-agnostic chat
    capability lands (``docs/vision.md`` HARNESS-AGNOSTIC) source it from there.
    """
    ids: set[str] = set()
    try:
        with os.scandir(projects_root) as roots:
            for entry in roots:
                if not entry.is_dir():
                    continue
                with os.scandir(entry.path) as files:
                    for f in files:
                        name = f.name
                        if name.endswith(".jsonl"):
                            ids.add(name[: -len(".jsonl")])
                        elif name.endswith(".meta.json"):
                            # stem of "<id>.meta.json" is "<id>.meta"; strip the
                            # whole suffix (a `.stem`-based strip would keep
                            # "<id>.meta" and delete every meta-only chat).
                            ids.add(name[: -len(".meta.json")])
    except OSError:
        # Missing (fresh tenant) or unreadable (transient EFS) → unusable keep-set.
        return set()
    return ids


def sweep_orphan_profiles(
    profile_dir: str,
    projects_root: str,
    *,
    grace_s: float = GRACE_S,
    max_deletes: int = MAX_DELETES,
    now: float | None = None,
) -> SweepResult:
    """Delete profile dirs whose chat no longer exists. Safe to run concurrently
    with live sessions and with itself: every delete is guarded by the profile's
    external flock (B.1/B.2), an mtime grace (B.7), positive UUID classification +
    a reserved-name whitelist + a per-run cap (B.5), and path-traversal rejection
    (B.4). Fail-closed on an unusable keep-set."""
    result = SweepResult()

    keep = live_chat_ids(projects_root)
    if not keep:
        # Empty OR unreadable → do NOT read this as "every profile is an orphan".
        result.aborted = "keep-set empty or unreadable"
        log.warning("cobrowse GC: skipped — %s (projects_root=%s)", result.aborted, projects_root)
        return result

    now = time.time() if now is None else now

    try:
        entries = sorted(os.listdir(profile_dir))
    except OSError:
        result.aborted = "profile dir unreadable"
        return result

    for name in entries:
        if name.startswith(_TOMB_PREFIX):
            # A tomb left by a crashed prior rm — finish it, best-effort.
            _rmtree_quiet(os.path.join(profile_dir, name))
            continue
        if name in keep or name in _RESERVED:
            continue
        # POSITIVE classification: only a UUID-shaped dir is ever a deletion
        # candidate. A UUID has no '/' or '.', so this also rejects '.'/'..' and
        # any traversal name (B.4) before a path is built.
        if not _UUID_RE.match(name):
            continue
        target = os.path.join(profile_dir, name)
        if not os.path.isdir(target):
            continue
        try:
            age = now - os.path.getmtime(target)
        except OSError:
            result.errors += 1
            continue
        if age < grace_s:
            result.skipped_grace += 1
            continue
        if len(result.deleted) >= max_deletes:
            result.hit_cap = True
            log.warning(
                "cobrowse GC: hit per-run cap of %d deletes; remaining orphans "
                "drain on the next sweep",
                max_deletes,
            )
            break
        outcome = _delete_one(profile_dir, name)
        if outcome == "deleted":
            result.deleted.append(name)
        elif outcome == "live":
            result.skipped_live += 1
        elif outcome == "perm":
            result.denied_perm += 1
        else:
            result.errors += 1

    metrics.record_gc_sweep(
        deleted=len(result.deleted),
        denied_perm=result.denied_perm,
        skipped_live=result.skipped_live,
    )
    _log_summary(result, profile_dir)
    return result


def run_sweep(profile_dir: str | None, projects_root: str) -> SweepResult | None:
    """Best-effort GC entry for the server hooks (container boot + session start).
    NEVER raises — a sweep failure must not block serving. Returns None when no
    profile dir is configured (ephemeral/CI)."""
    if not profile_dir:
        return None
    try:
        return sweep_orphan_profiles(profile_dir, projects_root)
    except Exception:
        log.exception("cobrowse GC: sweep crashed (ignored)")
        return None


def _delete_one(profile_dir: str, name: str) -> str:
    """Delete one orphan profile under its external flock. Returns 'deleted',
    'live' (a Chrome holds the lock — skip, B.2), 'perm' (EPERM/EACCES —
    foreign-owned, surfaced LOUDLY, never a silent skip: under per-user isolation
    this is the tell the GC must move to a root/drop-to-uid helper), or 'error'."""
    target = os.path.join(profile_dir, name)

    # Defense-in-depth path check (B.4): realpath must be exactly one level under
    # profile_dir and keep the same basename — no symlink/traversal escape.
    base = os.path.realpath(profile_dir)
    real = os.path.realpath(target)
    if os.path.dirname(real) != base or os.path.basename(real) != name:
        log.error("cobrowse GC: refused suspicious profile path name=%r real=%r", name, real)
        return "error"

    lock_path = profile_lock_path(target)
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EACCES):
            return "perm"
        return "error"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return "live"  # a live Chrome owns it (B.2)
            if exc.errno in (errno.EPERM, errno.EACCES):
                return "perm"
            return "error"
        # Hold the external lock across the whole delete. A concurrent session
        # start() contends on this same inode (browser_driver._acquire_profile_lock),
        # so it cannot launch on the dir we are removing. Rename FIRST (atomic,
        # same parent → same mount) so any racer sees the intact dir or a missing
        # dir, never a half-deleted one (B.1).
        return _rename_then_rm(profile_dir, name)
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _rename_then_rm(profile_dir: str, name: str) -> str:
    target = os.path.join(profile_dir, name)
    tomb = os.path.join(profile_dir, f"{_TOMB_PREFIX}{name}")
    try:
        if os.path.lexists(tomb):
            _rmtree_quiet(tomb)  # leftover from a crashed prior rm
        os.rename(target, tomb)  # atomic: same parent dir → same mount
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EACCES):
            return "perm"
        log.exception("cobrowse GC: rename failed for %s", name)
        return "error"
    # The profile no longer exists under its live name; a racer that takes the
    # lock after us makedirs a fresh, disjoint dir. Surface an EPERM during the
    # recursive removal (foreign-owned) rather than swallowing it.
    try:
        shutil.rmtree(tomb)
    except PermissionError:
        return "perm"
    except OSError:
        log.exception("cobrowse GC: rmtree failed for %s (tomb left for next sweep)", name)
        return "error"
    return "deleted"


def _rmtree_quiet(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _log_summary(result: SweepResult, profile_dir: str) -> None:
    if result.denied_perm:
        # LOUD: EPERM means a profile is owned by another uid — the per-user
        # isolation tripwire. The GC must move to a root/drop-to-uid helper before
        # isolation ships or these leak silently. Never downgrade below error.
        log.error(
            "cobrowse GC: %d profile(s) DENIED (EPERM) — foreign-owned, NOT "
            "reclaimed; per-user isolation likely landed, GC needs its "
            "root/drop-to-uid helper (plan B.0/B.2)",
            result.denied_perm,
        )
    if result.deleted or result.skipped_live or result.errors:
        log.info(
            "cobrowse GC: deleted=%d skipped_live=%d skipped_grace=%d errors=%d cap_hit=%s (%s)",
            len(result.deleted),
            result.skipped_live,
            result.skipped_grace,
            result.errors,
            result.hit_cap,
            profile_dir,
        )

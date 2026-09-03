"""Orphaned-profile GC (src.profile_gc) — pure filesystem logic, no Chromium.

Each test pins one audited guard from Pillar B of
docs/plan/20260810T182738Z-cobrowse-profile-footprint.md: the keep-set union +
suffix strip, positive-UUID classification, fail-closed on an unusable keep-set,
the reserved-name whitelist, the mtime grace, the live-lock skip, path-traversal
rejection, the per-run cap, atomic rename-then-rm, and EPERM honesty.
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path
from typing import Any

from src import profile_gc
from src.browser_driver import profile_lock_path

UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"
UUID_C = "33333333-3333-4333-8333-333333333333"

# now() far enough ahead of every freshly-created dir's mtime that the grace
# window is comfortably passed (so a dir is eligible for deletion by default).
_PAST_GRACE = time.time() + profile_gc.GRACE_S + 10_000


def _projects(tmp_path: Path) -> Path:
    """A ``.claude/projects/<escaped-cwd>/`` root, as the browser pod sees it."""
    root = tmp_path / ".claude" / "projects"
    (root / "-home-agent").mkdir(parents=True)
    return root


def _chat(projects: Path, chat_id: str, *, meta: bool = False, jsonl: bool = True) -> None:
    d = projects / "-home-agent"
    if jsonl:
        (d / f"{chat_id}.jsonl").write_text("{}")
    if meta:
        (d / f"{chat_id}.meta.json").write_text("{}")


def _profile(base: Path, name: str) -> Path:
    p = base / name
    p.mkdir(parents=True)
    (p / "Cookies").write_text("logins")  # some content so rmtree has work
    return p


# --- keep-set (mirror of chats.py:list_chats) -------------------------------------


def test_live_chat_ids_unions_jsonl_and_meta_with_correct_suffix_strip(tmp_path: Path) -> None:
    projects = _projects(tmp_path)
    _chat(projects, UUID_A, jsonl=True)  # jsonl only (has taken a turn)
    _chat(projects, UUID_B, jsonl=False, meta=True)  # meta only (freshly created)
    _chat(projects, UUID_C, jsonl=True, meta=True)  # both

    ids = profile_gc.live_chat_ids(str(projects))

    # A `.stem`-based strip would yield "<id>.meta" for UUID_B and lose it.
    assert ids == {UUID_A, UUID_B, UUID_C}


def test_live_chat_ids_ignores_soft_deleted_trash(tmp_path: Path) -> None:
    projects = _projects(tmp_path)
    _chat(projects, UUID_A)
    trash = projects / "-home-agent" / ".trash"
    trash.mkdir()
    (trash / f"{UUID_B}.2026-01-01T00-00-00Z.jsonl").write_text("{}")  # soft-deleted

    ids = profile_gc.live_chat_ids(str(projects))

    assert ids == {UUID_A}  # the trashed chat is gone → its profile is reclaimable


def test_live_chat_ids_empty_on_missing_root(tmp_path: Path) -> None:
    assert profile_gc.live_chat_ids(str(tmp_path / "nope")) == set()


# --- fail-closed (B.5: never read empty/unreadable as "all orphaned") -------------


def test_sweep_aborts_on_empty_keep_set_without_deleting(tmp_path: Path) -> None:
    projects = _projects(tmp_path)  # exists but ZERO chats
    base = tmp_path / "profile"
    _profile(base, UUID_A)  # would look like an orphan under a naive classifier

    result = profile_gc.sweep_orphan_profiles(str(base), str(projects), now=_PAST_GRACE)

    assert result.aborted is not None
    assert result.deleted == []
    assert (base / UUID_A).exists()  # NOT deleted — empty keep-set is suspicious


def test_sweep_aborts_on_unreadable_projects_root(tmp_path: Path) -> None:
    base = tmp_path / "profile"
    _profile(base, UUID_A)

    result = profile_gc.sweep_orphan_profiles(str(base), str(tmp_path / "missing"), now=_PAST_GRACE)

    assert result.aborted is not None
    assert (base / UUID_A).exists()


# --- positive classification + reserved names (B.5) -------------------------------


def test_sweep_deletes_orphan_keeps_live_and_spares_non_uuid_and_reserved(
    tmp_path: Path,
) -> None:
    projects = _projects(tmp_path)
    _chat(projects, UUID_A)  # live
    base = tmp_path / "profile"
    _profile(base, UUID_A)  # live chat → keep
    _profile(base, UUID_B)  # orphan (no such chat) → delete
    _profile(base, "shared")  # SHARED_SESSION fallback → keep (reserved)
    _profile(base, "Default")  # legacy artifact → keep (reserved)
    _profile(base, "not-a-uuid")  # stray dir → keep (positive classifier)

    result = profile_gc.sweep_orphan_profiles(str(base), str(projects), now=_PAST_GRACE)

    assert result.deleted == [UUID_B]
    assert (base / UUID_A).exists()
    assert (base / "shared").exists()
    assert (base / "Default").exists()
    assert (base / "not-a-uuid").exists()
    assert not (base / UUID_B).exists()


# --- grace period (B.7) -----------------------------------------------------------


def test_sweep_skips_a_too_new_orphan(tmp_path: Path) -> None:
    projects = _projects(tmp_path)
    _chat(projects, UUID_A)  # a live chat so the keep-set isn't empty
    base = tmp_path / "profile"
    fresh = _profile(base, UUID_B)  # orphan, but just created

    # now == its mtime → age 0 < grace → skipped, not deleted.
    result = profile_gc.sweep_orphan_profiles(str(base), str(projects), now=os.path.getmtime(fresh))

    assert result.skipped_grace == 1
    assert result.deleted == []
    assert fresh.exists()


# --- live-session skip (B.2) ------------------------------------------------------


def test_sweep_skips_an_orphan_whose_external_lock_is_held(tmp_path: Path) -> None:
    projects = _projects(tmp_path)
    _chat(projects, UUID_A)
    base = tmp_path / "profile"
    orphan = _profile(base, UUID_B)  # orphan, but "live"
    lock = profile_lock_path(str(orphan))
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a live Chrome holds it
    try:
        result = profile_gc.sweep_orphan_profiles(str(base), str(projects), now=_PAST_GRACE)
        assert result.skipped_live == 1
        assert result.deleted == []
        assert orphan.exists()  # a live session's profile is never removed
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- atomic rename-then-rm (B.1) --------------------------------------------------


def test_delete_leaves_no_tomb_and_cleans_a_stale_one(tmp_path: Path) -> None:
    projects = _projects(tmp_path)
    _chat(projects, UUID_A)
    base = tmp_path / "profile"
    _profile(base, UUID_B)  # orphan to delete
    # A tomb left by a crashed prior rm — the sweep must finish it, not orphan it.
    stale_tomb = base / f"{profile_gc._TOMB_PREFIX}{UUID_C}"
    stale_tomb.mkdir()
    (stale_tomb / "junk").write_text("x")

    result = profile_gc.sweep_orphan_profiles(str(base), str(projects), now=_PAST_GRACE)

    assert result.deleted == [UUID_B]
    assert not (base / UUID_B).exists()
    assert not stale_tomb.exists()  # stale tomb reclaimed
    assert not any(n.startswith(profile_gc._TOMB_PREFIX) for n in os.listdir(base))


# --- per-run cap (B.5) ------------------------------------------------------------


def test_sweep_caps_deletes_per_run(tmp_path: Path) -> None:
    projects = _projects(tmp_path)
    _chat(projects, UUID_A)  # keep-set non-empty
    base = tmp_path / "profile"
    ids = [f"{i:08d}-1111-4111-8111-111111111111" for i in range(5)]
    for cid in ids:
        _profile(base, cid)

    result = profile_gc.sweep_orphan_profiles(
        str(base), str(projects), now=_PAST_GRACE, max_deletes=2
    )

    assert len(result.deleted) == 2
    assert result.hit_cap is True
    assert sum(1 for cid in ids if (base / cid).exists()) == 3  # rest drain next sweep


# --- EPERM honesty (R2: never a silent skip) --------------------------------------


def test_sweep_counts_eperm_loudly_and_does_not_swallow(tmp_path: Path, monkeypatch: Any) -> None:
    projects = _projects(tmp_path)
    _chat(projects, UUID_A)
    base = tmp_path / "profile"
    _profile(base, UUID_B)

    def boom(_path: str) -> None:
        raise PermissionError(13, "EACCES")  # foreign-owned under per-user isolation

    monkeypatch.setattr(profile_gc.shutil, "rmtree", boom)

    result = profile_gc.sweep_orphan_profiles(str(base), str(projects), now=_PAST_GRACE)

    assert result.denied_perm == 1  # surfaced, not counted as a benign skip
    assert result.deleted == []


# --- path traversal (B.4) ---------------------------------------------------------


def test_delete_one_refuses_a_symlink_escape(tmp_path: Path) -> None:
    base = tmp_path / "profile"
    base.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("keep")
    # A UUID-named entry that is actually a symlink OUT of the profile root.
    os.symlink(outside, base / UUID_B)

    outcome = profile_gc._delete_one(str(base), UUID_B)

    assert outcome == "error"  # realpath check refuses it
    assert (outside / "precious").exists()  # nothing outside the root was touched


# --- run_sweep wrapper ------------------------------------------------------------


def test_run_sweep_is_a_noop_without_a_profile_dir(tmp_path: Path) -> None:
    assert profile_gc.run_sweep(None, str(tmp_path)) is None  # ephemeral/CI


# --- external lock path derivation (B.1) ------------------------------------------


def test_profile_lock_path_is_a_sibling_locks_dir() -> None:
    # .cobrowse/profile/<id>  ->  .cobrowse/locks/<id>.lock  (outside the deletable
    # profile subtree, so a rename-then-rm can't unlink the exclusion inode).
    assert (
        profile_lock_path("/home/agent/.cobrowse/profile/abc")
        == "/home/agent/.cobrowse/locks/abc.lock"
    )
    assert (
        profile_lock_path("/home/agent/.cobrowse/profile/abc/")  # trailing slash
        == "/home/agent/.cobrowse/locks/abc.lock"
    )


# --- observability: the sweep records its outcome as counters ---------------------


def test_sweep_records_deleted_and_denied_perm_counters(tmp_path: Path, monkeypatch: Any) -> None:
    recorded: dict[str, int] = {}

    def capture(*, deleted: int, denied_perm: int, skipped_live: int) -> None:
        recorded.update(deleted=deleted, denied_perm=denied_perm, skipped_live=skipped_live)

    monkeypatch.setattr(profile_gc.metrics, "record_gc_sweep", capture)

    projects = _projects(tmp_path)
    _chat(projects, UUID_A)  # keep-set non-empty
    base = tmp_path / "profile"
    _profile(base, UUID_B)  # one orphan → one delete

    profile_gc.sweep_orphan_profiles(str(base), str(projects), now=_PAST_GRACE)

    assert recorded == {"deleted": 1, "denied_perm": 0, "skipped_live": 0}


def test_aborted_sweep_records_nothing(tmp_path: Path, monkeypatch: Any) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(profile_gc.metrics, "record_gc_sweep", lambda **k: calls.append(k))

    projects = _projects(tmp_path)  # zero chats → fail-closed abort
    base = tmp_path / "profile"
    _profile(base, UUID_B)

    result = profile_gc.sweep_orphan_profiles(str(base), str(projects), now=_PAST_GRACE)

    assert result.aborted is not None
    assert calls == []  # an aborted (no-op) sweep must not touch the counters

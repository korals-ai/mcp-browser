"""Live-on-PVC profile: per-profile flock, boot-time lock sweep, and the one-time
reverse migration from the archive-era layout (profile.tar.gz → live dir).

No Chromium: exercises the pure filesystem logic. Chrome now runs its
user-data-dir directly on the tenant volume — every write durable continuously —
and the two hazards of that placement are what these tests pin: stale Singleton*
locks are cleared only under the profile flock (never a live Chrome's), and an
archive-era profile.tar.gz is extracted once so logins carry over. Real-Chrome
behavior is covered by the chaos targets.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import tarfile
from typing import Any

import pytest

from src.browser_driver import (
    _DURABLE_ARCHIVE,
    PlaywrightDriver,
    clear_stale_profile_locks,
    profile_lock_path,
)


def _make_archive(profile: Any, cookies: str = "prior-logins") -> None:
    """An archive-era durable snapshot: tar of a user-data-dir root."""
    src = profile.parent / f"seed-{profile.name}"
    (src / "Default").mkdir(parents=True, exist_ok=True)
    (src / "Default" / "Cookies").write_text(cookies)
    (src / "Local State").write_text("{}")
    profile.mkdir(parents=True, exist_ok=True)
    with tarfile.open(profile / _DURABLE_ARCHIVE, "w:gz") as t:
        t.add(src, arcname=".")


def _plant_stale_singletons(profile: Any) -> None:
    os.symlink("dead-host-123", profile / "SingletonLock")  # dangling by design
    os.symlink("/tmp/gone/SingletonSocket", profile / "SingletonSocket")


# --- reverse migration (archive era → live dir) -----------------------------------


def test_migrate_extracts_archive_onto_profile_root_and_deletes_it(tmp_path: Any) -> None:
    profile = tmp_path / "profile"
    _make_archive(profile)

    d = PlaywrightDriver(profile_dir=str(profile))
    d._migrate_archived_profile()

    assert (profile / "Default" / "Cookies").read_text() == "prior-logins"  # logins kept
    assert not (profile / _DURABLE_ARCHIVE).exists()  # live dir IS the store now


def test_migrate_is_idempotent_after_a_crash_between_extract_and_delete(tmp_path: Any) -> None:
    # Crash-shape: extract completed, archive not yet deleted → next launch simply
    # re-extracts the same content and then deletes.
    profile = tmp_path / "profile"
    _make_archive(profile)
    d = PlaywrightDriver(profile_dir=str(profile))
    d._migrate_archived_profile()
    _make_archive(profile)  # the archive "reappears" (crash before delete)

    d._migrate_archived_profile()

    assert (profile / "Default" / "Cookies").read_text() == "prior-logins"
    assert not (profile / _DURABLE_ARCHIVE).exists()


def test_migrate_is_a_noop_without_an_archive(tmp_path: Any) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Default").mkdir()
    (profile / "Default" / "Cookies").write_text("live")

    PlaywrightDriver(profile_dir=str(profile))._migrate_archived_profile()

    assert (profile / "Default" / "Cookies").read_text() == "live"  # untouched


def test_migrate_keeps_a_corrupt_archive_and_does_not_raise(tmp_path: Any) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / _DURABLE_ARCHIVE).write_bytes(b"not a tar.gz")

    PlaywrightDriver(profile_dir=str(profile))._migrate_archived_profile()  # must not raise

    # Kept for forensics, not silently deleted — the session starts fresh instead.
    assert (profile / _DURABLE_ARCHIVE).read_bytes() == b"not a tar.gz"


def test_sweep_removes_archive_era_crash_scratch(tmp_path: Any) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".cobrowse_open_tabs.json").write_text("{}")
    (profile / ".profile.tar.gz.tmp-abc123").write_bytes(b"partial")  # old image's crash residue

    PlaywrightDriver(profile_dir=str(profile))._sweep_checkpoint_scratch()

    assert sorted(os.listdir(profile)) == [".cobrowse_open_tabs.json"]


# --- per-profile flock (cross-pod single-writer) ----------------------------------


def test_profile_lock_is_exclusive_and_released_on_close_path(tmp_path: Any) -> None:
    profile = tmp_path / "profile" / "chat-a"
    profile.mkdir(parents=True)

    a = PlaywrightDriver(profile_dir=str(profile))
    a._acquire_profile_lock()
    # The lock lives OUTSIDE the (deletable) profile dir, so the GC can rm the
    # profile without unlinking the exclusion inode mid-delete (B.1).
    assert os.path.exists(profile_lock_path(str(profile)))
    assert not (profile / ".cobrowse_profile.lock").exists()
    b = PlaywrightDriver(profile_dir=str(profile))
    with pytest.raises(RuntimeError, match="in use"):
        b._acquire_profile_lock()  # a live session owns the profile → fail loudly

    a._release_profile_lock()
    b._acquire_profile_lock()  # takeover after release
    b._release_profile_lock()


def test_release_is_safe_when_never_acquired(tmp_path: Any) -> None:
    d = PlaywrightDriver(profile_dir=str(tmp_path / "p"))
    d._release_profile_lock()  # must not raise


# --- boot-time stale-lock sweep ---------------------------------------------------


def test_boot_sweep_clears_stale_singletons(tmp_path: Any) -> None:
    base = tmp_path / "profile"
    profile = base / "chat-a"
    profile.mkdir(parents=True)
    (profile / "Default").mkdir()
    (profile / "Default" / "Cookies").write_text("keep-me")
    _plant_stale_singletons(profile)

    assert clear_stale_profile_locks(str(base)) == 1

    assert not (profile / "SingletonLock").exists()
    assert not (profile / "SingletonSocket").exists()
    assert (profile / "Default" / "Cookies").read_text() == "keep-me"  # data untouched


def test_boot_sweep_skips_a_profile_whose_flock_is_held(tmp_path: Any) -> None:
    # A held flock means a LIVE Chrome (another pod) owns the profile: its
    # Singleton files are legitimate running-state and must not be deleted. The
    # lock is the EXTERNAL inode (profile_lock_path), the same one a launch holds.
    base = tmp_path / "profile"
    live = base / "chat-live"
    live.mkdir(parents=True)
    _plant_stale_singletons(live)  # "live" locks from the sweep's perspective
    live_lock = profile_lock_path(str(live))
    os.makedirs(os.path.dirname(live_lock), exist_ok=True)
    fd = os.open(live_lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        stale = base / "chat-stale"
        stale.mkdir()
        _plant_stale_singletons(stale)

        assert clear_stale_profile_locks(str(base)) == 1  # only the unheld one

        assert os.path.lexists(live / "SingletonLock")  # live profile untouched
        assert not os.path.lexists(stale / "SingletonLock")  # stale profile swept
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_boot_sweep_tolerates_a_missing_base_dir(tmp_path: Any) -> None:
    assert clear_stale_profile_locks(str(tmp_path / "nope")) == 0  # must not raise


# --- launch wiring ----------------------------------------------------------------


def test_start_launches_on_the_profile_dir_itself_and_migrates_first(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The user-data-dir passed to Chrome IS the (tenant-volume) profile dir, the
    archive migrates before launch, and a launch failure releases the flock."""
    profile = tmp_path / "profile" / "chat-a"
    _make_archive(profile)
    d = PlaywrightDriver(profile_dir=str(profile))
    seen: dict[str, Any] = {}

    async def fake_launch(profile_dir: str, w: int, h: int) -> Any:
        seen["user_data_dir"] = profile_dir
        raise RuntimeError("stop here")  # end the test before tab adoption

    monkeypatch.setattr(d, "_launch_persistent", fake_launch)

    import asyncio

    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(d.start())

    assert seen["user_data_dir"] == str(profile)  # live-on-PVC, not a runtime dir
    assert not (profile / _DURABLE_ARCHIVE).exists()  # migration ran before launch
    assert d._profile_lock_fd is None  # failed launch released the flock
    # …and the profile is immediately lockable again (not wedged).
    d._acquire_profile_lock()
    d._release_profile_lock()


def test_tar_binary_is_available() -> None:
    """The migration shells out to tar; the image must carry it (and CI does)."""
    assert subprocess.run(["tar", "--version"], capture_output=True).returncode == 0


# --- per-launch regenerable-cache purge (footprint) -------------------------------


def test_purge_regenerable_cache_drops_cache_keeps_durable_state(tmp_path: Any) -> None:
    from src.browser_driver import purge_regenerable_cache

    prof = tmp_path / "profile" / "chat-a"
    # regenerable cache (must be removed) — at the profile root AND under Default/:
    (prof / "Default" / "Cache" / "Cache_Data").mkdir(parents=True)
    (prof / "Default" / "Cache" / "Cache_Data" / "data_0").write_text("http cache")
    (prof / "Default" / "Code Cache" / "js").mkdir(parents=True)
    (prof / "Default" / "GPUCache").mkdir(parents=True)
    (prof / "GPUCache").mkdir(parents=True)
    (prof / "Default" / "Service Worker" / "CacheStorage" / "x").mkdir(parents=True)
    # durable state (must SURVIVE) — never matched by name:
    (prof / "Default" / "Cookies").write_text("login-cookies")
    (prof / "Default" / "Local Storage" / "leveldb").mkdir(parents=True)
    (prof / "Default" / "IndexedDB" / "db").mkdir(parents=True)
    (prof / "Default" / "Preferences").write_text("{}")

    removed = purge_regenerable_cache(str(prof))

    assert removed >= 5  # Cache, Code Cache, Default/GPUCache, GPUCache, CacheStorage, ...
    assert not (prof / "Default" / "Cache").exists()
    assert not (prof / "Default" / "Code Cache").exists()
    assert not (prof / "Default" / "GPUCache").exists()
    assert not (prof / "GPUCache").exists()
    assert not (prof / "Default" / "Service Worker" / "CacheStorage").exists()
    # durable state preserved:
    assert (prof / "Default" / "Cookies").read_text() == "login-cookies"
    assert (prof / "Default" / "Local Storage" / "leveldb").exists()
    assert (prof / "Default" / "IndexedDB" / "db").exists()
    assert (prof / "Default" / "Preferences").exists()


def test_purge_regenerable_cache_is_a_noop_on_a_clean_profile(tmp_path: Any) -> None:
    from src.browser_driver import purge_regenerable_cache

    prof = tmp_path / "profile" / "chat-b"
    (prof / "Default").mkdir(parents=True)
    (prof / "Default" / "Cookies").write_text("x")

    assert purge_regenerable_cache(str(prof)) == 0  # nothing regenerable to drop
    assert (prof / "Default" / "Cookies").read_text() == "x"

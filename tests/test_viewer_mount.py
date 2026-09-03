"""The bundled viewer is served at ``/`` — and never over the other planes.

The mount is added last and at the root, which is exactly the shape that could
swallow ``/mcp``, ``/cobrowse`` and ``/metrics`` if the ordering ever changed.
These tests pin the ordering, not just the presence of the route: a viewer that
shadowed the agent plane would take the whole tool down while looking healthy.
"""

from __future__ import annotations

import importlib
import pathlib
from typing import Any

import pytest


def _build_with_viewer(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    static = tmp_path / "viewer"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>co-browse</title>")
    (static / "viewer.js").write_text("export {};")
    monkeypatch.setenv("BROWSER_VIEWER_DIR", str(static))
    import src.server as server

    importlib.reload(server)
    return server.build_app()


def _paths(app: Any) -> list[str]:
    return [getattr(r, "path", "") for r in app.router.routes]


def _viewer_index(app: Any) -> int:
    # Starlette normalises Mount("/") to path "", so the route is found by name,
    # not by the string it was declared with.
    for i, r in enumerate(app.router.routes):
        if getattr(r, "name", None) == "viewer":
            return i
    return -1


def test_viewer_mounted_at_root_when_the_bundle_is_present(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _build_with_viewer(tmp_path, monkeypatch)
    assert _viewer_index(app) >= 0


def test_viewer_mount_is_last_so_it_cannot_shadow_the_other_planes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _build_with_viewer(tmp_path, monkeypatch)
    paths = _paths(app)
    root = _viewer_index(app)
    for earlier in ("/cobrowse/{session_id}", "/metrics"):
        assert paths.index(earlier) < root, f"{earlier} must match before the viewer"


def test_no_root_route_without_a_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    # Absence is a supported mode, not a failure: the platform serves its own
    # viewer and builds this image without one.
    monkeypatch.setenv("BROWSER_VIEWER_DIR", "")
    import src.server as server

    importlib.reload(server)
    assert _viewer_index(server.build_app()) < 0


def test_a_configured_but_missing_directory_does_not_crash_the_server(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # StaticFiles raises at construction on a missing dir, which would take the
    # agent plane down with it over a cosmetic feature.
    monkeypatch.setenv("BROWSER_VIEWER_DIR", str(tmp_path / "nope"))
    import src.server as server

    importlib.reload(server)
    assert _viewer_index(server.build_app()) < 0

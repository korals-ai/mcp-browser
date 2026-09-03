"""Per-chat session routing on the browser tool side.

The agent plane resolves the chat's session id from the live MCP request's
``?chat_id=`` (added by the in-pod broker) and, since Pillar C, REJECTS a tool
call that carries no chat id rather than sharing one ``shared`` profile across
chats. A no-HTTP scope (stdio/tests) still yields ``SHARED_SESSION``, and a chat
id used as a profile-dir name is path-sanitized so a hostile id can't escape the
profile root (its safe-landing is still ``SHARED_SESSION``).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel.server import request_ctx

from src import server


def _set_request(query: dict[str, str] | None) -> Any:
    """Point request_ctx at a fake request carrying ``query`` (None => no request
    field). Returns the token to reset with."""
    request = SimpleNamespace(query_params=query) if query is not None else None
    return request_ctx.set(SimpleNamespace(request=request))  # type: ignore[arg-type]


def test_session_id_reads_chat_id_from_request() -> None:
    token = _set_request({"chat_id": "chat-9"})
    try:
        assert server._session_id() == "chat-9"
    finally:
        request_ctx.reset(token)


def test_session_id_falls_back_when_no_request_context() -> None:
    # No MCP request in scope (stdio/tests) -> shared fallback, never a crash.
    assert server._session_id() == server.SHARED_SESSION


def test_session_id_rejects_an_http_tool_call_with_no_chat_id() -> None:
    # Pillar C: a real MCP request (HTTP plane) that carries no chat_id must be
    # REJECTED, not silently landed on the shared profile (cross-chat cookie mix).
    token = _set_request({"session": "tok"})  # some other param, no chat_id
    try:
        with pytest.raises(ToolError):
            server._session_id()
    finally:
        request_ctx.reset(token)


def test_session_id_rejects_an_empty_chat_id() -> None:
    # An explicit but empty ?chat_id= is still "no chat context" -> reject.
    token = _set_request({"chat_id": ""})
    try:
        with pytest.raises(ToolError):
            server._session_id()
    finally:
        request_ctx.reset(token)


def test_session_id_rejects_when_request_is_none() -> None:
    # A request context whose request field is None is the no-HTTP-plane case
    # (stdio) -> shared, never a raise (readiness/probe safety).
    token = request_ctx.set(SimpleNamespace(request=None))  # type: ignore[arg-type]
    try:
        assert server._session_id() == server.SHARED_SESSION
    finally:
        request_ctx.reset(token)


def test_profile_dir_is_per_chat_subdir(monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "PROFILE_DIR", "/vol/.cobrowse/profile")
    assert server._profile_dir_for("chat-9") == "/vol/.cobrowse/profile/chat-9"


def test_profile_dir_none_without_a_mounted_volume(monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "PROFILE_DIR", None)
    assert server._profile_dir_for("chat-9") is None


def test_profile_dir_refuses_path_traversal(monkeypatch: Any) -> None:
    # A hostile chat_id must not escape the profile root: fall back to the shared
    # subdir rather than joining "../../etc" onto BROWSER_PROFILE_DIR.
    monkeypatch.setattr(server, "PROFILE_DIR", "/vol/profile")
    assert server._profile_dir_for("../../etc") == "/vol/profile/shared"
    assert server._profile_dir_for("a/b") == "/vol/profile/shared"
    # "."/".." alone carry NO slash, so the slug regex on its own matched them and
    # would have joined "/vol/profile/.." (escape up one level). Harmless under
    # read/write, catastrophic once the GC can rm/rename — the B.4 gap _safe_dir_name
    # closes.
    assert server._profile_dir_for("..") == "/vol/profile/shared"
    assert server._profile_dir_for(".") == "/vol/profile/shared"


def test_cache_dir_is_per_chat_subdir(monkeypatch: Any) -> None:
    # Per-session cache dir on the emptyDir: concurrent Chromes must NOT share one
    # --disk-cache-dir (corruption + cross-chat cache leak).
    monkeypatch.setattr(server, "CACHE_DIR", "/chromium-cache")
    assert server._cache_dir_for("chat-9") == "/chromium-cache/chat-9"


def test_cache_dir_none_without_a_configured_cache(monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "CACHE_DIR", None)
    assert server._cache_dir_for("chat-9") is None


def test_cache_dir_refuses_path_traversal(monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "CACHE_DIR", "/chromium-cache")
    assert server._cache_dir_for("../../etc") == "/chromium-cache/shared"
    assert server._cache_dir_for("a/b") == "/chromium-cache/shared"
    assert server._cache_dir_for("..") == "/chromium-cache/shared"
    assert server._cache_dir_for(".") == "/chromium-cache/shared"


# --- tenant volume root: derive from the PVC anchor, NOT $HOME --------------------


def test_tenant_volume_root_derives_from_the_pvc_anchor_not_home(monkeypatch: Any) -> None:
    # The browser container runs as user "tool" with HOME=/home/tool, but the
    # operator mounts the tenant PVC at /home/agent and anchors BROWSER_PROFILE_DIR
    # there. Deriving the volume root from $HOME made PROJECTS_ROOT
    # /home/tool/.claude/projects (nonexistent) → the GC read an empty keep-set and
    # fail-closed forever on prod. It MUST come from the profile-dir anchor.
    monkeypatch.setenv("HOME", "/home/tool")
    monkeypatch.setattr(server, "PROFILE_DIR", "/home/agent/.cobrowse/profile")
    assert server._tenant_volume_root() == "/home/agent"
    # trailing slash tolerated
    monkeypatch.setattr(server, "PROFILE_DIR", "/home/agent/.cobrowse/profile/")
    assert server._tenant_volume_root() == "/home/agent"


def test_tenant_volume_root_falls_back_to_home_without_a_volume(
    monkeypatch: Any, tmp_path: Any
) -> None:
    # No volume mounted (CI/local): $HOME and the mount coincide (realpath'd, so a
    # real dir avoids macOS /var→/private/var surprises).
    monkeypatch.setattr(server, "PROFILE_DIR", None)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert server._tenant_volume_root() == os.path.realpath(str(tmp_path))

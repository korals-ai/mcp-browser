"""Read the tenant's portal login credentials from the mounted creds Secret.

The dispatcher materialises the co-browse connector's creds into the per-tenant
``connectors-creds`` Secret, which the operator mounts into this pod at
``$CONNECTORS_CREDS_DIR`` (file-per-key). We read the single
``PORTAL_CREDENTIALS_JSON`` key — a JSON array of ``{portal_id, login_url,
username, password}`` — and index it by ``portal_id``.

Read fresh on each ``browser_login`` so a live creds refresh (the operator
rewrites the Secret; the kubelet updates the mounted file) is picked up without a
pod restart — same live-refresh contract the odoo sidecar relies on. The password
lives ONLY here and in the driver; it never enters the agent's context.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("workspace-tool-browser")

# Mount dir the operator sets (mirrors the connectors-creds contract). The file
# name is the Secret key the browser connector's build_env emits.
_CREDS_DIR_ENV = "CONNECTORS_CREDS_DIR"
_PORTAL_FILE = "PORTAL_CREDENTIALS_JSON"


@dataclass(frozen=True)
class PortalCred:
    portal_id: str
    login_url: str
    username: str
    password: str


def _creds_path() -> Path | None:
    d = os.environ.get(_CREDS_DIR_ENV)
    return Path(d) / _PORTAL_FILE if d else None


def read_portals(path: Path | None = None) -> dict[str, PortalCred]:
    """Return ``{portal_id: PortalCred}`` from the mounted Secret, or ``{}`` when
    absent/malformed (co-browsing is watch-capable with no portals — a missing
    file is normal, not an error)."""
    path = path if path is not None else _creds_path()
    if path is None or not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        log.warning("portal creds unreadable at %s: %s", path, exc)
        return {}
    out: dict[str, PortalCred] = {}
    for entry in raw if isinstance(raw, list) else []:
        try:
            pid = entry["portal_id"]
            out[pid] = PortalCred(
                portal_id=pid,
                login_url=entry["login_url"],
                username=entry["username"],
                password=entry.get("password", ""),
            )
        except (KeyError, TypeError):
            continue  # skip a malformed entry, keep the rest
    return out

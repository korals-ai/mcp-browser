# Toolspace sidecar (browser): a per-tenant tool pod that owns a real
# Chrome (Chrome for Testing) — run HEADED under Xvfb — and exposes it on TWO
# planes over one port (8096):
#   * /mcp       — the agent control plane (FastMCP streamable-HTTP); the in-pod
#                  broker dials it for browser_open/snapshot/click/type.
#   * /cobrowse  — the human view+input plane (WebSocket); the dispatcher proxies
#                  the browser tab here for the live screencast.
#   * /          — a bundled standalone viewer for that plane. The platform
#                  serves its own React viewer and ignores this one; it exists so
#                  the image is useful to someone running it on its own.
# Both drive ONE shared Chrome session per chat — the co-browsing substrate.
# Real Chrome (not Playwright's open-source Chromium) headed under a virtual
# display: proprietary codecs/DRM, and far lower bot-wall friction on the real
# portals (supplier logins, Google) the agent drives on the human's behalf.
#
# Design + rationale: docs/plan/20260706T014659Z-cobrowse-agent-poc.md.
# Runs in its OWN per-tenant pod (pod: browser in the operator roster), NOT
# co-located in the workspace pod — Chromium is far too heavy for the cold-start
# critical path (same reasoning as the file-tool split, Jun 2026).

# The standalone co-browse viewer. Bundled here so the running server can hand a
# browser something to open — a co-browse plane with no client is only half the
# tool. Its protocol/paint/input core is shared source with the platform's React
# viewer, which imports the very same files, so the two cannot drift.
FROM node:22-slim AS viewer-builder

WORKDIR /viewer

COPY viewer/package.json viewer/package-lock.json ./
# ci, not install: honour the lockfile exactly, fail loudly if they disagree.
RUN npm ci --no-audit --no-fund

COPY viewer/tsconfig.json ./
COPY viewer/src ./src
COPY viewer/index.html ./
RUN npx tsc --noEmit \
 && npm run build \
 && cp index.html static/

FROM python:3.12-slim AS py-builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc musl-dev \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim

COPY --from=py-builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=py-builder /usr/local/bin /usr/local/bin

# Chrome for Testing + its system libraries. `playwright install-deps chromium`
# pulls the exact apt package set a Chromium build needs (fonts, nss, x libs,
# …); we then install a PINNED Chrome for Testing binary (real Chrome, driven by
# Playwright via executable_path) and Xvfb so it can run HEADED under a virtual
# display. We deliberately do NOT `playwright install chromium`: the bundled
# open-source Chromium would be ~170 MB of dead weight, since executable_path
# points at the CfT binary instead. Pin resolved + verified 200 against
# https://googlechromelabs.github.io/chrome-for-testing (Stable, 2026-07-28).
ARG CHROME_FOR_TESTING_VERSION=151.0.7922.47
RUN playwright install-deps chromium \
 && apt-get update \
 && apt-get install -y --no-install-recommends xvfb xauth unzip curl ca-certificates \
 && curl -fsSL -o /tmp/chrome.zip \
      "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_FOR_TESTING_VERSION}/linux64/chrome-linux64.zip" \
 && unzip -q /tmp/chrome.zip -d /opt \
 && rm /tmp/chrome.zip \
 && rm -rf /var/lib/apt/lists/*

# Playwright drives this exact binary (executable_path); BROWSER_HEADLESS=false
# makes the driver launch it headed (needs the Xvfb DISPLAY set by the entrypoint).
ENV BROWSER_EXECUTABLE_PATH=/opt/chrome-linux64/chrome
ENV BROWSER_HEADLESS=false

WORKDIR /app

COPY src ./src
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY --from=viewer-builder /viewer/static ./viewer

# Declared, not inferred: the server mounts a viewer only when told where one is.
ENV BROWSER_VIEWER_DIR=/app/viewer
# root-owned, sticky X socket dir so Xvfb (running as the unprivileged tool user)
# places its display socket here without complaint; the entrypoint recreates it
# if the runtime /tmp is a fresh mount.
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh \
 && mkdir -p /tmp/.X11-unix \
 && chmod 1777 /tmp/.X11-unix

ENV PYTHONPATH=/app

# Mirror the workspace pod's unprivileged identity (uid/gid 65532) so any files
# a download writes onto the shared PVC carry the ownership the main container
# expects (fsGroup 65532). See workspace Dockerfile + operator podSpec.
RUN groupadd --system --gid 65532 tool \
 && useradd --system --uid 65532 --gid 65532 --home-dir /home/tool --shell /bin/bash tool \
 && mkdir -p /home/tool \
 && chown -R tool:tool /home/tool /opt/chrome-linux64
ENV HOME=/home/tool

EXPOSE 8096

USER tool

# Headed Chrome needs an X server. The entrypoint starts Xvfb on a fixed display
# and execs the server (see docker-entrypoint.sh). We do NOT use `xvfb-run` — its
# --auto-servernum path hangs at container boot as PID 1, wedging the pod at 0/1.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

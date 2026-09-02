#!/bin/sh
# Start a virtual X server for HEADED Chrome, then exec the co-browse server.
#
# We deliberately do NOT use `xvfb-run`: its `--auto-servernum` path
# (find_free_servernum + the start/retry `wait` loop) HANGS at container boot
# when it is PID 1 grabbing the first display — it leaves Xvfb running but never
# execs the command, so the pod is stuck at 0/1 forever with an empty log (the
# server never starts). Verified in-cluster: the exact server command works the
# moment it is run directly. So we run Xvfb ourselves on a fixed display and
# `exec` the server, which makes it PID 1 (receives SIGTERM for graceful
# shutdown) with DISPLAY set for Playwright's Chrome.
set -e

DISPLAY_NUM=99
# A crash-restart reuses the container fs; clear any stale lock so Xvfb starts.
rm -f "/tmp/.X${DISPLAY_NUM}-lock"

# The X server refuses to create /tmp/.X11-unix as non-root (euid != 0), so we
# make it ourselves (tmp is world-writable) — otherwise Xvfb can't place its unix
# socket there and Chrome has no display to connect to.
mkdir -p /tmp/.X11-unix 2>/dev/null || true
chmod 1777 /tmp/.X11-unix 2>/dev/null || true

# Screen sized to the max human viewport (2560x1600) plus headroom so a maximized
# viewer never clips. Headless Chrome ignores DISPLAY, so this is harmless when
# BROWSER_HEADLESS is unset (tests / local docker).
Xvfb ":${DISPLAY_NUM}" -screen 0 2880x1800x24 -nolisten tcp &

# Wait briefly for the display socket so the first (lazy) Chrome launch has it.
# Bounded: if Xvfb never comes up we still start the server (it binds :8096 and
# stays Ready; only a co-browse session that reaches Chrome would then fail) —
# far better than a stuck-0/1 pod that serves nothing.
i=0
while [ ! -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && [ "$i" -lt 50 ]; do
  i=$((i + 1))
  sleep 0.1
done

export DISPLAY=":${DISPLAY_NUM}"
exec python -m src.server

#!/usr/bin/env bash
# Pre-build gate for the workspace-tool-browser sidecar image. Mirrors the
# workspace gate: ruff format + ruff check + mypy + pip-audit + pytest.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { echo "$(date '+%Y-%m-%d %H:%M:%S') [workspace-tool-browser] $*"; }
fail() { log "FAIL: $*"; exit 1; }

log "Running pre-build checks..."

cd "$SCRIPT_DIR"

VENV="$SCRIPT_DIR/.venv"

if [ ! -x "$VENV/bin/ruff" ]; then
  log "Bootstrapping .venv..."
  uv venv "$VENV" --python python3.12 >&2
  uv pip install --python "$VENV/bin/python" --index-url https://pypi.org/simple/ -e '.[dev]' >&2
fi

pick() {
  if [ -x "$VENV/bin/$1" ]; then
    echo "$VENV/bin/$1"
  elif command -v "$1" >/dev/null 2>&1; then
    command -v "$1"
  fi
}

RUFF="$(pick ruff)"
MYPY="$(pick mypy)"
PYTEST="$(pick pytest)"
PIP_AUDIT="$(pick pip-audit)"

HINT="  Install: $VENV/bin/pip install --index-url https://pypi.org/simple/ -e '.[dev]'"
[ -n "$RUFF" ]      || fail "ruff not found. $HINT"
[ -n "$MYPY" ]      || fail "mypy not found. $HINT"
[ -n "$PYTEST" ]    || fail "pytest not found. $HINT"
[ -n "$PIP_AUDIT" ] || fail "pip-audit not found. $HINT"

log "1/6 Format check (ruff format)..."
"$RUFF" format --check src tests || fail "ruff format (run: ruff format src tests)"
log "  ✓ ruff format passed"

log "2/6 Linting (ruff)..."
"$RUFF" check src tests || fail "ruff"
log "  ✓ ruff passed"

log "3/6 Static typing (mypy)..."
"$MYPY" src || fail "mypy"
log "  ✓ mypy passed"

log "4/6 Dependency CVE scan (pip-audit)..."
PIP_INDEX_URL=https://pypi.org/simple/ "$PIP_AUDIT" --no-deps -r requirements.txt || fail "pip-audit"
log "  ✓ pip-audit passed"

log "5/6 Running unit tests (with coverage)..."
if "$PYTEST" --help 2>&1 | grep "coverage reporting" > /dev/null; then
  "$PYTEST" -q --cov=src --cov-report=term --cov-fail-under=0 || fail "pytest"
else
  log "  (pytest-cov not installed; running without coverage)"
  "$PYTEST" -q || fail "pytest"
fi
log "  ✓ pytest passed"


log "6/6 Viewer typecheck (tsc over viewer/src)..."
# standalone.ts is imported by nothing in chat-ui, so no other gate typechecks
# it; without this step its first checker is the Docker viewer-builder stage —
# after every pre-build gate is green. Guarded like it's chart-render guard:
# skipped without node on PATH, enforced in ci-runner (which carries node).
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  ( cd "$SCRIPT_DIR/viewer" \
      && npm install --no-audit --no-fund >/dev/null 2>&1 \
      && npx tsc --noEmit ) || fail "viewer tsc (cd viewer && npx tsc --noEmit)"
  log "  ✓ viewer tsc passed"
else
  log "  (node not on PATH; viewer tsc skipped — runs in ci-runner)"
fi

log "Pre-build checks complete ✓"

#!/usr/bin/env bash
# SKVM PANEL - universal launcher (no systemd, no root required).
#
# Works in: GitHub Codespaces, rigbox.dev, CodeSandbox, Gitpod, any Linux
# container/VPS shell, and normal Linux hosts. For production servers
# with systemd, install.sh is still the recommended path.
#
#   bash run.sh                 setup (first run) + start panel + worker
#   bash run.sh --setup-only    venv + dependencies only (CI / image build)
#   bash run.sh --no-runtime    never try to start dockerd/podman
#   bash run.sh --help
#
# Environment:
#   SKVM_PORT=5555              panel port (Codespaces: forward 5555)
#   SKVM_ADMIN_USERNAME=admin   first-run admin (default: admin)
#   SKVM_ADMIN_PASSWORD=...     first-run password (recommended: set as a
#                               Codespace/platform secret; otherwise a random
#                               one is written to data/first_admin_credentials.txt)
#   DOCKER_HOST=...             container runtime endpoint (auto-detected:
#                               docker socket, then Podman sockets)
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"
SETUP_ONLY=0
NO_RUNTIME=0
for arg in "$@"; do
  case "$arg" in
    --setup-only) SETUP_ONLY=1 ;;
    --no-runtime) NO_RUNTIME=1 ;;
    --help) sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown argument: $arg (try --help)"; exit 2 ;;
  esac
done

PORT="${SKVM_PORT:-5555}"
DATA_DIR="${SKVM_DATA_DIR:-$APP_DIR/data}"
export SKVM_DATA_DIR="$DATA_DIR"
export SKVM_DB="${SKVM_DB:-$DATA_DIR/skvm.db}"
mkdir -p "$DATA_DIR"
# First run in a fresh clone/codespace: auto-create .env with a generated
# SECRET_KEY and admin credentials, so there is nothing to fill in by hand.
if [[ ! -f .env ]]; then
  _gen() { python3 -c "import secrets; print(secrets.token_urlsafe($1))" 2>/dev/null || openssl rand -hex "$2"; }
  {
    printf 'SECRET_KEY=%s\n' "$(_gen 48 32)"
    printf 'SKVM_PORT=%s\n' "$PORT"
    printf 'SKVM_BIND=0.0.0.0\n'
    printf 'SKVM_BACKEND=auto\n'
    printf 'SKVM_ADMIN_USERNAME=%s\n' "${SKVM_ADMIN_USERNAME:-admin}"
    printf 'SKVM_ADMIN_PASSWORD=%s\n' "${SKVM_ADMIN_PASSWORD:-$(_gen 18 12)}"
  } > .env
  chmod 600 .env 2>/dev/null || true
  echo "[skvm] Created .env with auto-generated SECRET_KEY + admin credentials (chmod 600)."
  echo "[skvm] View login: grep -E '^(SKVM_ADMIN|SECRET_KEY)' .env  (keep this file private, never commit it)"
fi
[[ -f .env ]] && { set -a; . ./.env; set +a; } || true

echo "=== SKVM PANEL launcher ==="
echo "App: $APP_DIR | Data: $DATA_DIR | Port: $PORT"

# ---------------------------------------------------------------- python
PY="python3"
command -v "$PY" >/dev/null 2>&1 || { echo "python3 not found. Install Python 3.10+ first."; exit 1; }
if [[ ! -x venv/bin/python ]]; then
  echo "[skvm] Creating virtualenv..."
  "$PY" -m venv venv || { echo "venv creation failed (install python3-venv)"; exit 1; }
fi
if [[ ! -f venv/.deps-installed || requirements.txt -nt venv/.deps-installed ]]; then
  echo "[skvm] Installing dependencies (first run may take a minute)..."
  venv/bin/python -m pip install -q --upgrade pip
  venv/bin/python -m pip install -q -r requirements.txt
  touch venv/.deps-installed
fi
VPY="$APP_DIR/venv/bin/python"
"$VPY" -m compileall -q app.py job_runner.py node.py core/ || true
if [[ "$SETUP_ONLY" -eq 1 ]]; then echo "[skvm] Setup complete."; exit 0; fi

# ------------------------------------------------- container runtime (VPS)
# Provisioning needs a real container runtime. Detection order:
# reachable endpoint -> dockerd (start if possible) -> podman service.
runtime_up() {
  DOCKER_HOST="${DOCKER_HOST:-}" "$VPY" - <<'PYEOF' >/dev/null 2>&1
import os, sys
sys.path.insert(0, os.getcwd())
from core.docker_engine import client_or_none
sys.exit(0 if client_or_none() else 1)
PYEOF
}
if [[ "$NO_RUNTIME" -eq 0 ]]; then
  if runtime_up; then
    echo "[skvm] Container runtime: reachable (${DOCKER_HOST:-auto-detected socket})"
  elif command -v dockerd >/dev/null 2>&1; then
    echo "[skvm] Starting dockerd in background (log: $DATA_DIR/dockerd.log)..."
    (dockerd >"$DATA_DIR/dockerd.log" 2>&1 &) || true
    for _ in $(seq 1 20); do runtime_up && break; sleep 1; done
    runtime_up && echo "[skvm] dockerd is up." || echo "[skvm] dockerd did not come up (needs privileges?). Panel will run; VPS deploys will report the real error."
  elif command -v podman >/dev/null 2>&1; then
    SOCK_DIR="${XDG_RUNTIME_DIR:-/tmp}/podman"; mkdir -p "$SOCK_DIR"
    export DOCKER_HOST="unix://$SOCK_DIR/podman.sock"
    echo "[skvm] Starting rootless Podman API on $DOCKER_HOST ..."
    (podman system service --time=0 "$DOCKER_HOST" >"$DATA_DIR/podman.log" 2>&1 &) || true
    for _ in $(seq 1 15); do runtime_up && break; sleep 1; done
    runtime_up && echo "[skvm] Podman is up." || echo "[skvm] Podman did not come up (see $DATA_DIR/podman.log). Panel will run; VPS deploys will report the real error."
  else
    echo "[skvm] No Docker/Podman found. Panel will run in panel-only mode:"
    echo "       install Docker (Codespaces: use the Docker-in-Docker devcontainer)"
    echo "       or Podman, then restart. Nothing will be faked in the meantime."
  fi
else
  echo "[skvm] --no-runtime: skipping runtime startup."
fi
export DOCKER_HOST="${DOCKER_HOST:-}"

# ------------------------------------------------------------------ start
echo "[skvm] Initialising database + first admin..."
"$VPY" - <<'PYEOF'
from app import app, bootstrap_admin
from core import db
from core.config import node_token
with app.app_context():
    db.init_db()
    boot = bootstrap_admin()
    db.ensure_local_node(node_token())
    if boot:
        print(f"[skvm] First admin '{boot[0]}' created.")
        print(f"[skvm] Password: read {boot[1]} once (chmod 600), sign in, change it, delete the file.")
        print("[skvm] Tip: next time set SKVM_ADMIN_PASSWORD as an environment secret.")
PYEOF

echo "[skvm] Starting provisioning worker (log: $DATA_DIR/worker.log)..."
"$VPY" job_runner.py >>"$DATA_DIR/worker.log" 2>&1 &
WORKER_PID=$!
cleanup() { kill "$WORKER_PID" "${PANEL_PID:-}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

cat <<EOF
------------------------------------------------------------
 SKVM PANEL is starting
 Local:    http://127.0.0.1:$PORT
 Network:  http://0.0.0.0:$PORT  (Codespaces/rigbox/CodeSandbox:
           use the forwarded 5555 preview URL for this port)
 Health:   http://127.0.0.1:$PORT/healthz
 Logs:     $DATA_DIR/worker.log  (+ this terminal)
 Stop:     Ctrl+C
------------------------------------------------------------
EOF
venv/bin/gunicorn --worker-class eventlet --workers 1 --bind "0.0.0.0:$PORT" --timeout 120 app:app &
PANEL_PID=$!
wait "$PANEL_PID"

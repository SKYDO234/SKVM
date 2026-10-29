#!/usr/bin/env bash
# SKVM PANEL container entrypoint: wait (briefly) for the container
# runtime, start the provisioning worker, then the web panel.
set -Eeuo pipefail
cd /app
mkdir -p "${SKVM_DATA_DIR:-/data}"

# Give a DinD / Podman endpoint a short window to appear; never block
# forever and never pretend it worked.
python - <<'PYEOF' || true
import os, sys, time
sys.path.insert(0, "/app")
try:
    from core.docker_engine import client_or_none, resolve_endpoint
    deadline = time.time() + 30
    while time.time() < deadline:
        if client_or_none():
            print(f"[skvm] Container runtime reachable: {resolve_endpoint()}", flush=True)
            break
        time.sleep(1)
    else:
        print(f"[skvm] Runtime not reachable yet ({resolve_endpoint()}); panel starts anyway, deploys will report the real error.", flush=True)
except Exception as exc:
    print(f"[skvm] Runtime check skipped: {exc}", flush=True)
PYEOF

python job_runner.py >>"${SKVM_DATA_DIR:-/data}/worker.log" 2>&1 &
WORKER_PID=$!
trap 'kill "$WORKER_PID" 2>/dev/null || true' EXIT INT TERM

PORT="${SKVM_PORT:-5555}"
echo "[skvm] SKVM PANEL on 0.0.0.0:$PORT (worker pid $WORKER_PID)"
gunicorn --worker-class eventlet --workers 1 --bind "0.0.0.0:$PORT" --timeout 120 app:app &
PANEL_PID=$!
trap 'kill "$WORKER_PID" "$PANEL_PID" 2>/dev/null || true' EXIT INT TERM
wait "$PANEL_PID"

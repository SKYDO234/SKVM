#!/usr/bin/env bash
# SKVM PANEL verification — static + runtime smoke checks (host-dependent
# backend checks are warnings, never fake passes).
set -u
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ok=0
say(){ printf '[%s] %s\n' "$1" "$2"; }
warn(){ say WARN "$1"; ok=1; }
PY="$APP_DIR/venv/bin/python"; [[ -x "$PY" ]] || PY="python3"
"$PY" -m compileall -q "$APP_DIR/app.py" "$APP_DIR/job_runner.py" "$APP_DIR/node.py" "$APP_DIR/core" && say OK "python compileall" || warn "python compileall failed"
for f in "$APP_DIR/install.sh" "$APP_DIR/setup.sh" "$APP_DIR"/scripts/*.sh; do bash -n "$f" && say OK "bash -n $f" || warn "bash syntax: $f"; done
command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && say OK "docker daemon available" || warn "docker daemon unavailable (panel installs; provisioning will report this honestly)"
command -v incus >/dev/null 2>&1 && incus info >/dev/null 2>&1 && say OK "incus daemon available" || warn "incus daemon unavailable (optional backend)"
[[ -e /dev/kvm ]] && say OK "/dev/kvm present (VM engine possible)" || warn "/dev/kvm absent (VM engine unavailable; containers unaffected)"
for f in "$APP_DIR/run.sh" "$APP_DIR/Dockerfile" "$APP_DIR/docker-compose.yml" "$APP_DIR/docker-compose.dind.yml" "$APP_DIR/.devcontainer/devcontainer.json"; do
  [[ -f "$f" ]] && say OK "container-mode file: ${f##*/}" || warn "missing container-mode file: $f"
done
bash -n "$APP_DIR/run.sh" && say OK "bash -n run.sh" || warn "bash syntax: run.sh"
"$PY" -c "import json; json.load(open('$APP_DIR/.devcontainer/devcontainer.json'))" 2>/dev/null && say OK "devcontainer.json parses" || warn "devcontainer.json invalid"
SKVM_DATA_DIR="$(mktemp -d)" "$PY" - "$APP_DIR" <<'PYEOF' && say OK "pytest smoke suite" || warn "pytest smoke suite failed"
import subprocess, sys
r = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q"], cwd=sys.argv[1])
sys.exit(r.returncode)
PYEOF
exit "$ok"

#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
exec "$APP_DIR/venv/bin/python" -c 'from job_runner import watchdog_once; watchdog_once()'

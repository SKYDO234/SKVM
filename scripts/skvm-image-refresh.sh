#!/usr/bin/env bash
set -Eeuo pipefail
# Incus caches remote images itself; this is a light daemon-side check only.
command -v incus >/dev/null 2>&1 || exit 0
incus image list local: --format json >/dev/null 2>&1 || true
# Docker: report (do not prune automatically — pruning is destructive).
command -v docker >/dev/null 2>&1 && docker images --format '{{.Repository}}:{{.Tag}}' >/dev/null 2>&1 || true
exit 0

#!/usr/bin/env bash
# SKVM container entrypoint: hand over to autosetup.sh so the image and a bare
# sandbox follow exactly the same bring-up path (config, schema, owner, launch).
#
#   docker run -p 8080:8080 -v skvm-data:/data ghcr.io/<owner>/skvm:latest
#   docker run -p 8080:8080 -v skvm-data:/data \
#     -v /var/run/docker.sock:/var/run/docker.sock -e SKVM_BACKEND=docker \
#     ghcr.io/<owner>/skvm:docker
#
# Recognised environment:
#   PORT                       listen port (default 8080)
#   SKVM_INSTANCE_DIR          state dir (default /data) - put a volume here
#   SKVM_ADMIN_USERNAME        owner login (default admin)
#   SKVM_ADMIN_PASSWORD        owner password (default: generated into the volume)
#   SKVM_PANEL_URL             public URL, used for links + cookie policy
#   SKVM_BACKEND               auto | docker | demo | node
#   SKVM_ALLOW_REGISTRATION    true|false (default true; forced false with --expose)
#   SKVM_EXTRA_ARGS            extra autosetup.sh flags, e.g. "--no-demo --quiet"
set -Eeuo pipefail
umask 022

: "${PORT:=8080}"
: "${SKVM_INSTANCE_DIR:=/data}"
export SKVM_INSTANCE_DIR SKVM_VENV="${SKVM_VENV:-/opt/venv}"

if [ ! -w "$SKVM_INSTANCE_DIR" ]; then
  if [ "$(id -u)" = "0" ]; then
    mkdir -p "$SKVM_INSTANCE_DIR" && chown -R 10000:10000 "$SKVM_INSTANCE_DIR" || true
    chmod 0700 "$SKVM_INSTANCE_DIR" 2>/dev/null || true
  else
    printf '[entrypoint] %s is not writable by uid %s - mount a volume there or pass --user\n' \
      "$SKVM_INSTANCE_DIR" "$(id -u)" >&2
    exit 1
  fi
fi

args=(--no-clone --skip-apt --skip-install --fg --port "$PORT")
if [ "${SKVM_SHARE_PORT:-0}" = "1" ]; then args+=(--expose); fi
if [ -n "${SKVM_EXTRA_ARGS:-}" ]; then
  # shellcheck disable=SC2206  # intentional word splitting
  extra=(${SKVM_EXTRA_ARGS})
  args+=("${extra[@]}")
fi

cd /app
printf '[entrypoint] autosetup.sh %s\n' "${args[*]}"
exec bash /app/autosetup.sh "${args[@]}"

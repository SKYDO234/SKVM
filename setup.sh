#!/usr/bin/env bash
# Compatibility shim: Omega Panel V4 operators ran `sudo bash setup.sh`.
# SKVM PANEL's installer is install.sh (Docker by default, Incus preserved).
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh" "$@"

#!/usr/bin/env bash
# Convenience alias: `sudo bash setup.sh` runs the SKVM PANEL installer.
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh" "$@"

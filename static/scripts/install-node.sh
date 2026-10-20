#!/usr/bin/env bash
# SKVM node agent installer — run on a hypervisor host.
#   curl -fsSL https://panel.example.com/static/scripts/install-node.sh | sudo bash -s -- --enroll KEY_ID:SECRET
set -euo pipefail

ENDPOINT="${SKVM_NODE_ENDPOINT:-0.0.0.0:9105}"
AGENT_DIR="${SKVM_AGENT_DIR:-/opt/skvm-node}"
AGENT_URL="${SKVM_AGENT_URL:-https://panel.example.com/static/scripts/skvm-node.py}"
ENROLL=""

while [ $# -gt 0 ]; do
  case "$1" in
    --enroll) ENROLL="$2"; shift 2 ;;
    --endpoint) ENDPOINT="$2"; shift 2 ;;
    --url) AGENT_URL="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

mkdir -p "$AGENT_DIR"
if [ -f "$AGENT_DIR/skvm-node.py" ]; then
  cp "$AGENT_DIR/skvm-node.py" "$AGENT_DIR/skvm-node.py.bak"
fi
curl -fsSL "$AGENT_URL" -o "$AGENT_DIR/skvm-node.py" || cp "$(dirname "$0")/../../agent/skvm-node.py" "$AGENT_DIR/skvm-node.py"
chmod +x "$AGENT_DIR/skvm-node.py"

python3 -m pip install --quiet --upgrade flask requests psutil >/dev/null 2>&1 || true

cat > /etc/systemd/system/skvm-node.service <<UNIT
[Unit]
Description=SKVM node agent
After=network-online.target docker.service
Wants=docker.service

[Service]
Type=simple
Environment=SKVM_NODE_ENDPOINT=$ENDPOINT
Environment=SKVM_AGENT_DIR=$AGENT_DIR
ExecStart=/usr/bin/env python3 $AGENT_DIR/skvm-node.py --listen $ENDPOINT
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now skvm-node.service

if [ -n "$ENROLL" ]; then
  echo "registering with the panel…"
  python3 "$AGENT_DIR/skvm-node.py" --enroll "$ENROLL" --listen "$ENDPOINT" || true
fi

echo
echo "SKVM node agent installed."
echo "  endpoint : $ENDPOINT"
echo "  service  : systemctl status skvm-node"
echo "  logs     : journalctl -u skvm-node -f"
echo "Register the node in the panel under Admin → Hypervisors and paste the key id/secret it prints."

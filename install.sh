#!/usr/bin/env bash
# SKVM PANEL installer — Debian/Ubuntu (apt) and RHEL-family (dnf).
#
#   sudo bash install.sh [--backend docker|incus|both] [--port 5555] [--noninteractive]
#   bash install.sh [--container] [--background]   # Codespaces/containers
#
# Two install modes:
#   systemd   (default on real Linux servers): services in /opt/skvm-panel,
#             requires root + a running systemd.
#   container (auto-detected in GitHub Codespaces, rigbox, CodeSandbox,
#             Gitpod and any environment without systemd): delegates to
#             run.sh — user-space venv install, auto-generated SECRET_KEY
#             + admin credentials in .env, panel + worker on port 5555.
# Default backend is Docker (installed + validated).
# Incus can be installed alongside Docker with --backend both.
# The installer auto-generates SECRET_KEY and first-admin credentials
# into /etc/skvm-panel/skvm.env (reinstalls never rotate them).
# The panel binds 0.0.0.0:5555 via Gunicorn (eventlet, for the SocketIO
# console). Put Nginx/Caddy + HTTPS in front for public deployments and
# firewall 5555 accordingly (see README.md).
set -Eeuo pipefail
trap 'echo "[SKVM] ERROR at line $LINENO: $BASH_COMMAND" >&2' ERR

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${SKVM_INSTALL_DIR:-/opt/skvm-panel}"
SERVICE_USER="${SKVM_SERVICE_USER:-skvm-panel}"
DATA_DIR="/var/lib/skvm-panel"
CONF_DIR="/etc/skvm-panel"
BACKEND="docker"
PORT="${SKVM_PORT:-5555}"
INSTALL_MODE="${SKVM_INSTALL_MODE:-auto}"   # auto | systemd | container
BACKGROUND=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend=*)
      BACKEND="${1#*=}"
      if [[ -z "$BACKEND" ]]; then
        echo "[SKVM] --backend requires a value: docker, incus or both (e.g. --backend docker)" >&2
        exit 2
      fi
      shift ;;
    --backend)
      if [[ $# -ge 2 && "$2" != -* ]]; then
        BACKEND="$2"; shift 2
      else
        # Bare `--backend` (seen in Codespaces): fall back to the documented
        # Docker default instead of crashing on `shift 2`.
        echo "[SKVM] --backend has no value; using the default 'docker'. To choose one: --backend docker|incus|both" >&2
        BACKEND="docker"; shift
      fi ;;
    --noninteractive|-y) shift ;;
    --port=*)
      PORT="${1#*=}"
      if [[ -z "$PORT" ]]; then
        echo "[SKVM] --port requires a value (e.g. --port 5555)" >&2
        exit 2
      fi
      shift ;;
    --port)
      if [[ $# -lt 2 || "$2" == -* ]]; then
        echo "[SKVM] --port requires a value (e.g. --port 5555)" >&2
        exit 2
      fi
      PORT="$2"; shift 2 ;;
    --container) INSTALL_MODE="container"; shift ;;
    --systemd) INSTALL_MODE="systemd"; shift ;;
    --background) BACKGROUND=1; shift ;;
    --help|-h)
      cat <<EOF
SKVM PANEL installer
Usage:
  sudo bash install.sh [--backend docker|incus|both] [--port 5555]   (systemd servers)
  bash install.sh [--container] [--background]                       (Codespaces/containers)
Force a mode with --systemd or --container (or SKVM_INSTALL_MODE=...).
EOF
      exit 0 ;;
    -*)
      echo "[SKVM] Unknown argument: $1" >&2
      echo "[SKVM] Options use double dashes, e.g.: bash install.sh --backend docker (try --help)" >&2
      exit 2 ;;
    *)
      echo "[SKVM] Unknown argument: $1 (try --help)" >&2
      exit 2 ;;
  esac
done
case "$BACKEND" in
  docker|incus|both) ;;
  *) echo "[SKVM] --backend must be docker, incus or both (e.g. --backend docker)" >&2; exit 2 ;;
esac
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "[SKVM] --port must be a number between 1 and 65535 (e.g. --port 5555)" >&2
  exit 2
fi
case "$INSTALL_MODE" in
  auto|systemd|container) ;;
  *) echo "[SKVM] SKVM_INSTALL_MODE must be auto, systemd or container" >&2; exit 2 ;;
esac

# ------------------------------------------------------------- mode detect
systemd_running() {
  command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]] && systemctl is-system-running >/dev/null 2>&1
}
in_container_env() {
  [[ -f /.dockerenv ]] || [[ -n "${CODESPACES:-}" ]] || [[ -n "${REMOTE_CONTAINERS:-}" ]] \
    || [[ -n "${GITPOD_WORKSPACE_ID:-}" ]] || [[ -n "${SKVM_CONTAINER:-}" ]]
}
if [[ "$INSTALL_MODE" == "auto" ]]; then
  if systemd_running && ! in_container_env; then INSTALL_MODE="systemd"; else INSTALL_MODE="container"; fi
fi

install_container() {
  echo "[SKVM] Codespace/container environment detected — using container install mode."
  echo "[SKVM] (No systemd/root needed. For a systemd server, re-run with: sudo bash install.sh --systemd)"
  cd "$APP_DIR"
  if [[ "$BACKEND" == "incus" ]]; then
    echo "[SKVM] NOTE: Incus/KVM cannot run inside most codespaces/containers; switching to the Docker backend."
    export SKVM_BACKEND="docker"
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[SKVM] python3 not found — trying to install it..."
    if command -v apt-get >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
      sudo apt-get update && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip || true
    fi
    command -v python3 >/dev/null 2>&1 || { echo "Install Python 3.10+ (e.g. sudo apt install python3 python3-venv), then re-run: bash install.sh"; exit 1; }
  fi
  if [[ "$(id -u)" -eq 0 && -n "${SUDO_USER:-}" ]]; then
    echo "[SKVM] Tip: you ran with sudo — files will be root-owned. Prefer: bash install.sh (no sudo) in codespaces/containers."
  fi
  if [[ "$BACKGROUND" -eq 1 ]]; then
    mkdir -p data
    echo "[SKVM] Starting SKVM in the background (log: $APP_DIR/data/skvm-run.log)..."
    nohup bash run.sh >data/skvm-run.log 2>&1 &
    echo $! > data/skvm-run.pid
    for _ in $(seq 1 30); do
      curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1
    done
    if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
      echo "[SKVM] Panel is UP on port $PORT."
    else
      echo "[SKVM] Panel is still starting — check: tail -f data/skvm-run.log"
    fi
    cat <<EOF
------------------------------------------------------------
 SKVM PANEL (container mode) installed + started
 URL:      use your platform's forwarded port $PORT preview
           (Codespaces: Ports panel -> 5555 -> Open in Browser)
 Login:    grep -E '^(SKVM_ADMIN|SECRET_KEY)' .env
 Logs:     tail -f data/skvm-run.log   (worker: data/worker.log)
 Stop:     kill \$(cat data/skvm-run.pid)
------------------------------------------------------------
EOF
    exit 0
  fi
  echo "[SKVM] Handing over to run.sh (panel + worker, Ctrl+C to stop)..."
  exec bash run.sh
}

if [[ "$INSTALL_MODE" == "container" ]]; then
  install_container
fi

# ------------------------------------------------------------ systemd mode
if [[ "$(id -u)" -ne 0 ]]; then echo "Run as root: sudo bash install.sh (or use container mode: bash install.sh --container)"; exit 1; fi
if ! systemd_running; then
  echo "systemd is not running on this host, so systemd mode cannot install services."
  echo "Use container mode instead:  bash install.sh --container   (or just: bash run.sh)"
  exit 1
fi

echo "[SKVM] Installing SKVM PANEL (backend: $BACKEND, port: $PORT) from $APP_DIR"

install_apt() { apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"; }
install_dnf() { dnf -y makecache; dnf -y install "$@"; }
PKG_OK=0
if command -v apt-get >/dev/null 2>&1; then
  install_apt ca-certificates curl wget git jq python3 python3-venv python3-pip sudo openssl; PKG_OK=1
elif command -v dnf >/dev/null 2>&1; then
  install_dnf ca-certificates curl wget git jq python3 python3-pip sudo openssl; PKG_OK=1
fi
[[ "$PKG_OK" -eq 1 ]] || { echo "Supported package managers: apt-get or dnf."; exit 1; }

# ---------------------------------------------------------------- Docker
if [[ "$BACKEND" == "docker" || "$BACKEND" == "both" ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "[SKVM] Installing Docker Engine…"
    if command -v apt-get >/dev/null 2>&1; then
      install_apt docker.io || { curl -fsSL https://get.docker.com | sh; }
    else
      install_dnf docker || { curl -fsSL https://get.docker.com | sh; }
    fi
  fi
  systemctl enable --now docker 2>/dev/null || systemctl enable --now docker.service || true
  # LXCFS virtualizes guest /proc so real neofetch/fastfetch/free inside
  # each VPS report the container's own CPU/RAM limits, not host totals.
  if [[ "${SKVM_LXCFS:-true}" != "false" && "${SKVM_LXCFS:-true}" != "0" ]]; then
    if command -v apt-get >/dev/null 2>&1; then
      DEBIAN_FRONTEND=noninteractive apt-get install -y lxcfs >/dev/null 2>&1 || true
    fi
    systemctl enable --now lxcfs >/dev/null 2>&1 || systemctl start lxcfs >/dev/null 2>&1 || true
    if [[ -e /var/lib/lxcfs/proc/meminfo ]]; then
      echo "[SKVM] LXCFS active at /var/lib/lxcfs — guest /proc will be virtualized per-VPS"
    else
      echo "[SKVM] LXCFS not active (package unavailable?) — guests still work; fetch tools show runtime stats without /proc virtualization"
    fi
  fi
  if docker info >/dev/null 2>&1; then
    echo "[SKVM] Docker daemon OK: $(docker --version)"
  else
    echo "[SKVM] WARNING: Docker daemon did not respond. The panel will install,"
    echo "       but provisioning will fail honestly until Docker is running."
    echo "       Check: systemctl status docker"
  fi
fi

# ----------------------------------------------------------------- Incus
if [[ "$BACKEND" == "incus" || "$BACKEND" == "both" ]]; then
  if ! command -v incus >/dev/null 2>&1; then
    echo "[SKVM] Installing Incus…"
    if command -v apt-get >/dev/null 2>&1; then
      apt-get update
      if ! DEBIAN_FRONTEND=noninteractive apt-get install -y incus; then
        install -d -m 0755 /etc/apt/keyrings
        curl -fsSL https://pkgs.zabbly.com/key.asc -o /etc/apt/keyrings/zabbly.asc
        CODENAME="${VERSION_CODENAME:-$(. /etc/os-release; echo "${VERSION_CODENAME:-bookworm}")}"
        printf 'deb [signed-by=/etc/apt/keyrings/zabbly.asc] https://pkgs.zabbly.com/incus/stable %s main\n' "$CODENAME" >/etc/apt/sources.list.d/incus.list
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y incus
      fi
      # VM support (optional, for engine=vm)
      DEBIAN_FRONTEND=noninteractive apt-get install -y qemu-system cpu-checker 2>/dev/null || true
    else
      echo "[SKVM] Incus packages are only automated on apt systems; install Incus manually, then re-run."; exit 1
    fi
  fi
  # Incus mounts the host's LXCFS into system containers automatically.
  if [[ "${SKVM_LXCFS:-true}" != "false" && "${SKVM_LXCFS:-true}" != "0" ]] && command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y lxcfs >/dev/null 2>&1 || true
    systemctl enable --now lxcfs >/dev/null 2>&1 || true
    [[ -e /var/lib/lxcfs/proc/meminfo ]] && echo "[SKVM] LXCFS active — Incus containers get virtualized /proc" || true
  fi
  systemctl enable --now incus 2>/dev/null || systemctl enable --now incus.service 2>/dev/null || true
  if command -v incus >/dev/null 2>&1; then
    incus info >/dev/null 2>&1 || incus admin init --auto || incus admin init --minimal || true
    echo "[SKVM] Incus: $(incus --version 2>/dev/null || echo detected)"
    command -v kvm-ok >/dev/null 2>&1 && echo "[SKVM] $(kvm-ok 2>&1 || true)" || echo "[SKVM] /dev/kvm: $([[ -e /dev/kvm ]] && echo present || echo absent — VM engine unavailable, containers still work)"
  fi
fi

# ------------------------------------------------------------ app install
mkdir -p "$INSTALL_DIR"
if [[ "$APP_DIR" != "$INSTALL_DIR" ]]; then cp -a "$APP_DIR"/. "$INSTALL_DIR"/; fi
id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$DATA_DIR" "$CONF_DIR"
# Backend group access for the service account (takes effect on service start)
getent group docker >/dev/null 2>&1 && usermod -aG docker "$SERVICE_USER" || true
for g in incus-admin incus kvm libvirt; do getent group "$g" >/dev/null 2>&1 && usermod -aG "$g" "$SERVICE_USER" || true; done

PYTHON_BIN="$(command -v python3)"
"$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/python" -m pip install --upgrade pip wheel >/dev/null
"$INSTALL_DIR/venv/bin/pip" install --no-cache-dir -r "$INSTALL_DIR/requirements.txt"

# --- Environment + auto-generated secrets ---------------------------------
# SECRET_KEY and the first-admin credentials are generated here and added
# to /etc/skvm-panel/skvm.env automatically. Re-running the installer never
# rotates them: existing values (env file, secret file, operator-supplied
# environment) always win, so sessions and stored VPS passwords stay valid.
gen_secret() { "$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(48))' 2>/dev/null || openssl rand -hex 32; }
env_val() { [[ -f "$CONF_DIR/skvm.env" ]] && grep -E "^$1=" "$CONF_DIR/skvm.env" | tail -n 1 | cut -d= -f2- || true; }

SECRET_KEY_VALUE="${SECRET_KEY:-${SKVM_SECRET_KEY:-}}"
[[ -n "$SECRET_KEY_VALUE" ]] || SECRET_KEY_VALUE="$(env_val SECRET_KEY)"
[[ -n "$SECRET_KEY_VALUE" ]] || SECRET_KEY_VALUE="$(env_val SKVM_SECRET_KEY)"
if [[ -z "$SECRET_KEY_VALUE" && -f "$DATA_DIR/.secret_key" ]]; then
  SECRET_KEY_VALUE="$(tr -d '[:space:]' < "$DATA_DIR/.secret_key")"
fi
[[ -n "$SECRET_KEY_VALUE" ]] || SECRET_KEY_VALUE="$(gen_secret)"
# Keep the app's secret file in sync with the env value (env wins at runtime).
printf '%s\n' "$SECRET_KEY_VALUE" > "$DATA_DIR/.secret_key"

ADMIN_USER_VALUE="${SKVM_ADMIN_USERNAME:-}"
[[ -n "$ADMIN_USER_VALUE" ]] || ADMIN_USER_VALUE="$(env_val SKVM_ADMIN_USERNAME)"
[[ -n "$ADMIN_USER_VALUE" ]] || ADMIN_USER_VALUE="admin"
ADMIN_PASS_VALUE="${SKVM_ADMIN_PASSWORD:-}"
FRESH_DB=1; [[ -f "$DATA_DIR/skvm.db" ]] && FRESH_DB=0
if [[ -z "$ADMIN_PASS_VALUE" && "$FRESH_DB" -eq 1 ]]; then
  # First install: keep any password already in the env file, otherwise
  # use the documented default admin123 (change it after first login).
  # On reinstall the existing admin account is untouched.
  ADMIN_PASS_VALUE="$(env_val SKVM_ADMIN_PASSWORD)"
  [[ -n "$ADMIN_PASS_VALUE" ]] || ADMIN_PASS_VALUE="admin123"
fi

SKVM_BACKEND_VALUE="auto"
[[ "$BACKEND" == "docker" ]] && SKVM_BACKEND_VALUE="docker"
[[ "$BACKEND" == "incus" ]] && SKVM_BACKEND_VALUE="incus"
{
  printf 'SECRET_KEY=%s\n' "$SECRET_KEY_VALUE"
  printf 'SKVM_HOSTNAME=%s\n' "${SKVM_HOSTNAME:-skvm-node-1}"
  printf 'SKVM_BIND=0.0.0.0\n'
  printf 'SKVM_PORT=%s\n' "$PORT"
  printf 'SKVM_BACKEND=%s\n' "$SKVM_BACKEND_VALUE"
  printf 'SKVM_DATA_DIR=%s\n' "$DATA_DIR"
  printf 'SKVM_DB=%s\n' "$DATA_DIR/skvm.db"
  printf 'SKVM_SECRET_FILE=%s\n' "$DATA_DIR/.secret_key"
  printf 'SKVM_NODE_TOKEN_FILE=%s\n' "$DATA_DIR/.node_token"
  printf 'SKVM_TIMEZONE=%s\n' "${SKVM_TIMEZONE:-UTC}"
  printf 'SKVM_LXCFS=%s\n' "${SKVM_LXCFS:-true}"
  printf 'SKVM_ADMIN_USERNAME=%s\n' "$ADMIN_USER_VALUE"
  [[ -n "$ADMIN_PASS_VALUE" ]] && printf 'SKVM_ADMIN_PASSWORD=%s\n' "$ADMIN_PASS_VALUE"
} > "$CONF_DIR/skvm.env"
chown root:"$SERVICE_USER" "$CONF_DIR/skvm.env"; chmod 0640 "$CONF_DIR/skvm.env"
echo "[SKVM] SECRET_KEY generated + admin credentials (default admin/admin123 unless overridden) written to $CONF_DIR/skvm.env (0640)."

export PYTHONPATH="$INSTALL_DIR"
set -a; . "$CONF_DIR/skvm.env"; set +a
"$INSTALL_DIR/venv/bin/python" - <<'PY'
from app import app, bootstrap_admin
from core import db
from core.config import node_token
with app.app_context():
    db.init_db()
    boot = bootstrap_admin()
    db.ensure_local_node(node_token())
    if boot:
        print(f"[SKVM] First admin '{boot[0]}' created.")
        print(f"[SKVM] Credentials file (chmod 600, read it once, then delete): {boot[1]}")
    else:
        print("[SKVM] Existing database detected; admin credentials unchanged.")
PY
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$DATA_DIR"
chmod 0600 "$DATA_DIR/first_admin_credentials.txt" 2>/dev/null || true
chmod 0600 "$DATA_DIR/.secret_key" "$DATA_DIR/.node_token" 2>/dev/null || true

for unit in skvm-panel.service skvm-jobs.service skvm-node.service skvm-watchdog.service skvm-watchdog.timer skvm-image-refresh.service skvm-image-refresh.timer; do
  install -m 0644 "$INSTALL_DIR/systemd/$unit" "/etc/systemd/system/$unit"
done
# Honour non-default port in the panel unit
if [[ "$PORT" != "5555" ]]; then
  sed -i "s/0.0.0.0:5555/0.0.0.0:$PORT/" /etc/systemd/system/skvm-panel.service
fi
systemctl daemon-reload
systemctl enable --now skvm-panel.service skvm-jobs.service skvm-node.service skvm-watchdog.timer skvm-image-refresh.timer

sleep 2
if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
  echo "[SKVM] Health check OK."
else
  echo "[SKVM] WARNING: panel did not answer /healthz yet. Check: journalctl -u skvm-panel -n 100"
fi

cat <<EOF

============================================================
 SKVM PANEL installed
 Panel:        http://<server-ip>:$PORT  (bind 0.0.0.0:$PORT, Gunicorn)
 Install path: $INSTALL_DIR
 Data / DB:    $DATA_DIR/skvm.db
 Backend:      $BACKEND (panel validates the daemon at deploy time)
 Admin login:  admin / admin123  (unless you supplied SKVM_ADMIN_*)
   View stored values:  sudo grep -E '^(SECRET_KEY|SKVM_ADMIN)' $CONF_DIR/skvm.env
           or: sudo cat $DATA_DIR/first_admin_credentials.txt (chmod 600)
   IMPORTANT: change the default password right after your first login.

 NEXT STEPS
 1. Firewall: allow $PORT/tcp only if exposing the panel directly:
      ufw allow $PORT/tcp   # or your cloud firewall
    Recommended production setup: Nginx/Caddy reverse proxy with HTTPS
    in front, then restrict $PORT to localhost. See README.md.
 2. Sign in (default admin / admin123), change the password immediately,
    then delete the copies:  shred -u $DATA_DIR/first_admin_credentials.txt
    (you may also remove the SKVM_ADMIN_PASSWORD line from skvm.env after
    first login - it is only used when the first admin is created).
 3. Configure branding/plans/OS images under Admin after first login.
 4. Logs: journalctl -u skvm-panel -u skvm-jobs -f
============================================================
EOF

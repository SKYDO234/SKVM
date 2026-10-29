#!/usr/bin/env bash
# SKVM PANEL installer — Debian/Ubuntu (apt) and RHEL-family (dnf), systemd hosts.
#
#   sudo bash install.sh [--backend docker|incus|both] [--noninteractive]
#
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

for arg in "$@"; do
  case "$arg" in
    --backend=*) BACKEND="${arg#*=}" ;;
    --backend) shift; BACKEND="${1:-docker}" ;;
    --noninteractive|-y) ;;
    --port=*) PORT="${arg#*=}" ;;
    --help)
      cat <<EOF
SKVM PANEL installer
Usage: sudo bash install.sh [--backend docker|incus|both] [--port 5555] [--noninteractive]
EOF
      exit 0 ;;
    *) echo "Unknown argument: $arg (try --help)"; exit 2 ;;
  esac
done
case "$BACKEND" in docker|incus|both) ;; *) echo "--backend must be docker, incus or both"; exit 2 ;; esac

if [[ "$(id -u)" -ne 0 ]]; then echo "Run as root: sudo bash install.sh"; exit 1; fi
if ! command -v systemctl >/dev/null 2>&1; then echo "systemd is required."; exit 1; fi

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
gen_admin_password() { "$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(18))' 2>/dev/null || openssl rand -hex 12; }
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
  # Only add a generated password on first install; on reinstall the
  # existing admin account is untouched (bootstrap ignores it anyway).
  ADMIN_PASS_VALUE="$(env_val SKVM_ADMIN_PASSWORD)"
  [[ -n "$ADMIN_PASS_VALUE" ]] || ADMIN_PASS_VALUE="$(gen_admin_password)"
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
  printf 'SKVM_ADMIN_USERNAME=%s\n' "$ADMIN_USER_VALUE"
  [[ -n "$ADMIN_PASS_VALUE" ]] && printf 'SKVM_ADMIN_PASSWORD=%s\n' "$ADMIN_PASS_VALUE"
} > "$CONF_DIR/skvm.env"
chown root:"$SERVICE_USER" "$CONF_DIR/skvm.env"; chmod 0640 "$CONF_DIR/skvm.env"
echo "[SKVM] SECRET_KEY + admin credentials generated into $CONF_DIR/skvm.env (0640)."

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
 Admin login:  username + password were auto-generated for you.
   View them:  sudo grep -E '^(SECRET_KEY|SKVM_ADMIN)' $CONF_DIR/skvm.env
           or: sudo cat $DATA_DIR/first_admin_credentials.txt (chmod 600)

 NEXT STEPS
 1. Firewall: allow $PORT/tcp only if exposing the panel directly:
      ufw allow $PORT/tcp   # or your cloud firewall
    Recommended production setup: Nginx/Caddy reverse proxy with HTTPS
    in front, then restrict $PORT to localhost. See README.md.
 2. Sign in with the generated admin credentials, change the password,
    then delete the copies:  shred -u $DATA_DIR/first_admin_credentials.txt
    (you may also remove the SKVM_ADMIN_PASSWORD line from skvm.env after
    first login - it is only used when the first admin is created).
 3. Configure branding/plans/OS images under Admin after first login.
 4. Logs: journalctl -u skvm-panel -u skvm-jobs -f
============================================================
EOF

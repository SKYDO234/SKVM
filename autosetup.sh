#!/usr/bin/env bash
# ==========================================================================
# SKVM · autosetup.sh
#
# One-shot, non-interactive bootstrap for throwaway Linux sandboxes:
#
#   • rigbox.dev workspaces            (microVM, baked-in `rig`, port routes)
#   • CodeSandbox devboxes             (pairs with .codesandbox/tasks.json)
#   • GitHub Codespaces / devcontainers
#   • plain Debian/Ubuntu containers, CI images, laptops
#
# It installs missing system packages, builds a virtualenv, writes `.env`,
# creates the schema, seeds the owner account (plus a simulated fleet when the
# sandbox has no hypervisor), starts the panel and prints a public URL when the
# platform provides one.  Every step is idempotent, so re-running is safe.
#
#   curl -fsSL https://raw.githubusercontent.com/SKYDO234/SKVM/main/autosetup.sh | bash
#   bash autosetup.sh --help
# ==========================================================================

set -Eeuo pipefail

# --------------------------------------------------------------- defaults ##
REPO_URL="${SKVM_REPO_URL:-https://github.com/SKYDO234/SKVM.git}"
REPO_REF="${SKVM_REPO_REF:-main}"
TARGET_DIR="${SKVM_DIR:-}"
INSTANCE_DIR="${SKVM_INSTANCE_DIR:-}"
PORT="${SKVM_PORT:-${PORT:-8080}}"
HOST="0.0.0.0"
BACKEND="${SKVM_BACKEND:-auto}"
ENV_NAME="${SKVM_ENV:-production}"
PUBLIC_URL="${SKVM_PUBLIC_URL:-}"
ADMIN_USER="${SKVM_ADMIN_USER:-admin}"
ADMIN_PASSWORD="${SKVM_ADMIN_PASSWORD:-}"
ADMIN_EMAIL="${SKVM_ADMIN_EMAIL:-}"
APP_NAME="${SKVM_APP_NAME:-skvm}"
IMAGE_DIR="${SKVM_IMAGE_DIR:-}"
DO_CLONE=1 DO_APT=1 DO_INSTALL=1 DO_SEED=1 DO_DEMO=1 DO_RUN=1
FOREGROUND=0 DO_EXPOSE=0 FORCE_INSTALL=0 QUIET=0

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd -P)"

# ------------------------------------------------------------------ output ##
if [ -t 1 ] && [ "${NO_COLOR:-0}" != "1" ]; then
  C_DIM=$'\033[2m'; C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'
  C_B=$'\033[1m'; C_OFF=$'\033[0m'
else
  C_DIM=""; C_OK=""; C_WARN=""; C_ERR=""; C_B=""; C_OFF=""
fi

say()  { if [ "$QUIET" = 1 ]; then return 0; fi; printf '%s\n' "$*"; }
step() { if [ "$QUIET" = 1 ]; then return 0; fi; printf '\n%s· %s%s\n' "$C_B" "$*" "$C_OFF"; }
ok()   { if [ "$QUIET" = 1 ]; then return 0; fi; printf '  %s+[ok]%s %s\n' "$C_OK" "$C_OFF" "$*"; }
note() { if [ "$QUIET" = 1 ]; then return 0; fi; printf '  %s·%s %s\n' "$C_DIM" "$C_OFF" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
die()  { printf '\n%sautosetup: %s%s\n' "$C_ERR" "$*" "$C_OFF" >&2; exit 1; }

# ------------------------------------------------------------------- usage ##
usage() {
  cat <<'SKVM_HELP'
SKVM autosetup - install, configure and launch the panel in one command.

Invoke
  bash autosetup.sh [options]

  Nothing is interactive. Platform, package manager, sudo, hypervisor
  availability and the public preview URL are all detected.

Options
  --dir PATH          Project directory (default: this script's directory when
                      it is a SKVM checkout, otherwise $HOME/skvm)
  --no-clone          Never git-clone; the repo must already be there
  --repo URL          Git remote to clone from               (default: SKVM)
  --ref REF           Branch/tag/commit to check out        (default: main)
  --port N            Listen port                  (default: $PORT or 8080)
  --host ADDR         Bind address                          (default: 0.0.0.0)
  --backend NAME      auto | docker | demo | node          (default: auto)
  --env-name NAME     production | development      (default: production)
  --instance-dir PATH Persistent state dir        (default: <repo>/instance)
  --public-url URL     External URL - auto-detected on rigbox, CodeSandbox
                      and Codespaces; override it behind another proxy
  --image-dir PATH    Where the driver stores images / backups
  --user NAME         Owner username                         (default: admin)
  --password PASS     Owner password          (default: generated, 0600 file)
  --email ADDR        Owner e-mail                (default: <user>@localhost)
  --app-name NAME     Route name for `rig app new`              (default: skvm)
  --expose            Ask rigbox to publish a public route for the port
  --no-expose         Never touch the rigbox router
  --skip-apt          Do not install system packages
  --skip-install      Reuse the existing virtualenv
  --reinstall         Recreate the virtualenv and rewrite .env
  --no-seed           Do not create/reset the owner account
  --no-demo           Do not seed the simulated fleet
  --no-run            Set everything up but leave the server stopped
  --fg                Run in the foreground (tasks.json, rig.yaml start:)
  --restart           Re-apply config and restart the background server
  --stop              Stop the background server
  --status            Report whether it is running
  --logs              Follow the server log

Examples
  # cold sandbox -> live panel, backgrounded, public route on rigbox
  bash autosetup.sh --expose

  # persistent database, no fake servers
  bash autosetup.sh --instance-dir /var/lib/skvm --no-demo

  # after a snapshot restore / config change
  bash autosetup.sh --restart
SKVM_HELP
}

# ------------------------------------------------------------------- args ##
ACTION="setup"
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)      usage; exit 0 ;;
    --dir)          TARGET_DIR="${2:?--dir needs a path}"; shift 2 ;;
    --repo)         REPO_URL="${2:?--repo needs a url}"; shift 2 ;;
    --ref)          REPO_REF="${2:?--ref needs a ref}"; shift 2 ;;
    --port)         PORT="${2:?--port needs a number}"; shift 2 ;;
    --host)         HOST="${2:?--host needs an address}"; shift 2 ;;
    --backend)      BACKEND="${2:?--backend needs a name}"; shift 2 ;;
    --env-name)     ENV_NAME="${2:?--env-name needs a name}"; shift 2 ;;
    --instance-dir) INSTANCE_DIR="${2:?--instance-dir needs a path}"; shift 2 ;;
    --public-url)   PUBLIC_URL="${2:?--public-url needs a url}"; shift 2 ;;
    --image-dir)    IMAGE_DIR="${2:?--image-dir needs a path}"; shift 2 ;;
    --user)         ADMIN_USER="${2:?--user needs a name}"; shift 2 ;;
    --password)     ADMIN_PASSWORD="${2:?--password needs a value}"; shift 2 ;;
    --email)        ADMIN_EMAIL="${2:?--email needs an address}"; shift 2 ;;
    --app-name)     APP_NAME="${2:?--app-name needs a name}"; shift 2 ;;
    --no-clone)     DO_CLONE=0; shift ;;
    --skip-apt)     DO_APT=0; shift ;;
    --skip-install) DO_INSTALL=0; shift ;;
    --reinstall)    FORCE_INSTALL=1; shift ;;
    --no-seed)      DO_SEED=0; shift ;;
    --no-demo)      DO_DEMO=0; shift ;;
    --no-run)       DO_RUN=0; shift ;;
    --fg)           FOREGROUND=1; shift ;;
    --expose)       DO_EXPOSE=1; shift ;;
    --no-expose)    DO_EXPOSE=0; shift ;;
    --quiet)        QUIET=1; shift ;;
    --restart)      ACTION="restart"; shift ;;
    --stop)         ACTION="stop"; shift ;;
    --status)       ACTION="status"; shift ;;
    --logs)         ACTION="logs"; shift ;;
    *)              die "unknown option '$1' - try: bash autosetup.sh --help" ;;
  esac
done

case "$BACKEND" in auto|docker|demo|node) ;; *) die "--backend must be auto|docker|demo|node" ;; esac
case "$ENV_NAME" in production|development|testing) ;; *) die "--env-name must be production|development|testing" ;; esac
case "$PORT" in ''|*[!0-9]*) die "--port must be a number" ;; esac

# ------------------------------------------------------ privilege handling ##
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    if sudo -n true >/dev/null 2>&1; then SUDO="sudo -n"; else SUDO="sudo"; fi
  fi
fi
run_root() { if [ -n "$SUDO" ]; then $SUDO "$@"; else "$@"; fi; }

# ------------------------------------------------------------ platform map ##
detect_platform() {
  if [ -n "${CODESANDBOX_SSE:-}" ] || [ -n "${CODESANDBOX_HOST:-}" ] \
     || [ -n "${CSB_BASE_PREVIEW_HOST:-}" ] || [ -d /home/codesandbox/.sandbox ]; then
    printf 'codesandbox'; return 0
  fi
  if [ "${CODESPACES:-}" = "true" ] || [ -n "${GITPOD_WORKSPACE_URL:-}" ] || [ -n "${CODESPACE_NAME:-}" ]; then
    printf 'codespaces'; return 0
  fi
  if [ -n "${RIGBOX_WORKSPACE_ID:-}" ] || [ -n "${RIGBOX_WORKSPACE:-}" ] \
     || [ -d /etc/rigbox ] || [ -f /etc/rigbox-release ]; then
    printf 'rigbox'; return 0
  fi
  if command -v rig >/dev/null 2>&1 && rig whoami >/dev/null 2>&1; then
    printf 'rigbox'; return 0
  fi
  if [ -f /.dockerenv ] || grep -qs 'docker\|kubepods\|container' /proc/1/cgroup; then
    printf 'container'; return 0
  fi
  printf 'generic'
}
PLATFORM="$(detect_platform)"

# --------------------------------------------------------- locate / clone ##
REPO_DIR=""
if [ -f "$SCRIPT_DIR/manage.py" ] && [ -d "$SCRIPT_DIR/skvm" ]; then
  REPO_DIR="$SCRIPT_DIR"
elif [ -f "$PWD/manage.py" ] && [ -d "$PWD/skvm" ]; then
  REPO_DIR="$PWD"
fi

if [ -z "$REPO_DIR" ] && [ "$DO_CLONE" = 1 ]; then
  if [ -z "$TARGET_DIR" ]; then TARGET_DIR="$HOME/skvm"; fi
  step "Fetching SKVM"
  command -v git >/dev/null 2>&1 || die "git is required to clone $REPO_URL (or run this inside a checkout)"
  if [ -d "$TARGET_DIR/.git" ]; then
    note "updating the existing checkout at $TARGET_DIR"
    git -C "$TARGET_DIR" fetch --depth 1 origin "$REPO_REF" >/dev/null 2>&1 || true
    git -C "$TARGET_DIR" reset --hard "origin/$REPO_REF" >/dev/null 2>&1 || true
  else
    say "  cloning $REPO_URL ($REPO_REF) into $TARGET_DIR"
    git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$TARGET_DIR" >/dev/null 2>&1 \
      || git clone --branch "$REPO_REF" "$REPO_URL" "$TARGET_DIR"
  fi
  REPO_DIR="$TARGET_DIR"
elif [ -z "$REPO_DIR" ] && [ -n "$TARGET_DIR" ]; then
  REPO_DIR="$TARGET_DIR"
fi
if [ -z "$REPO_DIR" ]; then REPO_DIR="$PWD"; fi
[ -f "$REPO_DIR/manage.py" ] || die "no SKVM checkout at $REPO_DIR - run this from the repo root, or pass --repo"
cd "$REPO_DIR"

INSTANCE_DIR="${INSTANCE_DIR:-$REPO_DIR/instance}"
mkdir -p "$INSTANCE_DIR" 2>/dev/null || true
chmod 700 "$INSTANCE_DIR" 2>/dev/null || true
VENV="$INSTANCE_DIR/venv"
# One pid file per port so two panels sharing a state directory never fight
# over the same process; the log stays shared because it is easier to tail.
PID_FILE="$INSTANCE_DIR/skvm-${PORT}.pid"
LOG_FILE="$INSTANCE_DIR/skvm.log"
CRED_FILE="$INSTANCE_DIR/admin-credentials.txt"
ENV_FILE="$REPO_DIR/.env"

export SKVM_ENV="$ENV_NAME" SKVM_BACKEND="$BACKEND" SKVM_INSTANCE_DIR="$INSTANCE_DIR"
if [ -n "$IMAGE_DIR" ]; then export SKVM_IMAGE_DIR="$IMAGE_DIR"; fi

# -------------------------------------------------- lifecycle shortcuts ----
read_pid() {
  if [ ! -f "$PID_FILE" ]; then return 1; fi
  local pid
  pid="$(tr -dc '0-9' < "$PID_FILE")"
  if [ -z "$pid" ]; then return 1; fi
  kill -0 "$pid" 2>/dev/null || return 1
  printf '%s' "$pid"
}

case "$ACTION" in
  restart)
    # cheap re-apply, then a fresh process (installs are already done)
    DO_APT=0 DO_INSTALL=0 DO_SEED=0 DO_DEMO=0 ACTION="setup"
    ;;
  stop)
    if pid="$(read_pid)"; then
      kill "$pid" 2>/dev/null || true
      for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
      kill -9 "$pid" 2>/dev/null || true
      rm -f "$PID_FILE"
      ok "stopped skvm on port ${PORT} (pid $pid)"
    else
      rm -f "$PID_FILE"
      warn "no running server on port ${PORT}"
    fi
    exit 0 ;;
  status)
    if pid="$(read_pid)"; then
      if health="$(curl -fsS -m 3 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null)"; then
        ok "running · pid ${pid} · ${health:0:150}"
      else
        warn "pid ${pid} is alive but ${PORT}/healthz did not answer - see $LOG_FILE"
      fi
    else
      warn "not running on port ${PORT} - start it with: bash autosetup.sh"
      if [ -f "$LOG_FILE" ]; then note "last log line: $(tail -n 1 "$LOG_FILE" 2>/dev/null)"; fi
    fi
    exit 0 ;;
  logs)
    [ -f "$LOG_FILE" ] || die "no log at $LOG_FILE yet"
    exec tail -n 120 -f "$LOG_FILE" ;;
esac

say "${C_B}SKVM autosetup${C_OFF} ${C_DIM}· platform=${PLATFORM} · port=${PORT} · backend=${BACKEND}${C_OFF}"

# ------------------------------------------------------ system packages ----
PY=""
pick_python() {
  local candidate
  for candidate in "${PYTHON:-}" python3.12 python3.11 python3.10 python3; do
    if [ -z "$candidate" ] || ! command -v "$candidate" >/dev/null 2>&1; then continue; fi
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PY="$(command -v "$candidate")"
      return 0
    fi
  done
  return 1
}

apt_install() {
  local pkg missing=()
  if ! command -v apt-get >/dev/null 2>&1; then
    note "no apt-get available - assuming packages are already installed"
    return 0
  fi
  for pkg in "$@"; do
    if command -v dpkg >/dev/null 2>&1; then
      dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    elif ! command -v "$pkg" >/dev/null 2>&1; then
      missing+=("$pkg")
    fi
  done
  if [ "${#missing[@]}" -eq 0 ]; then
    note "system packages already present"
    return 0
  fi
  step "Installing system packages: ${missing[*]}"
  export DEBIAN_FRONTEND=noninteractive
  run_root apt-get update -qq >/dev/null 2>&1 || warn "apt-get update failed - continuing"
  run_root apt-get install -y --no-install-recommends "${missing[@]}" \
    || warn "apt-get install failed for ${missing[*]} - continuing"
}

HAVE_DOCKER=0
if [ -S /var/run/docker.sock ] || [ -n "${DOCKER_HOST:-}" ] || [ -n "${SKVM_DOCKER_HOST:-}" ]; then
  HAVE_DOCKER=1
fi

step "Prerequisites"
if ! pick_python; then
  if [ "$DO_APT" = 1 ]; then
    apt_install python3 python3-venv python3-pip
    pick_python || die "python 3.10+ is required but was still not found"
  else
    die "python 3.10+ is required (--skip-apt was given)"
  fi
fi
ok "python $("$PY" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))') at $PY"
if ! command -v git >/dev/null 2>&1; then
  if [ "$DO_APT" = 1 ]; then apt_install git; fi
  command -v git >/dev/null 2>&1 || die "git is missing"
fi
if ! command -v curl >/dev/null 2>&1; then
  if [ "$DO_APT" = 1 ]; then apt_install curl ca-certificates; fi
  command -v curl >/dev/null 2>&1 || note "curl is missing - health checks will be skipped"
fi
if ! "$PY" -c 'import venv' >/dev/null 2>&1; then
  if [ "$DO_APT" = 1 ]; then apt_install python3-venv; fi
  "$PY" -c 'import venv' >/dev/null 2>&1 || die "the venv module is unavailable - install python3-venv"
fi
if [ "$HAVE_DOCKER" = 1 ]; then
  ok "docker is reachable - real containers will be scheduled"
else
  note "no docker socket - the panel will serve its simulated driver"
fi

# ------------------------------------------------------------ virtualenv ----
step "Python environment"
if [ "$FORCE_INSTALL" = 1 ] && [ -d "$VENV" ]; then
  note "--reinstall: removing $VENV"
  rm -rf "$VENV"
fi
if [ ! -x "$VENV/bin/python" ]; then
  say "  creating the virtualenv at $VENV"
  "$PY" -m venv "$VENV" 2>/dev/null || "$PY" -m venv --without-pip "$VENV" || die "could not create a virtualenv"
fi
VPY="$VENV/bin/python"
if ! "$VPY" -c 'import pip' >/dev/null 2>&1; then
  note "bootstrapping pip"
  "$VPY" -m ensurepip --upgrade >/dev/null 2>&1 \
    || {
      curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$INSTANCE_DIR/get-pip.py" \
        && "$VPY" "$INSTANCE_DIR/get-pip.py" --quiet && rm -f "$INSTANCE_DIR/get-pip.py"
    } \
    || warn "pip bootstrap failed - install will likely fail"
fi

missing_mod=""
for mod in flask sqlalchemy requests psutil cryptography pyotp qrcode flask_limiter docker; do
  if ! "$VPY" -c "import $mod" >/dev/null 2>&1; then missing_mod="$missing_mod $mod"; fi
done
if [ "$DO_INSTALL" = 1 ] && [ -n "$missing_mod" ]; then
  say "  installing requirements (about a minute on a cold sandbox)"
  "$VPY" -m pip install --quiet --upgrade pip setuptools wheel || warn "pip could not upgrade itself"
  "$VPY" -m pip install --quiet -r requirements.txt || die "pip install -r requirements.txt failed"
  ok "dependencies installed"
else
  if [ -n "$missing_mod" ]; then
    note "skipped pip install (still missing:$missing_mod) - use --reinstall to force it"
  fi
  ok "dependencies already present"
fi

# ------------------------------------------------------------------ .env ----
step "Panel configuration"
if [ "$BACKEND" = "auto" ] && [ "$HAVE_DOCKER" = 0 ]; then
  BACKEND="demo"; export SKVM_BACKEND="demo"
fi
if [ "$HAVE_DOCKER" = 0 ] && [ "$BACKEND" = "docker" ]; then
  warn "SKVM_BACKEND=docker but no docker socket is reachable — create/delete calls will fail with 502 (use --backend auto to fall back to the simulated driver)"
fi

case "$PLATFORM" in
  codesandbox)
    if [ -z "$PUBLIC_URL" ]; then
      if [ -n "${CODESANDBOX_HOST:-}" ]; then
        csb_host="${CODESANDBOX_HOST//\$PORT/$PORT}"
      else
        csb_host="${HOSTNAME:-sandbox}-${PORT}.${CSB_BASE_PREVIEW_HOST:-csb.app}"
      fi
      PUBLIC_URL="https://${csb_host}"
    fi
    ;;
  codespaces)
    if [ -z "$PUBLIC_URL" ]; then
      PUBLIC_URL="https://${CODESPACE_NAME:-codespace}-${PORT}.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
    fi
    ;;
  rigbox)
    if [ -z "$PUBLIC_URL" ]; then PUBLIC_URL="https://${APP_NAME}.rigbox.dev"; fi
    ;;
  *)
    if [ -z "$PUBLIC_URL" ]; then PUBLIC_URL="http://127.0.0.1:${PORT}"; fi
    ;;
esac
SECURE_COOKIES=0
case "$PUBLIC_URL" in https://*) SECURE_COOKIES=1 ;; esac

write_env() {
  local tmp key skip managed line m
  tmp="$(mktemp "${INSTANCE_DIR}/.env.XXXXXX")"
  {
    echo "# Generated by autosetup.sh on $(date -u +'%Y-%m-%dT%H:%M:%SZ') · platform=${PLATFORM}"
    echo "# Anything not re-written below (custom keys) survives a re-run; delete this file to reset."
    echo "SKVM_ENV=${ENV_NAME}"
    echo "SKVM_BACKEND=${BACKEND}"
    echo "SKVM_INSTANCE_DIR=${INSTANCE_DIR}"
    echo "SKVM_PANEL_URL=${PUBLIC_URL}"
    echo "SKVM_PANEL_NAME=${SKVM_PANEL_NAME:-SKVM}"
    echo "SKVM_PANEL_TAGLINE=${SKVM_PANEL_TAGLINE:-Sovereign Cloud Control}"
    echo "SKVM_TRUSTED_PROXY_COUNT=2"
    echo "SKVM_COOKIE_SECURE=${SECURE_COOKIES}"
    echo "SKVM_FORCE_HTTPS=${SECURE_COOKIES}"
    echo "SKVM_ALLOW_REGISTRATION=${SKVM_ALLOW_REGISTRATION:-true}"
    echo "SKVM_REQUIRE_EMAIL_VERIFICATION=false"
    echo "SKVM_REQUIRE_ADMIN_APPROVAL=false"
    echo "SKVM_REAPER=1"
    echo "SKVM_DEMO=1"
    echo "SKVM_MAIL_MODE=log"
    echo "SKVM_METRIC_INTERVAL=10"
    echo "SKVM_RATELIMIT_DEFAULT=${SKVM_RATELIMIT_DEFAULT:-2000 per minute}"
    if [ -n "$IMAGE_DIR" ]; then echo "SKVM_IMAGE_DIR=${IMAGE_DIR}"; fi
  } > "$tmp"
  if [ -f "$ENV_FILE" ]; then
    managed=" SKVM_ENV SKVM_BACKEND SKVM_INSTANCE_DIR SKVM_PANEL_URL SKVM_PANEL_NAME SKVM_PANEL_TAGLINE SKVM_TRUSTED_PROXY_COUNT SKVM_COOKIE_SECURE SKVM_FORCE_HTTPS SKVM_ALLOW_REGISTRATION SKVM_REQUIRE_EMAIL_VERIFICATION SKVM_REQUIRE_ADMIN_APPROVAL SKVM_REAPER SKVM_DEMO SKVM_MAIL_MODE SKVM_METRIC_INTERVAL SKVM_RATELIMIT_DEFAULT SKVM_IMAGE_DIR "
    while IFS= read -r line; do
      case "$line" in ''|\#*) continue ;; *=*) ;; *) continue ;; esac
      key="${line%%=*}"
      skip=0
      for m in $managed; do
        if [ "$key" = "$m" ]; then skip=1; break; fi
      done
      if [ "$skip" = 0 ]; then printf '%s\n' "$line" >> "$tmp"; fi
    done < "$ENV_FILE"
  fi
  chmod 600 "$tmp" 2>/dev/null || true
  if ! mv "$tmp" "$ENV_FILE" 2>/dev/null; then
    # read-only checkout (managed release directories): the app also loads
    # $SKVM_INSTANCE_DIR/.env, so keep the generated config next to the data.
    ENV_FILE="$INSTANCE_DIR/.env"
    mv -f "$tmp" "$ENV_FILE" || die "could not write .env (checked $REPO_DIR and $INSTANCE_DIR)"
  fi
  chmod 600 "$ENV_FILE" 2>/dev/null || true
}
write_env
ok ".env ready at $ENV_FILE · backend=${BACKEND} · url=${PUBLIC_URL} · secure cookies=${SECURE_COOKIES}"

if [ ! -f "$INSTANCE_DIR/secret_key" ] || [ ! -f "$INSTANCE_DIR/encryption_key" ]; then
  if ( cd "$REPO_DIR" && "$VPY" -c 'from skvm.config import resolve_secret_key, resolve_encryption_key
resolve_secret_key()
resolve_encryption_key()' ); then
    ok "signing + Fernet keys created in $INSTANCE_DIR (0600)"
  else
    warn "keys could not be pre-generated; the panel creates them on first boot"
  fi
fi

# ------------------------------------------------------- owner + database --
# The panel bootstraps an owner during its first app start, so the password is
# decided (and exported) before `init-db` runs — that way the first-run banner
# and this script agree on one credential.
if [ "$DO_SEED" = 1 ] && [ -f "$CRED_FILE" ] && [ -z "$ADMIN_PASSWORD" ]; then
  note "owner '${ADMIN_USER}' already exists - keeping its password (rotate with --password)"
  ADMIN_PASSWORD="$(sed -n 's/^[[:space:]]*password[[:space:]]*//p' "$CRED_FILE" | head -1)"
  DO_SEED=0
fi
if [ "$DO_SEED" = 1 ]; then
  if [ -z "$ADMIN_PASSWORD" ]; then
    ADMIN_PASSWORD="$("$VPY" -c 'import secrets; print(secrets.token_urlsafe(12))')"
  fi
  export SKVM_ADMIN_USERNAME="$ADMIN_USER" SKVM_ADMIN_PASSWORD="$ADMIN_PASSWORD"
  if [ -n "$ADMIN_EMAIL" ]; then export SKVM_ADMIN_EMAIL="$ADMIN_EMAIL"; fi
fi

step "Database"
"$VPY" manage.py init-db || die "manage.py init-db failed"

if [ "$DO_SEED" = 1 ]; then
  step "Owner account"
  seed_args=(--username "$ADMIN_USER" --password "$ADMIN_PASSWORD")
  if [ -n "$ADMIN_EMAIL" ]; then seed_args+=(--email "$ADMIN_EMAIL"); fi
  "$VPY" manage.py seed "${seed_args[@]}" >/dev/null || die "manage.py seed failed"
  {
    echo "SKVM sandbox owner · written $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    echo "  url       ${PUBLIC_URL}"
    echo "  username  ${ADMIN_USER}"
    echo "  password  ${ADMIN_PASSWORD}"
    echo "  delete this file once you have copied it"
  } > "$CRED_FILE"
  chmod 600 "$CRED_FILE" 2>/dev/null || true
  ok "owner '${ADMIN_USER}' ready · password stored in $CRED_FILE"
fi

if [ "$DO_DEMO" = 1 ]; then
  step "Demo fleet"
  if "$VPY" manage.py demo-data --servers 5 >/dev/null 2>&1; then
    ok "simulated servers and 36 h of metric history seeded"
  else
    note "demo data skipped (already seeded, or the driver declined)"
  fi
fi

# ---------------------------------------------------------- helpers: run ---
expose_route() {
  if [ "$DO_EXPOSE" != 1 ]; then return 0; fi
  if [ "$PLATFORM" != "rigbox" ]; then
    note "--expose only applies to rigbox workspaces (found: ${PLATFORM})"
    return 0
  fi
  if ! command -v rig >/dev/null 2>&1; then
    warn "no rig CLI on PATH - publish it yourself: rig app new --name ${APP_NAME} --port ${PORT}"
    return 0
  fi
  step "Publishing a rigbox route"
  if rig app new --name "$APP_NAME" --port "$PORT" >/dev/null 2>&1; then
    ok "https://${APP_NAME}.rigbox.dev -> port ${PORT}"
  else
    warn "route not created - retry with: rig app expose-port --port ${PORT} --name ${APP_NAME} --wrap"
  fi
  return 0
}

wait_ready() {
  local tries="$1" i=0
  if ! command -v curl >/dev/null 2>&1; then sleep 4; return 0; fi
  while [ "$i" -lt "$tries" ]; do
    if curl -fsS -m 2 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then return 0; fi
    if [ -n "${SERVER_PID:-}" ] && ! kill -0 "$SERVER_PID" 2>/dev/null; then return 1; fi
    i=$((i + 1))
    sleep 1
  done
  return 1
}

report() {
  local backend_note
  if [ "$HAVE_DOCKER" = 1 ]; then
    backend_note="${BACKEND} ${C_DIM}(real containers)${C_OFF}"
  else
    backend_note="${BACKEND} ${C_DIM}(simulated fleet - no hypervisor in this sandbox)${C_OFF}"
  fi
  printf '\n%sSKVM is live%s\n\n' "$C_B" "$C_OFF"
  printf '  panel        %s\n' "$PUBLIC_URL"
  printf '  api docs     %s/docs/\n' "$PUBLIC_URL"
  printf '  status page  %s/status\n' "$PUBLIC_URL"
  printf '  prometheus   %s/metrics\n' "$PUBLIC_URL"
  printf '  health       %s/healthz\n\n' "$PUBLIC_URL"
  printf '  username     %s%s%s\n' "$C_B" "$ADMIN_USER" "$C_OFF"
  printf '  password     %s%s%s\n\n' "$C_B" "${ADMIN_PASSWORD:-see $CRED_FILE}" "$C_OFF"
  printf '  repo         %s\n' "$REPO_DIR"
  printf '  state        %s %b(db, keys, backups, log)%b\n' "$INSTANCE_DIR" "$C_DIM" "$C_OFF"
  printf '  log          %s\n' "$LOG_FILE"
  printf '  backend      %b\n' "$backend_note"
  printf '  platform     %s\n\n' "$PLATFORM"
  printf '  provision a VPS:  %s/servers/new\n' "$PUBLIC_URL"
  printf '  restart:          bash autosetup.sh --restart\n'
  printf '  follow the log:   bash autosetup.sh --logs\n'
}

# ---------------------------------------------------------------- launch ---
SERVE_ARGS=(manage.py serve --host "$HOST" --port "$PORT" --threads 16)
export SKVM_REAPER=1

if [ "$DO_RUN" = 0 ]; then
  step "Ready (server left stopped)"
  say "  start it later:  cd $REPO_DIR && $VENV/bin/python manage.py serve --port $PORT"
  say "  credentials:     cat $CRED_FILE"
  exit 0
fi

if [ "$FOREGROUND" = 1 ]; then
  expose_route
  say "  ${C_DIM}serving on ${HOST}:${PORT} in the foreground - Ctrl+C stops it${C_OFF}"
  # exec() keeps our pid, so record it - that lets `--status` see a panel that
  # a task runner supervises in the foreground.
  echo $$ > "$PID_FILE" 2>/dev/null || true
  exec "$VPY" "${SERVE_ARGS[@]}"
fi

step "Starting the panel"
if pid="$(read_pid)"; then
  note "restarting the existing server (pid $pid)"
  kill "$pid" 2>/dev/null || true
  sleep 1
fi
: > "$LOG_FILE"
nohup "$VPY" "${SERVE_ARGS[@]}" >>"$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

if ! wait_ready 60; then
  warn "the panel did not answer /healthz in time - last log lines:"
  tail -n 25 "$LOG_FILE" >&2 || true
  die "full log: $LOG_FILE"
fi
ok "pid ${SERVER_PID} · listening on ${HOST}:${PORT}"

expose_route
report

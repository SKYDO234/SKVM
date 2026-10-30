# SKVM PANEL

**SKVM PANEL — Nebula Glass.** A VPS hosting and management platform with a glassmorphic user/admin experience, real provisioning with hard resource limits, a live browser console, and tmate SSH access. Built exclusively for virtual servers — no game-hosting features, no simulated data.

![Stack](https://img.shields.io/badge/stack-Python%20%C2%B7%20Flask%20%C2%B7%20Docker%20%2B%20Incus-67e8f9)

---

## Highlights

| Area | How SKVM does it |
|---|---|
| Backends | **Docker** (Docker SDK for Python) and **Incus** (system containers + QEMU/KVM virtual machines). Auto mode picks a daemon that actually responds; a deploy fails honestly when none is available. |
| OS catalogue | Ubuntu 20.04 / 22.04 / 24.04, Debian 11 / 12, each mapped to its Incus and Docker image references. Admins can add or disable images. |
| Resource enforcement | Server-side on every deploy: plan caps → admin sanity bounds → per-user quota → real node capacity (host CPU/RAM/disk). Docker receives `nano_cpus`/`mem_limit`; disk uses overlay2 `storage_opt.size` where the driver supports it (jobs report truthfully when it cannot). Incus receives `limits.cpu`, `limits.memory` and a sized root device. |
| Console | Flask-SocketIO + xterm.js terminal attached to the server itself (`docker exec` TTY / `incus exec`). Latency is a measured SocketIO round-trip. Authentication and ownership are checked on connect, input and resize. |
| Guest resource view | **LXCFS** virtualizes guest `/proc` (`cpuinfo`, `meminfo`, `uptime`, `stat`, …) from each container’s cgroup, so the real `neofetch`/`fastfetch`/`free` inside a VPS report its own CPU/RAM limits instead of host-node totals. Docker containers get the LXCFS proc files bind-mounted automatically (plus a `cpuset` matching the vCPU allocation, since a CPU quota alone does not change the visible CPU count); Incus uses the host’s LXCFS automatically. No panel-written fetch wrappers. The admin dashboard shows LXCFS status honestly when it is unavailable. |
| SSH access | Provisioning installs OpenSSH + tmate and **creates the keys**: `ssh-keygen -A` host keys for sshd and a passphrase-less root identity (`id_ed25519`, RSA fallback) that tmate’s SSH client requires. Starting a session re-checks/creates missing keys (repairing older guests), waits a bounded time for tmate readiness, and returns the real connection string from the guest — or guest-side diagnostics when tmate.io is unreachable. Root passwords are generated per server, set with `chpasswd`, stored Fernet-encrypted, and resettable. |
| Honest monitoring | Host figures are labelled as host figures. Per-server usage is read from the guest itself (daemon stats, `/proc`, `df`, `free`). Nothing is fabricated. |
| Provisioning engine | Dedicated worker (`skvm-jobs`) with queued/running/done/error jobs, live progress polling, idempotent retry (existing instances are adopted, not duplicated), automatic cleanup of half-created instances, and a 60-second watchdog that re-syncs state with the backend. |
| Admin console | Branding (site name, logo, accent colour, website/Discord links, support email), maintenance mode, registration toggle, users (role/plan/quota/suspend/password/delete), servers (create-for-user, live resize, delete/force-delete), nodes, plans, OS images, plus provisioning jobs, audit log and application log. |
| Security | Flask-Login sessions (HttpOnly, SameSite=Lax), Werkzeug password hashing, CSRF tokens on every form, rate-limited auth and deploy endpoints, suspended-account lockout, full audit trail. Secrets live only in environment variables or 0600 files — the documented first-login admin password is `admin123` and the panel warns until it is changed. Backend calls use argument lists through the Docker SDK / Incus CLI; guest shells only receive panel-generated scripts with validated, quoted values. |

## Stack

Python 3 · Flask · Flask-Login · Flask-SQLAlchemy · Docker SDK for Python (Docker + Podman runtimes) · Flask-SocketIO · xterm.js · Tailwind CSS · Lucide icons · python-dotenv · Gunicorn (eventlet worker) · SQLite (`skvm.db`) · systemd or `run.sh` anywhere

SKVM's database schema (`users`, `nodes`, `vps`, `jobs`, `settings`, plus `plans`, `os_images`, `audit_logs`) upgrades older SKVM databases additively at startup — no destructive migrations.

---

## Install in GitHub Codespaces (and other containers)

`install.sh` works there too — it auto-detects Codespaces/rigbox/CodeSandbox/Gitpod (no systemd) and switches to container mode (no root, no systemd, user-space install via `run.sh`):

```bash
bash install.sh                # foreground (Ctrl+C stops panel + worker)
bash install.sh --background   # install + start detached, prints login/log commands
```

Force a mode any time with `--container` or `--systemd` (`SKVM_INSTALL_MODE=container|systemd` also works). Sign in as `admin` / `admin123` (change it immediately in Profile), then open the forwarded **5555** preview (Codespaces: *Ports → 5555 → Open in Browser*). Hosted preview proxies (Codespaces/Gitpod/dev containers) are handled automatically — SKVM trusts their `X-Forwarded-Host`/`X-Forwarded-Proto` headers so the public preview URL passes the CSRF referrer check; you do **not** need to set `SKVM_TRUST_PROXY` there. (If you ever see *"Bad Request — The referrer does not match the host"* on login, update/restart the panel so this auto-detection is running.)

## Install (production, systemd host)

```bash
git clone https://github.com/SKYDO234/SKVM.git
cd SKVM
sudo bash install.sh                      # Docker backend (default)
sudo bash install.sh --backend both       # Docker + Incus
sudo bash install.sh --backend incus      # Incus only
sudo bash setup.sh                        # alias for install.sh
```

The installer:

1. Installs host packages (Python, venv tooling, certificates, utilities).
2. Installs and validates Docker and/or Incus. For Incus it also checks KVM — VM deploys fail cleanly on hosts without `/dev/kvm` (common on nested VPS), while containers keep working.
3. Copies the application to `/opt/skvm-panel`, creates the `skvm-panel` service user (added to the `docker` / `incus-admin` groups), and builds the Python virtualenv.
4. **Auto-generates your secret and stores your login config for you** — no manual `.env` editing: a random `SECRET_KEY` is generated and added to `/etc/skvm-panel/skvm.env` (0640). The first admin defaults to `admin` / `admin123` unless you set `SKVM_ADMIN_USERNAME`/`SKVM_ADMIN_PASSWORD` before installing; the credentials are also recorded in `/var/lib/skvm-panel/first_admin_credentials.txt` (chmod 600) and the database at `/var/lib/skvm-panel/skvm.db` is initialised. Re-running the installer never rotates the key or touches the existing admin. **Change `admin123` immediately after first sign-in.**
5. Enables the systemd services:

| Unit | Role |
|---|---|
| `skvm-panel.service` | Web panel — Gunicorn eventlet on **0.0.0.0:5555** (single worker, required for the SocketIO console) |
| `skvm-jobs.service` | Provisioning worker |
| `skvm-node.service` | Node agent API on `127.0.0.1:5556` (private, token-authenticated) |
| `skvm-watchdog.timer` | State reconciliation every 60 s |
| `skvm-image-refresh.timer` | Image refresh check every 6 h |

After installation:

```bash
# Sign in: admin / admin123 (or your SKVM_ADMIN_* overrides), then change the password
sudo cat /var/lib/skvm-panel/first_admin_credentials.txt            # recorded first-admin credentials (chmod 600)
sudo shred -u /var/lib/skvm-panel/first_admin_credentials.txt       # after signing in + changing password
curl http://127.0.0.1:5555/healthz
journalctl -u skvm-panel -u skvm-jobs -f
```

### Firewall

Direct exposure: `sudo ufw allow 5555/tcp` (plus your cloud provider's firewall). Never expose the node-agent port (5556), the Docker socket, or the Incus socket publicly.

### Reverse proxy + HTTPS (recommended)

Point `SKVM_BIND=127.0.0.1` in `/etc/skvm-panel/skvm.env`, restart `skvm-panel`, set `SKVM_COOKIE_SECURE=true` and `SKVM_TRUST_PROXY=true`, then proxy HTTPS to the panel — including the WebSocket upgrade for the console:

```nginx
server {
    listen 443 ssl;
    server_name panel.example.com;
    ssl_certificate     /etc/letsencrypt/live/panel.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/panel.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:5555;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location /socket.io/ {
        proxy_pass http://127.0.0.1:5555;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
}
```

Certificates: `certbot --nginx -d panel.example.com`, or use Caddy for automatic TLS.

## Run anywhere — Codespaces, rigbox, CodeSandbox, any Linux container

One command, no systemd, no root:

```bash
git clone https://github.com/SKYDO234/SKVM.git && cd SKVM
bash run.sh        # sets up, starts panel + provisioning worker on 5555
```

`run.sh` creates the virtualenv, installs dependencies, auto-creates a
`.env` with a generated `SECRET_KEY` on first run (admin login defaults to `admin` / `admin123`),
initialises the database and first admin, auto-detects the container runtime (Docker
socket → `dockerd` → rootless Podman), starts the provisioning worker,
and serves the panel on **0.0.0.0:5555**. Forward port **5555** in your
platform to get the preview URL. Sign in as `admin` / `admin123` and
change the password immediately; set `SKVM_ADMIN_PASSWORD` before first
run if you prefer a custom initial password.

| Platform | Steps | VPS provisioning |
|---|---|---|
| **GitHub Codespaces** | [![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/SKYDO234/SKVM) — or *Code → Codespaces → Create*. The included `.devcontainer` enables Docker-in-Docker, forwards 5555 with preview, and runs setup automatically. Then run the **“SKVM PANEL: Run”** VS Code task (or `bash run.sh`). | ✅ Real — Docker-in-Docker is enabled by the devcontainer |
| **rigbox.dev** | Open a box, `git clone … && cd SKVM && bash run.sh`. If Docker/Podman isn't preinstalled, the launcher tries to start `dockerd`/rootless Podman itself. | ✅ When Docker or Podman is available/installable on the box |
| **CodeSandbox** | Import the repo — `.codesandbox/tasks.json` installs deps and runs `bash run.sh` with a 5555 preview. CodeSandbox can also read the `.devcontainer`. | ✅ On VM-based devboxes with Docker; otherwise panel-only (see below) |
| **Gitpod** | Open the repo — `.gitpod.yml` runs setup + `bash run.sh`, preview on 5555. | ✅ Gitpod workspaces include Docker |
| **Any Docker host** | `docker compose up -d --build` (host socket mode) or `docker compose -f docker-compose.dind.yml up -d --build` (privileged DinD — isolated sandboxes only). Sign in as `admin` / `admin123` and change it immediately (or set `SKVM_ADMIN_PASSWORD` first). | ✅ Socket mode = sibling containers on the host daemon |
| **Plain Linux VPS (systemd)** | `sudo bash install.sh` — see above. | ✅ Docker and/or Incus, incl. KVM VMs on Incus |

**Honest limits:** a "VPS" here is a real Docker/Podman container (or
Incus instance) with hard CPU/RAM limits — that requires a working
container runtime. If a sandbox forbids Docker/Podman entirely (no
daemon, no privileges), the panel still runs and every deploy reports
the real "runtime not available" error instead of faking a server.
Rootless Podman (`podman system service`) is auto-detected via
`DOCKER_HOST`/socket paths, which covers many unprivileged containers
where `dockerd` cannot run.

## Local development

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt pytest
cp .env.example .env
SKVM_ADMIN_PASSWORD='choose-a-strong-one' venv/bin/python app.py   # http://localhost:5555 (omit to use admin123)
venv/bin/python -m pytest tests/ -q
bash scripts/verify.sh
```

Without Docker or Incus the panel still runs — deploys then fail with the real "no backend available" error, by design. Start Docker (or Incus) and the same code provisions real servers.

## Configuration

All configuration is via `SKVM_*` environment variables (`.env` locally, `/etc/skvm-panel/skvm.env` in production). See `.env.example` and `core/config.py`. Key options:

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | generated → 0600 file | Session secret; also derives the guest-password encryption key |
| `SKVM_BIND` / `SKVM_PORT` | `0.0.0.0` / `5555` | Panel listen address |
| `SKVM_BACKEND` | `auto` | `auto` · `docker` · `incus` |
| `SKVM_DB` | `data/skvm.db` | SQLite database path |
| `SKVM_ADMIN_USERNAME` / `SKVM_ADMIN_PASSWORD` | `admin` / `admin123` | First-run admin only — change `admin123` immediately |
| `SKVM_COOKIE_SECURE` | `false` | Enable behind HTTPS |
| `SKVM_TRUST_PROXY` | auto (on in Codespaces/Gitpod/dev containers) | Trust `X-Forwarded-*` from a reverse proxy / hosted preview; set explicitly `true`/`false` to override the auto-detection |
| `SKVM_LXCFS` | `true` | Virtualize guest `/proc` via LXCFS so guest fetch tools show per-VPS CPU/RAM. `install.sh` installs + enables it on systemd hosts; `run.sh`/entrypoint start it where FUSE is available. Set `false` to disable. In Codespaces the devcontainer has no FUSE device, so LXCFS stays off there (the admin page reports this) — use `install.sh` on a real host for virtualized guest stats |
| `SKVM_ALLOW_REGISTRATION` | `true` | Public sign-ups (also toggleable in Admin) |

Branding, plans, OS images, maintenance mode and quotas are runtime settings managed in **Admin → Control Room**, stored in the database — no code changes needed.

## Project layout

```
app.py               Flask app, routes, admin, SocketIO console
job_runner.py        provisioning worker + watchdog
node.py              private node agent API (127.0.0.1:5556)
core/
  config.py          SKVM_* environment configuration, secret handling
  models.py          SQLAlchemy models
  db.py              data-access layer, seeds, migrations, audit log
  backend.py         unified backend dispatcher (create/status/action/stats/exec)
  docker_engine.py   Docker backend (limits, exec, stats, cleanup)
  engine.py          Incus backend (containers + QEMU/KVM VMs)
  provision.py       provisioning pipeline + reconciliation
  tmate.py           in-guest tmate + SSH key creation / session start / stop
  capabilities.py    host snapshot + Docker/Incus/KVM/LXCFS probes
  os_profiles.py     Ubuntu/Debian image catalogue
  crypto.py          Fernet encryption for guest root passwords
  extensions.py      Flask extension singletons
templates/           Tailwind + Lucide interface; console.html is the web terminal
static/              skvm.css, skvm.js
systemd/             skvm-*.service and .timer units
scripts/             guest provisioning, watchdog, image refresh, verify.sh
tests/               pytest suite (auth, CSRF, RBAC, limits, provisioning, console)
install.sh           production installer (setup.sh is an alias)
```

## Day-to-day operations

- **Logs:** Admin → Jobs & Logs (provisioning errors with the real backend message, audit trail, log tail) or `journalctl -u skvm-panel -u skvm-jobs -f`.
- **Backup:** stop the services (or use SQLite online backup) and copy `/var/lib/skvm-panel/skvm.db` plus `/etc/skvm-panel/skvm.env`. Keep the secret file with the backup — guest passwords are encrypted with a key derived from it.
- **A deploy says "No provisioning backend is available":** check `systemctl status docker` / `incus info`, confirm the `skvm-panel` user is in the `docker`/`incus-admin` group, restart `skvm-jobs`.
- **VM deploy fails on KVM:** expected without `/dev/kvm`. Use the container engine or a host with nested virtualisation enabled.
- **Docker disk quota not enforced:** the job detail states this explicitly. Hard quotas need overlay2 on XFS with `pquota`; CPU and RAM limits are always hard.
- **Console will not connect:** the server must be `running`; behind a proxy, confirm the `/socket.io/` upgrade block; then check `journalctl -u skvm-panel`.

## License

SKVM PANEL — VPS cloud management. All rights reserved by the SKVM project. Review your obligations for the third-party libraries in `requirements.txt` before redistribution.

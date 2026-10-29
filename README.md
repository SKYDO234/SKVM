# SKVM PANEL

**SKVM PANEL** is a production-conscious VPS hosting and management panel: user and admin dashboards, real container/VM provisioning with hard resource limits, a live web console, and tmate SSH access — for **VPS hosting only** (no Minecraft or game-server features).

It is built on the **Omega Panel V4** codebase (Incus provisioning engine, worker/watchdog design, capability detection), rebranded and extended for SKVM. Upstream attribution is retained in [`UPSTREAM-NOTICE.txt`](UPSTREAM-NOTICE.txt) (Omega Panel V4 © MrZetrix; original reference credit NafiGamer). Omega's obfuscated `legacy_v3/` payloads were intentionally **not** carried over.

---

## What it does

| Area | Implementation (all real, no simulated data) |
|---|---|
| Backends | **Docker** (Docker SDK for Python) and **Incus** (preserved Omega engine: LXC containers + QEMU/KVM VMs). Auto mode prefers Incus, falls back to Docker. Deploy fails honestly when no daemon responds. |
| OS images | Ubuntu 20.04 / 22.04 / 24.04, Debian 11 / 12 — each mapped to its Incus (`images:…`) and Docker Hub image. Admins can add/disable images. |
| Resource enforcement | Server-side, every deploy: plan caps → admin sanity bounds → per-user quota → real node capacity (host CPU/RAM/disk from `psutil`). Docker gets `nano_cpus`/`mem_limit`; disk via overlay2 `storage_opt.size` where supported (the job reports truthfully when the driver cannot enforce it). Incus gets `limits.cpu`, `limits.memory`, root-device `size`. |
| Console | Flask-SocketIO + xterm.js (look follows the supplied `webssh.html`: dark `#0a0a0c`, titlebar/status bar, JetBrains Mono). Attaches to the guest itself (`docker exec` TTY / `incus exec`). Real round-trip latency (SocketIO ack timing) — no fake numbers. Auth + ownership checked on connect, input, resize. |
| SSH access | tmate installed in the guest at provisioning; users start/stop sessions and get the real `tmate_ssh` string back from the guest. Root password is generated per-VPS, set via `chpasswd`, stored **Fernet-encrypted**, revealable only by owner/admin, resettable. |
| Stats honesty | Dashboard "Node" figures are host figures and labelled as such. Per-VPS usage is read from the guest/container (`docker stats`, `/proc`, `df`, `free` inside the guest). |
| Provisioning | Background worker (`skvm-jobs`) with queued/running/done/error jobs, live progress polling, idempotent adopt-on-retry, **automatic cleanup of half-created instances**, 60 s watchdog reconciliation. |
| Admin | Separate server-side-protected area: overview + branding (name, logo, accent, website/Discord, maintenance mode, registration toggle), users (role/plan/quota/suspend/password/delete), all VPS (create-for-user, live resize, delete/force-delete), nodes, plans, OS images, jobs + audit log + app log. |
| Security | Flask-Login sessions (HttpOnly, SameSite=Lax), Werkzeug password hashing, Flask-WTF CSRF on every form, Flask-Limiter on auth/deploy, suspended-user lockout, audit log, secrets only from env/0600 files — **no default passwords anywhere**. Backend exec uses argv lists (Docker SDK / `incus exec` argv); guest shell is only ever fed panel-generated scripts with validated, quoted values. |

## Stack

Python 3 · Flask · Flask-Login · Flask-SQLAlchemy (SQLite `skvm.db`, schema-compatible with Omega's `omega.db` tables) · Docker SDK for Python · Flask-SocketIO + xterm.js · Tailwind CSS · Lucide icons · python-dotenv · Gunicorn (eventlet) · systemd.

> The Omega V4 table layout (`users`, `nodes`, `vps`, `jobs`, `settings`) is preserved; SKVM columns are additive and migrated automatically at startup (`core/db.py:init_db`). The one deliberate deviation from a textbook stack: the data layer keeps Omega's function-level API (`create_vps`, `create_job`, …) implemented on SQLAlchemy, so the worker/node/provisioning code did not need a blind rewrite.

---

## Install (production, systemd host)

```bash
git clone https://github.com/SKYDO234/SKVM.git
cd SKVM
sudo bash install.sh                      # Docker backend (default)
sudo bash install.sh --backend both       # Docker + Incus (preserved Omega backend)
sudo bash install.sh --backend incus      # Incus only
sudo bash setup.sh                        # Omega-compat shim -> install.sh
```

The installer: installs host packages, installs/validates Docker and/or Incus (incl. KVM capability check — VM deploys fail cleanly without `/dev/kvm`), copies the app to `/opt/skvm-panel`, creates the `skvm-panel` service user (added to `docker`/`incus-admin` groups), builds `venv/`, writes `/etc/skvm-panel/skvm.env` (0640), initialises `skvm.db`, creates the first admin (random password → `/var/lib/skvm-panel/first_admin_credentials.txt`, chmod 600, **never printed or logged**), and enables:

- `skvm-panel.service` — Gunicorn eventlet, **`0.0.0.0:5555`** (required for the SocketIO console; keep `--workers 1`)
- `skvm-jobs.service` — provisioning worker
- `skvm-node.service` — node agent on `127.0.0.1:5556` (private; token-authenticated)
- `skvm-watchdog.timer` (60 s reconciliation), `skvm-image-refresh.timer` (6 h)

Then:

```bash
sudo cat /var/lib/skvm-panel/first_admin_credentials.txt   # sign in, change password
sudo shred -u /var/lib/skvm-panel/first_admin_credentials.txt
curl http://127.0.0.1:5555/healthz
journalctl -u skvm-panel -u skvm-jobs -f
```

### Firewall

Direct exposure (simplest): `sudo ufw allow 5555/tcp`. The node agent (5556) and Docker/Incus sockets must **never** be exposed publicly.

### Reverse proxy + HTTPS (recommended)

Restrict the panel to localhost (`SKVM_BIND=127.0.0.1` in `/etc/skvm-panel/skvm.env`, restart `skvm-panel`), set `SKVM_COOKIE_SECURE=true` and `SKVM_TRUST_PROXY=true`, then:

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
    location /socket.io/ {   # web console (WebSocket upgrade required)
        proxy_pass http://127.0.0.1:5555;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
}
```

Get certificates with `certbot --nginx -d panel.example.com` (or Caddy, which handles TLS + WebSockets automatically).

## Local development

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt pytest
cp .env.example .env   # set SECRET_KEY or let the app generate data/.secret_key
SKVM_ADMIN_PASSWORD='choose-a-strong-one' venv/bin/python app.py   # dev server on 0.0.0.0:5555
venv/bin/python -m pytest tests/ -q
bash scripts/verify.sh
```

Without Docker/Incus the panel still runs: deploys fail with the real "no backend available" error (by design — try it). With Docker running (`systemctl start docker`, user in `docker` group) or Incus available, deploys provision for real.

## Configuration (environment)

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` / `SKVM_SECRET_KEY` | generated → 0600 file | Flask secret (also derives the guest-password encryption key) |
| `SKVM_PORT` / `SKVM_BIND` | `5555` / `0.0.0.0` | Panel listen address |
| `SKVM_BACKEND` | `auto` | `auto` \| `docker` \| `incus` |
| `SKVM_DB` | `data/skvm.db` (`/var/lib/skvm-panel/skvm.db` in prod) | SQLite database |
| `SKVM_ADMIN_USERNAME` / `SKVM_ADMIN_PASSWORD` | `admin` / random | First-run admin only |
| `SKVM_COOKIE_SECURE`, `SKVM_TRUST_PROXY` | `false` | Enable behind HTTPS/proxy |
| `SKVM_ALLOW_REGISTRATION`, `SKVM_TIMEZONE`, … | see `.env.example` / `core/config.py` | Legacy `OMEGA_*` names are honoured as fallback for upgrades |

Branding, plans, OS images, maintenance mode and quotas are managed at runtime in **Admin** (database settings), not in code.

## Upgrading from Omega Panel V4

1. Back up `/var/lib/omega-panel/omega.db`.
2. Copy it to the SKVM data dir as `skvm.db` (or point `SKVM_DB` at it). SKVM creates missing tables/columns additively; existing users/VPS/jobs keep working, VPS rows default to the Incus backend.
3. `OMEGA_*` environment variables still work as fallbacks. Re-run `install.sh`; the Omega systemd units can then be disabled (`systemctl disable --now omega-panel omega-jobs omega-node`).

## Project layout

```
app.py            Flask app factory, routes, admin, SocketIO console
job_runner.py     provisioning worker + watchdog (skvm-jobs)
node.py           private node agent API (127.0.0.1:5556, token auth)
core/
  config.py       env config (SKVM_*, OMEGA_* fallback), secret handling
  models.py       SQLAlchemy models (Omega-compatible schema + SKVM tables)
  db.py           data-access facade (preserved API), seeds, migration, audit
  backend.py      unified dispatcher: create/status/action/resize/exec/stats
  docker_engine.py  Docker SDK backend (limits, exec, stats, cleanup)
  engine.py       preserved Omega Incus engine (rebranded limits/keys)
  provision.py    provisioning pipeline + watchdog reconciliation
  tmate.py        in-guest tmate install/start/stop
  capabilities.py host snapshot (labelled host figures) + backend/KVM probes
  os_profiles.py  Ubuntu/Debian image catalogue (Incus + Docker refs)
  crypto.py       Fernet encryption for guest root passwords
  i18n.py         retained Omega translations (compat; UI is English)
templates/        Tailwind + Lucide glass UI; console.html = webssh look
static/           skvm.css / skvm.js
systemd/          skvm-*.service / .timer
tests/            pytest: auth, CSRF, RBAC, plan limits, provisioning, console auth
install.sh        production installer (setup.sh = compat shim)
scripts/          guest provisioning, watchdog, image refresh, verify.sh
```

## Troubleshooting

- **Deploy says "No provisioning backend is available"** — neither daemon responded. `systemctl status docker` / `incus info`; ensure the service user is in the `docker` or `incus-admin` group, then restart `skvm-jobs`.
- **VM deploy fails on KVM** — expected without `/dev/kvm` (common on nested VPS). Use the container engine or a host with nested virtualization.
- **Docker disk quota not enforced** — the job detail says so explicitly. Hard quotas need overlay2 on XFS with `pquota`; otherwise disk is a recorded allocation target and container disk use should be monitored. CPU/RAM limits are always hard.
- **Console won't connect** — VPS must be `running`; behind a proxy, confirm the `/socket.io/` WebSocket upgrade block; check `journalctl -u skvm-panel`.
- **Provisioning error details** — Admin → Jobs & Logs shows the real backend error and attempt count; failed instances are auto-cleaned, so fixing the node and re-deploying is safe.

## License

See `UPSTREAM-NOTICE.txt`. Omega Panel V4 is credited to MrZetrix with original reference credit to NafiGamer (nafigamer0); SKVM PANEL changes are provided on the same terms as received. Review the upstream notice before redistribution.

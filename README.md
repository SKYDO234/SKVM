# SKVM

Sovereign VPS control plane — a glassmorphic, self-hosted panel for creating,
observing and operating virtual servers (Docker containers or KVM guests on
remote hypervisor nodes), with a web console, file manager, metrics, alerting,
quotas, audit trail and a scoped REST API.

## Quick start in a sandbox

`autosetup.sh` turns an empty Linux sandbox into a running panel: it installs
missing packages, builds a virtualenv, writes `.env`, creates the schema, seeds
the owner account (plus a simulated fleet when there is no hypervisor), starts
the server and prints the public URL. Every step is idempotent.

```bash
bash autosetup.sh --expose        # one shot; prints panel URL + credentials
```

### rigbox.dev workspace

```bash
rig login
rig deploy --name skvm            # uses rig.yaml in this repo
# …or inside a running workspace:
git clone https://github.com/SKYDO234/SKVM.git && cd SKVM
bash autosetup.sh --expose        # creates the https://<app>.rigbox.dev route
```

`rig.yaml` schedules 2 vCPU / 2 GB, keeps `instance/` state (database, keys,
backups) on a 5 GB volume, health-checks `/healthz` and exposes
`backend` / `allow_registration` as deploy parameters. The route starts private;
flip `visibility: public` (or `rig app …`) to share the preview link.

### CodeSandbox

Open this repository as a VM sandbox (Devbox) — `.devcontainer/devcontainer.json`
selects the Python 3.11 image and `.codesandbox/tasks.json` runs the setup task,
then starts **SKVM panel** with a preview on port 8080. Manual equivalent:

```bash
bash autosetup.sh                      # setup + background server
bash autosetup.sh --fg --skip-apt --skip-install   # foreground (task runner)
```

The preview host is derived from `CODESANDBOX_HOST` / `CSB_BASE_PREVIEW_HOST`, so
cookies are marked secure and HTTPS is enforced automatically.

### GitHub Codespaces / any devcontainer

`onCreateCommand` runs `bash ./autosetup.sh --no-run`; attach and the panel
starts, forwarding port 8080. On Codespaces the URL is built from
`CODESPACE_NAME` + `GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN`.

### Useful flags

| Flag | Effect |
| --- | --- |
| `--port N` | listen port (default `$PORT`, else 8080) |
| `--backend auto\|docker\|demo\|node` | hypervisor driver; `auto` falls back to the simulated fleet |
| `--instance-dir PATH` | persistent state (sqlite, Fernet keys, backups, logs) |
| `--user/--password/--email` | owner account (password defaults to a generated one in `instance/admin-credentials.txt`, 0600) |
| `--no-demo`, `--no-seed` | skip the simulated fleet / leave credentials alone |
| `--fg` | stay in the foreground (used by task runners and `rig.yaml start:`) |
| `--restart --status --stop --logs` | lifecycle helpers |
| `--reinstall` | rebuild the virtualenv and rewrite `.env` |

Environment overrides such as `SKVM_BACKEND=demo`, `SKVM_PORT=9000` or
`SKVM_DATABASE_URL=postgres://…` are honoured too — see `.env.example`.

## Running it yourself

```bash
python3 -m venv instance/venv && instance/venv/bin/python -m pip install -r requirements.txt
instance/venv/bin/python manage.py init-db
instance/venv/bin/python manage.py seed --username admin --password 'choose-one'
instance/venv/bin/python manage.py serve --host 0.0.0.0 --port 8080
instance/venv/bin/python manage.py doctor    # backend, schema and dependency health
```

Without a Docker daemon the panel serves the **demo** driver: a believable fleet
with live metrics, a fake filesystem, snapshots and backups, so the whole UI is
exercised end to end. Point `SKVM_BACKEND=docker` at a host with a daemon, or
register remote hypervisors under Admin → Hypervisors and install the node
agent (`static/scripts/install-node.sh`).

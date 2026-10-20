# SKVM

[![CI](https://github.com/SKYDO234/SKVM/actions/workflows/ci.yml/badge.svg)](https://github.com/SKYDO234/SKVM/actions/workflows/ci.yml) [![image](https://img.shields.io/badge/ghcr.io-skvm-0db99d?logo=docker&logoColor=white)](https://github.com/SKYDO234/SKVM/pkgs/container/skvm) [![Open in Codespaces](https://img.shields.io/static/v1?label=Open+in+&message=Codespaces&color=0d66d6&logo=githubcodespaces)](https://codespaces.new/SKYDO234/SKVM)

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

### GitHub Codespaces — for testing

[![Open in Codespaces](https://img.shields.io/static/v1?label=Open+in+&message=Codespaces&color=0d66d6&logo=githubcodespaces)](https://codespaces.new/SKYDO234/SKVM)

Code → **Codespaces → Create codespace on main**. The dev container selects
Python 3.11, `onCreateCommand` runs `bash ./autosetup.sh --no-run`, and the
first start boots the panel in the background; the editor opens the
**SKVM panel** preview on port 8080. The public URL, secure cookies and HTTPS
enforcement are derived from `CODESPACE_NAME` +
`GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN`, so nothing else to configure.

This is a test box, and the panel treats it like one:

* the forwarded port stays **private** (only you can reach it) until you opt in:
  `bash autosetup.sh --expose` → `gh codespace ports visibility 8080:public`,
  which also turns account registration off for that run;
* state lives in `instance/` on the codespace volume — delete the codespace and
  the database goes with it (`gh cs ssh`, then copy `instance/skvm.db` out, or
  point `SKVM_INSTANCE_DIR` at a mounted disk when you need persistence);
* the owner password is generated on first boot (`cat
  instance/admin-credentials.txt`), or set `SKVM_ADMIN_PASSWORD` as a Codespaces
  secret to choose it yourself;
* `docker-outside-of-docker` is enabled, so a Codespace can provision **real**
  containers (`--backend docker`) instead of the simulated fleet.

Handy commands inside a Codespace:

```bash
bash autosetup.sh --status      # is it up?
bash autosetup.sh --logs        # follow the server log
bash autosetup.sh --restart     # bounce it (also re-applies .env)
CI=1 bash .scripts/smoke.sh     # 43 route checks: pages, login, API, assets
python .scripts/check_node_fallback.py   # an unusable node must degrade, not 500
python .scripts/check_workflows.py       # every config parses, every CI step is valid shell
```

CI runs exactly that bootstrap on every push and pull request — bare Ubuntu, a
simulated Codespace, a simulated CodeSandbox and a simulated container image —
plus `bash -n`/shellcheck/ruff/`node --check` and a boot of both Docker images.
It never deploys anything; publishing images is a separate, explicit workflow.

### Container image (GitHub Packages)

```bash
docker run -p 8080:8080 -v skvm-data:/data ghcr.io/SKYDO234/skvm:latest
docker compose up skvm                       # same thing, from this repo
docker compose --profile docker up           # + docker CLI, host daemon mounted
```

`/entrypoint.sh` calls the same `autosetup.sh` (in container mode: no apt, no
clone, baked-in `/opt/venv`), so the image and a raw sandbox cannot drift apart.
`:latest` runs unprivileged against the simulated fleet or remote node agents;
`:docker` mounts `/var/run/docker.sock` and is therefore root-equivalent — a
lab host, not a shared box. The `.github/workflows/publish.yml` workflow builds
both for `linux/amd64` + `arm64` on every `v*` tag and on `main`.

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

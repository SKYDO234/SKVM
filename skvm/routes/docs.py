"""In-panel developer documentation: REST API reference, CLI, node agent."""

from __future__ import annotations

from flask import Blueprint, current_app, render_template

from ..backends import available_backends, backend_status
from ..models import TokenScope
from ..security import SCOPE_DESCRIPTIONS, login_required

bp = Blueprint("docs", __name__, url_prefix="/docs")

API_GROUPS = [
    {
        "title": "Authentication",
        "blurb": "Create a token under Account → API tokens, then send it as a bearer header. "
                 "Session cookies also work for same-origin calls.",
        "endpoints": [
            {"method": "GET", "path": "/api/v1/meta", "scope": "—", "desc": "Panel metadata + endpoint index"},
            {"method": "GET", "path": "/api/v1/account", "scope": "read", "desc": "Your account, quotas and totals"},
            {"method": "GET", "path": "/api/v1/summary", "scope": "read", "desc": "Fleet totals and open alerts"},
        ],
    },
    {
        "title": "Servers",
        "blurb": "Full lifecycle management. Every response is JSON; errors use {\"ok\": false, \"error\": ...}.",
        "endpoints": [
            {"method": "GET", "path": "/api/v1/servers", "scope": "read",
             "desc": "List servers · filters: status, q, tag, page, per_page"},
            {"method": "POST", "path": "/api/v1/servers", "scope": "write",
             "desc": "Create a server (name, os_image, cpu, ram_mb, disk_gb, ssh_keys[])"},
            {"method": "GET", "path": "/api/v1/servers/&lt;uid&gt;", "scope": "read", "desc": "Server detail + live stats"},
            {"method": "PATCH", "path": "/api/v1/servers/&lt;uid&gt;", "scope": "write", "desc": "Rename, tag, notes, auto-renew"},
            {"method": "DELETE", "path": "/api/v1/servers/&lt;uid&gt;", "scope": "write", "desc": "Destroy a server"},
            {"method": "POST", "path": "/api/v1/servers/&lt;uid&gt;/power", "scope": "power",
             "desc": "Body: {\"action\": \"start|stop|restart|suspend|resume\"}"},
            {"method": "GET", "path": "/api/v1/servers/&lt;uid&gt;/metrics", "scope": "read",
             "desc": "window=live|1h|6h|24h|7d|30d"},
            {"method": "POST", "path": "/api/v1/servers/&lt;uid&gt;/exec", "scope": "write",
             "desc": "Run a shell command inside the server, returns exit_code + output"},
            {"method": "GET", "path": "/api/v1/servers/&lt;uid&gt;/events", "scope": "read", "desc": "Activity timeline"},
        ],
    },
    {
        "title": "Data protection",
        "blurb": "Snapshots are image-level and instant; backups stream the filesystem to panel storage.",
        "endpoints": [
            {"method": "GET", "path": "/api/v1/servers/&lt;uid&gt;/snapshots", "scope": "read", "desc": "List snapshots"},
            {"method": "POST", "path": "/api/v1/servers/&lt;uid&gt;/snapshots", "scope": "write", "desc": "Create a snapshot"},
            {"method": "POST", "path": "/api/v1/servers/&lt;uid&gt;/snapshots/&lt;id&gt;/restore", "scope": "write", "desc": "Roll back"},
            {"method": "DELETE", "path": "/api/v1/servers/&lt;uid&gt;/snapshots/&lt;id&gt;", "scope": "write", "desc": "Delete"},
            {"method": "GET", "path": "/api/v1/servers/&lt;uid&gt;/backups", "scope": "read", "desc": "List backups"},
            {"method": "POST", "path": "/api/v1/servers/&lt;uid&gt;/backups", "scope": "write", "desc": "Start a backup"},
        ],
    },
    {
        "title": "Platform",
        "blurb": "Catalog, infrastructure and monitoring endpoints.",
        "endpoints": [
            {"method": "GET", "path": "/api/v1/images", "scope": "read", "desc": "OS catalog + availability per driver"},
            {"method": "GET", "path": "/api/v1/plans", "scope": "read", "desc": "Plan presets, regions and currency"},
            {"method": "GET", "path": "/api/v1/nodes", "scope": "read", "desc": "Hypervisor nodes and scheduler status"},
            {"method": "GET", "path": "/api/v1/alerts/rules", "scope": "read", "desc": "Alert rules"},
            {"method": "GET", "path": "/api/v1/alerts/events", "scope": "read", "desc": "Alert events"},
            {"method": "GET", "path": "/api/v1/audit", "scope": "admin", "desc": "Audit trail (staff only)"},
        ],
    },
]

CLI_DOCS = [
    ("python manage.py run", "start the panel (development server with websockets)"),
    ("python manage.py serve --host 0.0.0.0 --port 8000", "production gthread server"),
    ("python manage.py worker", "background sampler + alert evaluator"),
    ("python manage.py init-db", "create the database schema"),
    ("python manage.py seed --username admin", "create an owner account"),
    ("python manage.py demo-data --servers 6", "seed simulated infrastructure"),
    ("python manage.py doctor", "environment + hypervisor diagnostics"),
]

CURL_EXAMPLES = [
    {
        "title": "Provision a server",
        "command": """curl -sS https://panel.example.com/api/v1/servers \\
  -H "Authorization: Bearer $SKVM_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"name":"edge-fra-1","os_image":"ubuntu-24.04","cpu":4,"ram_mb":8192,"disk_gb":80}'""",
    },
    {
        "title": "Restart it and stream metrics",
        "command": """curl -sS -X POST https://panel.example.com/api/v1/servers/$UID/power \\
  -H "Authorization: Bearer $SKVM_TOKEN" -d '{"action":"restart"}'

curl -sS "https://panel.example.com/api/v1/servers/$UID/metrics?window=24h" \\
  -H "Authorization: Bearer $SKVM_TOKEN" """,
    },
    {
        "title": "Snapshot before a risky deploy",
        "command": """curl -sS -X POST https://panel.example.com/api/v1/servers/$UID/snapshots \\
  -H "Authorization: Bearer $SKVM_TOKEN" \\
  -d '{"name":"pre-deploy-1.4.2","description":"before the 1.4.2 rollout"}'""",
    },
]

NODE_DOCS = [
    {
        "title": "1 · Install the agent",
        "command": "curl -fsSL https://panel.example.com/static/scripts/install-node.sh | sudo bash",
    },
    {
        "title": "2 · Register it in the panel",
        "command": "Admin → Nodes → Register node  (prints a key id + secret)",
    },
    {
        "title": "3 · Start the agent with that secret",
        "command": "sudo SKVM_NODE_KEY=sk_xxx SKVM_NODE_SECRET=... python3 skvm_node/agent.py",
    },
]


@bp.route("/")
@login_required
def index():
    return render_template(
        "docs.html",
        groups=API_GROUPS,
        cli=CLI_DOCS,
        examples=CURL_EXAMPLES,
        node_docs=NODE_DOCS,
        scopes=[scope.value for scope in TokenScope],
        scope_descriptions={str(key): value for key, value in SCOPE_DESCRIPTIONS.items()},
        panel_url=current_app.config.get("PANEL_URL"),
        backends=available_backends(),
        backend=backend_status(),
    )

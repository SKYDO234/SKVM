"""Alerting UI + API: rules, events, acknowledgements, webhooks."""

from __future__ import annotations

from flask import Blueprint, render_template, request
from sqlalchemy import or_

from ..db import db, iso, utcnow
from ..models import AlertEvent, AlertRule, AlertState, Role, Server, Severity
from ..security import current_user, login_required
from ..services import alerts as alert_service
from ..services import audit
from ..services.settings import settings
from .helpers import arg, body, fail, get_server, ok, paging, paginated

bp = Blueprint("alerts", __name__, url_prefix="/alerts")


def _rules_for(user):
    query = db.session.query(AlertRule)
    if not user.can(Role.OPERATOR):
        query = query.filter(AlertRule.user_id == user.id)
    return query


def _events_for(user):
    query = db.session.query(AlertEvent).join(AlertRule, AlertRule.id == AlertEvent.rule_id)
    if not user.can(Role.OPERATOR):
        query = query.filter(AlertRule.user_id == user.id)
    return query


@bp.route("/")
@login_required
def index():
    user = current_user()
    rules = _rules_for(user).order_by(AlertRule.state.desc(), AlertRule.created_at.desc()).all()
    events = _events_for(user).order_by(AlertEvent.ts.desc()).limit(60).all()
    servers = (
        db.session.query(Server)
        .filter(Server.owner_id == user.id)
        .order_by(Server.name)
        .all()
        if not user.can(Role.OPERATOR)
        else db.session.query(Server).order_by(Server.name).all()
    )
    return render_template(
        "alerts.html",
        rules=rules,
        events=events,
        counters=alert_service.counters(user),
        metrics=alert_service.METRICS,
        servers=servers,
        severities=[severity.value for severity in Severity],
        states=[state.value for state in AlertState],
        settings_values=settings.all(),
    )


@bp.route("/api/v1/rules", methods=["GET", "POST"])
@login_required
def rules():
    user = current_user()
    if request.method == "GET":
        rows = _rules_for(user).order_by(AlertRule.created_at.desc()).all()
        return ok(rules=[row.public() for row in rows])

    data = body()
    metric = str(data.get("metric") or "cpu")
    if metric not in alert_service.METRICS:
        return fail("Unsupported metric", 400)
    server = None
    if data.get("server_uid"):
        server = get_server(str(data["server_uid"]), staff_ok=True) if data["server_uid"] else None
        if server is not None and server.owner_id != user.id and not user.can(Role.OPERATOR):
            return fail("You cannot alert on that server", 403)
    name = str(data.get("name") or f"{alert_service.METRICS[metric]['label']} watch")[:64]
    try:
        threshold = float(data.get("threshold", alert_service.METRICS[metric]["default"]))
    except (TypeError, ValueError):
        return fail("Threshold must be a number", 400)
    rule = AlertRule(
        user_id=user.id,
        server_id=server.id if server else None,
        name=name,
        metric=metric,
        comparator="<" if str(data.get("comparator")) == "<" else ">",
        threshold=threshold,
        duration_s=max(0, int(data.get("duration_s") or 300)),
        severity=str(data.get("severity") or Severity.WARNING.value),
        enabled=bool(data.get("enabled", True)),
        notify_email=bool(data.get("notify_email", True)),
        notify_webhook=str(data.get("notify_webhook") or "")[:255],
    )
    db.session.add(rule)
    audit.record("alert.rule_create", target_type="alert", target_label=rule.name,
                 meta={"metric": metric, "threshold": threshold})
    db.session.commit()
    return ok(rule=rule.public(), message="Alert rule created")


@bp.route("/api/v1/rules/<int:rule_id>", methods=["PATCH", "DELETE"])
@login_required
def rule_detail(rule_id: int):
    user = current_user()
    rule = db.session.get(AlertRule, rule_id)
    if rule is None or (rule.user_id != user.id and not user.can(Role.OPERATOR)):
        return fail("Rule not found", 404)
    if request.method == "DELETE":
        audit.record("alert.rule_delete", target_type="alert", target_id=rule.id, target_label=rule.name)
        db.session.delete(rule)
        db.session.commit()
        return ok(message="Rule deleted")

    data = body()
    if "threshold" in data:
        try:
            rule.threshold = float(data["threshold"])
        except (TypeError, ValueError):
            return fail("Threshold must be numeric", 400)
    if "name" in data:
        rule.name = str(data["name"])[:64]
    if "duration_s" in data:
        rule.duration_s = max(0, int(data["duration_s"]))
    if "severity" in data and str(data["severity"]) in [s.value for s in Severity]:
        rule.severity = str(data["severity"])
    if "enabled" in data:
        rule.enabled = bool(data["enabled"])
    if "notify_email" in data:
        rule.notify_email = bool(data["notify_email"])
    if "notify_webhook" in data:
        rule.notify_webhook = str(data["notify_webhook"])[:255]
    if rule.enabled is False:
        rule.state = AlertState.OK.value
        rule.breach_since = None
    audit.record("alert.rule_update", target_type="alert", target_id=rule.id, target_label=rule.name,
                 meta={"fields": sorted(data.keys())})
    db.session.commit()
    return ok(rule=rule.public(), message="Rule updated")


@bp.route("/api/v1/events")
@login_required
def events():
    user = current_user()
    page, size = paging(25, 100)
    query = _events_for(user).order_by(AlertEvent.ts.desc())
    if arg("state"):
        query = query.filter(AlertEvent.state == arg("state"))
    if arg("severity"):
        query = query.filter(AlertEvent.severity == arg("severity"))
    if arg("open", False, bool):
        query = query.filter(AlertEvent.acknowledged_at.is_(None))
    data = paginated(query, page, size, serializer=lambda row: row.public())
    return ok(**data)


@bp.route("/api/v1/events/<int:event_id>/ack", methods=["POST"])
@login_required
def ack(event_id: int):
    user = current_user()
    event = db.session.get(AlertEvent, event_id)
    if event is None:
        return fail("Event not found", 404)
    if event.rule and event.rule.user_id != user.id and not user.can(Role.OPERATOR):
        return fail("Not your alert", 403)
    alert_service.acknowledge(event, user)
    db.session.commit()
    return ok(event=event.public(), message="Alert acknowledged")


@bp.route("/api/v1/test", methods=["POST"])
@login_required
def test_webhook():
    """Send a sample payload so operators can verify their webhook."""
    import json

    import requests

    data = body()
    url = str(data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return fail("Provide an http(s) webhook URL", 400)
    payload = {
        "panel": "SKVM",
        "type": "test",
        "severity": "info",
        "message": "Test notification from SKVM — your webhook works.",
        "at": iso(utcnow()),
    }
    try:
        response = requests.post(
            url, data=json.dumps(payload),
            headers={"Content-Type": "application/json", "User-Agent": "SKVM-Alerts/2.0"},
            timeout=8,
        )
    except requests.RequestException as exc:
        return fail(f"Webhook failed: {exc}", 400)
    return ok(status=response.status_code, message=f"Webhook responded with HTTP {response.status_code}")


@bp.route("/api/v1/seed", methods=["POST"])
@login_required
def seed():
    """Create the recommended starter rules for the current user."""
    user = current_user()
    created = alert_service.seed_default_rules(user)
    db.session.commit()
    return ok(rules=[rule.public() for rule in created], message=f"{len(created)} rules created")

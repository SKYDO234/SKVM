"""Threshold alerting.

Rules are evaluated by the background worker against live samples.  A rule fires
only after its metric stays in breach for ``duration_s`` seconds, which keeps
short spikes from paging anyone.  Notifications go to e-mail and/or a webhook
(Slack, Discord, Mattermost, ntfy all accept the JSON payload we send).
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import requests

from ..db import db, iso, utcnow
from ..models import AlertEvent, AlertRule, AlertState, Role, Server, Severity, User
from . import audit
from .mailer import send_alert
from .metrics import collect
from .settings import settings

log = logging.getLogger("skvm.alerts")

METRICS = {
    "cpu": {"label": "CPU utilisation", "unit": "%", "default": 90, "max": 100},
    "ram": {"label": "Memory utilisation", "unit": "%", "default": 90, "max": 100},
    "disk": {"label": "Disk utilisation", "unit": "%", "default": 85, "max": 100},
    "net_rx": {"label": "Inbound traffic", "unit": "KB/s", "default": 50000, "max": 10_000_000},
    "net_tx": {"label": "Outbound traffic", "unit": "KB/s", "default": 50000, "max": 10_000_000},
    "load": {"label": "Load average", "unit": "", "default": 8, "max": 512},
    "offline": {"label": "Server offline", "unit": "", "default": 1, "max": 1},
    "bandwidth": {"label": "Bandwidth used", "unit": "%", "default": 90, "max": 100},
}


def _value_for(rule: AlertRule, server: Server, stats: dict[str, Any]) -> float:
    metric = rule.metric
    if metric == "cpu":
        return float(stats.get("cpu_pct") or 0)
    if metric == "ram":
        return float(stats.get("ram_pct") or 0)
    if metric == "disk":
        return float(stats.get("disk_pct") or 0)
    if metric == "net_rx":
        return float(stats.get("net_rx_kbps") or 0)
    if metric == "net_tx":
        return float(stats.get("net_tx_kbps") or 0)
    if metric == "load":
        return float(stats.get("load1") or 0)
    if metric == "offline":
        return 0.0 if str(stats.get("status")) == "running" else 1.0
    if metric == "bandwidth":
        limit = float(server.bandwidth_gb or 0)
        used = float(server.bandwidth_used_mb or 0) / 1024
        return round(used / limit * 100, 2) if limit else 0.0
    return 0.0


def _breached(rule: AlertRule, value: float) -> bool:
    if rule.comparator == "<":
        return value < float(rule.threshold)
    return value > float(rule.threshold)


def evaluate_rule(rule: AlertRule, servers: list[Server]) -> list[dict[str, Any]]:
    """Evaluate one rule across its target servers. Returns fired/resolved events."""
    outcome: list[dict[str, Any]] = []
    targets = [s for s in servers if rule.server_id in (None, s.id)]
    if not targets:
        return outcome

    worst_value = 0.0
    any_breach = False
    first_breach_server: Server | None = None
    for server in targets:
        if server.status == Server.SUSPENDED.value and rule.metric != "offline":
            continue
        stats = collect(server, cache_seconds=10)
        value = _value_for(rule, server, stats)
        if _breached(rule, value):
            any_breach = True
            if value > worst_value:
                worst_value = value
                first_breach_server = server

    now = utcnow()
    rule.last_value = round(worst_value, 2)

    if any_breach:
        if rule.breach_since is None:
            rule.breach_since = now
        breach_seconds = (now - (
            rule.breach_since if rule.breach_since.tzinfo else rule.breach_since.replace(tzinfo=now.tzinfo)
        )).total_seconds()
        if breach_seconds >= int(rule.duration_s or 0) and rule.state != AlertState.FIRING.value:
            rule.state = AlertState.FIRING.value
            rule.last_fired_at = now
            rule.fire_count = int(rule.fire_count or 0) + 1
            server = first_breach_server
            message = (
                f"{METRICS.get(rule.metric, {}).get('label', rule.metric)} "
                f"{rule.comparator} {rule.threshold:g} for {int(breach_seconds)}s "
                f"(now {worst_value:.2f})"
            )
            event = AlertEvent(
                rule_id=rule.id,
                server_id=server.id if server else rule.server_id,
                state=AlertState.FIRING.value,
                value=round(worst_value, 2),
                severity=rule.severity,
                message=message[:255],
            )
            db.session.add(event)
            db.session.flush()
            _notify(rule, event, server)
            outcome.append({"kind": "firing", "rule": rule.name, "event_id": event.id, "message": message})
    else:
        if rule.state == AlertState.FIRING.value:
            event = AlertEvent(
                rule_id=rule.id,
                server_id=rule.server_id,
                state=AlertState.RESOLVED.value,
                value=rule.last_value,
                severity=Severity.INFO.value,
                message=f"Recovered — {METRICS.get(rule.metric, {}).get('label', rule.metric)} back to normal",
                resolved_at=now,
            )
            db.session.add(event)
            db.session.flush()
            outcome.append({"kind": "resolved", "rule": rule.name, "event_id": event.id})
        rule.state = AlertState.OK.value
        rule.breach_since = None
    return outcome


def _notify(rule: AlertRule, event: AlertEvent, server: Server | None) -> None:
    payload = {
        "rule": rule.name,
        "metric": rule.metric,
        "comparator": rule.comparator,
        "threshold": rule.threshold,
        "value": event.value,
        "severity": rule.severity,
        "state": event.state,
        "server": server.name if server else "platform",
        "server_uid": server.uid if server else None,
        "message": event.message,
        "panel": "SKVM",
        "at": iso(event.ts),
    }
    if rule.notify_webhook:
        try:
            requests.post(
                rule.notify_webhook,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json", "User-Agent": "SKVM-Alerts/2.0"},
                timeout=8,
            )
        except requests.RequestException as exc:
            log.warning("alert webhook failed: %s", exc)
    owner: User | None = rule.user
    if rule.notify_email and owner and settings.boolean("alert_email_enabled"):
        try:
            send_alert(owner, {**payload, "server": server.name if server else "platform"})
        except Exception as exc:  # pragma: no cover
            log.warning("alert mail failed: %s", exc)


def evaluate_all() -> list[dict[str, Any]]:
    """Run every enabled rule — called by the background worker."""
    rules = db.session.query(AlertRule).filter(AlertRule.enabled.is_(True)).all()
    if not rules:
        return []
    servers = db.session.query(Server).all()
    by_owner: dict[int, list[Server]] = {}
    for server in servers:
        by_owner.setdefault(server.owner_id, []).append(server)
    fired: list[dict[str, Any]] = []
    for rule in rules:
        try:
            outcomes = evaluate_rule(rule, by_owner.get(rule.user_id, []))
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("alert rule %s failed: %s", rule.id, exc)
            continue
        for outcome in outcomes:
            if outcome["kind"] == "firing":
                audit.record(
                    "alert.fire", target_type="alert", target_id=rule.id, target_label=rule.name,
                    result="warn", meta={"message": outcome["message"], "severity": rule.severity},
                    actor="system", channel="system",
                )
            fired.append(outcome)
    return fired


def acknowledge(event: AlertEvent, user: User) -> AlertEvent:
    event.acknowledged_at = utcnow()
    event.acknowledged_by = user.username
    audit.record("alert.ack", target_type="alert", target_id=event.id, target_label=event.rule.name if event.rule else "")
    db.session.flush()
    return event


def open_events(user: User | None = None, limit: int = 20) -> list[AlertEvent]:
    query = (
        db.session.query(AlertEvent)
        .filter(AlertEvent.acknowledged_at.is_(None))
        .order_by(AlertEvent.ts.desc())
    )
    if user is not None and not user.can(Role.OPERATOR):
        query = query.join(AlertRule, AlertRule.id == AlertEvent.rule_id).filter(AlertRule.user_id == user.id)
    return query.limit(limit).all()


def counters(user: User | None = None) -> dict[str, int]:
    from sqlalchemy import func

    since = utcnow() - timedelta(days=1)
    base = db.session.query(func.count(AlertEvent.id)).filter(AlertEvent.ts >= since)
    if user is not None and not user.can(Role.OPERATOR):
        base = base.join(AlertRule, AlertRule.id == AlertEvent.rule_id).filter(AlertRule.user_id == user.id)
    return {
        "events_24h": int(base.scalar() or 0),
        "critical_24h": int(base.filter(AlertEvent.severity == Severity.CRITICAL.value).scalar() or 0),
        "open": len(open_events(user, limit=200)),
    }


def seed_default_rules(user: User, server: Server | None = None) -> list[AlertRule]:
    """Create a sensible starter ruleset for a new account/server."""
    created: list[AlertRule] = []
    presets = [
        ("High CPU", "cpu", ">", 90.0, 300, Severity.WARNING),
        ("Memory pressure", "ram", ">", 90.0, 300, Severity.WARNING),
        ("Disk almost full", "disk", ">", 85.0, 600, Severity.CRITICAL),
        ("Server unreachable", "offline", ">", 0.5, 120, Severity.CRITICAL),
    ]
    for name, metric, comparator, threshold, duration, severity in presets:
        rule = AlertRule(
            user_id=user.id,
            server_id=server.id if server else None,
            name=f"{name}{' · ' + server.name if server else ''}",
            metric=metric,
            comparator=comparator,
            threshold=threshold,
            duration_s=duration,
            severity=severity,
            notify_email=True,
        )
        db.session.add(rule)
        created.append(rule)
    db.session.flush()
    return created

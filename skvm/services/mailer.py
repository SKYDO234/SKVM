"""Outbound mail (welcome, password reset, alerts, quota warnings).

The panel works fine with ``MAIL_MODE=log`` (default) — messages are written to
the application log so a fresh install never blocks on SMTP configuration.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any, Iterable

from flask import current_app, render_template_string

log = logging.getLogger("skvm.mail")

BASE_TEMPLATE = """\
SKVM · {{ panel }}
------------------------------------------------------------
{{ title }}
------------------------------------------------------------
{{ body }}

--
{{ panel }} — {{ tagline }}
{{ panel_url }}
"""


def _render(title: str, body: str) -> tuple[str, str]:
    panel = current_app.config.get("PANEL_NAME", "SKVM")
    tagline = current_app.config.get("PANEL_TAGLINE", "")
    url = current_app.config.get("PANEL_URL", "")
    text = render_template_string(
        BASE_TEMPLATE, panel=panel, title=title, body=body, tagline=tagline, panel_url=url
    )
    html = f"""\
<html><body style="font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#0b0f1a;color:#e6e9f5;padding:24px">
  <div style="max-width:560px;margin:auto;border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:28px;
              background:linear-gradient(160deg,rgba(124,92,255,.16),rgba(14,18,32,.9))">
    <div style="font-size:12px;letter-spacing:.28em;text-transform:uppercase;color:#8b93b5">{panel}</div>
    <h2 style="margin:10px 0 18px;font-weight:600">{title}</h2>
    <div style="line-height:1.65;color:#c9cfe6;white-space:pre-wrap">{body}</div>
    <hr style="border:none;border-top:1px solid rgba(255,255,255,.08);margin:22px 0">
    <div style="font-size:12px;color:#8b93b5">{tagline}<br><a style="color:#9d8bff" href="{url}">{url}</a></div>
  </div>
</body></html>"""
    return text, html


def send(to: str | Iterable[str], subject: str, body: str, html: str | None = None) -> bool:
    """Send (or log) one message. Returns ``True`` when dispatched."""
    recipients = [to] if isinstance(to, str) else [r for r in to if r]
    if not recipients:
        return False
    mode = (current_app.config.get("MAIL_MODE") or "log").lower()
    text, auto_html = _render(subject, body)
    html_body = html or auto_html

    if mode != "smtp" or not current_app.config.get("SMTP_HOST"):
        log.info("[mail:%s] to=%s subject=%s\n%s", mode, ",".join(recipients), subject, text)
        return True

    message = EmailMessage()
    message["From"] = current_app.config.get("SMTP_FROM", "SKVM <no-reply@localhost>")
    message["To"] = ", ".join(recipients)
    message["Subject"] = f"[{current_app.config.get('PANEL_NAME', 'SKVM')}] {subject}"
    message.set_content(text)
    message.add_alternative(html_body, subtype="html")

    host = current_app.config["SMTP_HOST"]
    port = int(current_app.config.get("SMTP_PORT", 587))
    user = current_app.config.get("SMTP_USER")
    password = current_app.config.get("SMTP_PASSWORD")
    try:
        if current_app.config.get("SMTP_TLS", True):
            server: Any = smtplib.SMTP(host, port, timeout=15)
            server.starttls()
        else:
            server = smtplib.SMTP(host, port, timeout=15)
        if user:
            server.login(user, password or "")
        server.send_message(message)
        server.quit()
        log.info("mail sent to %s (%s)", recipients, subject)
        return True
    except Exception as exc:  # pragma: no cover - network dependent
        log.error("mail delivery failed: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# Message builders
# --------------------------------------------------------------------------- #
def send_welcome(user, temporary_password: str | None = None) -> bool:
    panel = current_app.config.get("PANEL_NAME", "SKVM")
    lines = [
        f"Hi {user.full_name or user.username},",
        "",
        f"Your {panel} account is ready.",
        f"  Username: {user.username}",
    ]
    if temporary_password:
        lines.append(f"  Temporary password: {temporary_password}")
        lines.append("You will be asked to change it on first sign-in.")
    lines += ["", f"Sign in: {current_app.config.get('PANEL_URL', '')}/login"]
    return send(user.email, f"Welcome to {panel}", "\n".join(lines))


def send_password_reset(user, raw_token: str) -> bool:
    link = f"{current_app.config.get('PANEL_URL', '')}/reset/{raw_token}"
    return send(
        user.email,
        "Password reset requested",
        f"Hi {user.username},\n\nUse the link below within 60 minutes to choose a new password:\n{link}\n\n"
        "If you did not request this, you can safely ignore the message.",
    )


def send_alert(user, alert: dict[str, Any]) -> bool:
    body = (
        f"Alert: {alert.get('rule')} ({alert.get('severity', 'warning')})\n"
        f"Target: {alert.get('server', 'platform')}\n"
        f"Observed: {alert.get('value')} · threshold {alert.get('threshold')}\n"
        f"State: {alert.get('state')}\n\n{alert.get('message', '')}"
    )
    return send(user.email, f"[{alert.get('severity', 'warning').upper()}] {alert.get('rule')}", body)


def send_quota_warning(user, text: str) -> bool:
    return send(user.email, "Quota usage warning", text)


def send_server_ready(server) -> bool:
    owner = server.owner
    if owner is None:
        return False
    body = (
        f"Hi {owner.full_name or owner.username},\n\n"
        f"{server.name} is online.\n"
        f"  Host: {server.primary_ip()}\n  SSH:  {server.ssh_command()}\n"
        f"  Specs: {server.cpu_cores:g} vCPU · {server.ram_mb} MB RAM · {server.disk_gb} GB disk\n\n"
        "Open the console in the panel for details."
    )
    return send(owner.email, f"{server.name} is ready", body)

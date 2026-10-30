"""Provisioning pipeline for SKVM PANEL.

Runs in the dedicated worker (job_runner.py), never in a web request.
Every step reports real progress from the backend; a failure marks the
VPS `error`, records the real error, and removes a half-created instance
so retries start clean. Guest preparation is Debian/Ubuntu only, matching
the supported OS profiles.
"""
from __future__ import annotations
import secrets
import shlex

from . import backend, db
from .config import AUTO_UPDATE
from .crypto import encrypt_text
from .engine import EngineError
from .tmate import KEY_SETUP_SCRIPT


def _guest_prep(vps, root_password: str):
    """Install SSH + tmate, set hostname/root password in the guest."""
    hn = shlex.quote(vps.hostname)
    pw = shlex.quote(f"root:{root_password}")
    auto = ""
    if AUTO_UPDATE:
        auto = ("apt-get install -y unattended-upgrades apt-listchanges >/dev/null 2>&1 || true; "
                "printf '%s\\n' 'Unattended-Upgrade::Automatic-Reboot \"false\";' "
                "'APT::Periodic::Update-Package-Lists \"1\";' 'APT::Periodic::Unattended-Upgrade \"1\";' "
                "> /etc/apt/apt.conf.d/52skvm-unattended || true;")
    script = (
        "export DEBIAN_FRONTEND=noninteractive; set -e; "
        f"hostname {hn} 2>/dev/null || true; echo {hn} > /etc/hostname; "
        "apt-get update -y; "
        "apt-get install -y sudo curl ca-certificates openssh-server openssh-client tmate iproute2 procps; "
        "mkdir -p /var/run/sshd /root/.ssh; chmod 700 /root/.ssh; "
        # SSH keys: host keys for sshd + a root identity for tmate (tmate
        # refuses to connect without one). Never overwrites existing keys.
        + KEY_SETUP_SCRIPT + "; "
        f"echo {pw} | chpasswd; "
        "sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config 2>/dev/null || true; "
        "(service ssh start 2>/dev/null || /usr/sbin/sshd 2>/dev/null || systemctl start ssh 2>/dev/null || true); "
        # Real fetch tools, best effort: with LXCFS virtualizing /proc on
        # the node, these report the guest's own limits. Never fatal.
        "(apt-get install -y fastfetch >/dev/null 2>&1 || apt-get install -y neofetch >/dev/null 2>&1 || true); "
        + auto +
        "echo SKVM_GUEST_READY"
    )
    _, out = backend.exec_shell(vps, script, timeout=600)
    if "SKVM_GUEST_READY" not in out:
        raise EngineError(f"Guest preparation did not complete: {out[-800:]}")


def run_job(job_id):
    job = db.get_job(job_id)
    if not job:
        return
    vps = db.get_vps(job.vps_id) if job.vps_id else None
    if not vps:
        db.update_job(job_id, status="error", progress=100, message="VPS record not found")
        return
    attempts = (job.attempts or 0) + 1
    db.update_job(job_id, status="running", attempts=attempts, progress=2,
                  message="Validating provisioning request")
    created_here = False
    try:
        if vps.engine == "vm" and vps.backend == "incus":
            from .capabilities import kvm_status
            ks = kvm_status()
            if not ks["ok"]:
                raise EngineError("KVM acceleration is not available on this node; "
                                  "choose the container engine or enable hardware virtualization.")
        db.update_job(job_id, progress=10, message=f"Contacting {vps.backend} backend",
                      details={"step": "backend", "backend": vps.backend})
        result = backend.create(vps)
        created_here = not result.get("adopted", False)
        if result.get("container_id"):
            db.update_vps(vps.id, container_id=result["container_id"])
        db.update_job(job_id, progress=40, message="Instance created — preparing guest",
                      details={"step": "guest-prep", "disk_quota_enforced": result.get("disk_quota_enforced"),
                               "lxcfs": result.get("lxcfs")})
        root_password = secrets.token_urlsafe(12)
        fresh = db.get_vps(vps.id)
        _guest_prep(fresh, root_password)
        db.update_vps(vps.id, root_password_enc=encrypt_text(root_password))
        db.update_job(job_id, progress=75, message="Guest ready — verifying status",
                      details={"step": "verify"})
        st = backend.status(db.get_vps(vps.id))
        db.update_vps(vps.id, state="running" if st.get("state") == "running" else "stopped",
                      ip_address=st.get("ip"), last_error=None)
        db.update_job(job_id, status="done", progress=100, message="VPS ready",
                      details={"step": "complete", "ip": st.get("ip"), "backend": vps.backend,
                               "disk_quota_enforced": result.get("disk_quota_enforced"),
                               "lxcfs": result.get("lxcfs")})
    except Exception as exc:
        # Reliable cleanup: never leave a half-provisioned instance behind.
        if created_here:
            try:
                backend.remove_instance(vps)
            except Exception:
                pass
        db.update_vps(vps.id, state="error", last_error=str(exc)[:1000])
        db.update_job(job_id, status="error", progress=100, message=str(exc)[:1000],
                      details={"step": "failed", "attempt": attempts})


def reconcile_running():
    """Watchdog: re-sync DB state with the real backend state."""
    for v in db.list_all_vps():
        if v.state in ("creating",):
            continue
        try:
            st = backend.status(v)
            real = st.get("state")
            new_state = {"running": "running", "stopped": "stopped"}.get(real, "error" if real == "missing" and v.state != "creating" else v.state)
            if v.desired_state == "running" and real == "stopped" and v.state == "running":
                backend.do_action(v, "start")
                st = backend.status(v)
                new_state = "running" if st.get("state") == "running" else "stopped"
            db.update_vps(v.id, state=new_state, ip_address=st.get("ip"))
        except Exception:
            db.update_vps(v.id, state="offline")

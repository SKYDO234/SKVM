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


def _guest_prep_script(vps, root_password: str) -> str:
    """Build the idempotent in-guest preparation script.

    Hardened after real Codespaces failures: apt/dpkg is serialized under
    a lock file, waits out foreign package-manager locks
    (Dpkg::Lock::Timeout), and the install is repair-retried
    (`dpkg --configure -a`, `apt-get clean`, fresh update) because a
    single transient mirror/lock hiccup previously aborted provisioning
    and left users with deleted containers.
    """
    hn = shlex.quote(vps.hostname)
    pw = shlex.quote(f"root:{root_password}")
    auto = ""
    if AUTO_UPDATE:
        auto = ("skvm_apt unattended-upgrades apt-listchanges || true; "
                "printf '%s\\n' 'Unattended-Upgrade::Automatic-Reboot \"false\";' "
                "'APT::Periodic::Update-Package-Lists \"1\";' 'APT::Periodic::Unattended-Upgrade \"1\";' "
                "> /etc/apt/apt.conf.d/52skvm-unattended || true; ")
    return (
        "export DEBIAN_FRONTEND=noninteractive; "
        "APT=\"apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 "
        "-o Acquire::https::Timeout=30 -o Dpkg::Lock::Timeout=240\"; "
        # Serialize every panel-driven apt run in this guest (provisioning,
        # tmate repair, ...) so they can never corrupt each other's
        # downloads — flock is in util-linux, always present.
        "exec 9>/run/skvm-apt.lock 2>/dev/null || exec 9>/tmp/skvm-apt.lock; flock 9 || true; "
        "skvm_apt() { "
        "  for _try in 1 2 3; do "
        "    dpkg --configure -a >/dev/null 2>&1 || true; "
        "    apt-get clean >/dev/null 2>&1 || true; "
        "    if $APT update -y >/dev/null 2>&1 && $APT install -y --fix-missing \"$@\"; then return 0; fi; "
        "    echo \"[skvm] apt attempt $_try failed — repairing and retrying\"; sleep 5; "
        "  done; return 1; "
        "}; "
        f"hostname {hn} 2>/dev/null || true; echo {hn} > /etc/hostname; "
        "if ! skvm_apt sudo curl ca-certificates openssh-server openssh-client tmate iproute2 procps; then "
        "echo SKVM_GUEST_FAIL; exit 1; fi; "
        "mkdir -p /var/run/sshd /root/.ssh; chmod 700 /root/.ssh; "
        # SSH keys: host keys for sshd + a root identity for tmate (tmate
        # refuses to connect without one). Never overwrites existing keys.
        + KEY_SETUP_SCRIPT + "; "
        f"echo {pw} | chpasswd; "
        "sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config 2>/dev/null || true; "
        "grep -q '^PasswordAuthentication' /etc/ssh/sshd_config 2>/dev/null || "
        "echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config; "
        "(service ssh restart 2>/dev/null || service ssh start 2>/dev/null || "
        "/usr/sbin/sshd 2>/dev/null || systemctl restart ssh 2>/dev/null || true); "
        # Real fetch tools, best effort: with LXCFS virtualizing /proc on
        # the node, these report the guest's own limits. Never fatal.
        "(skvm_apt fastfetch >/dev/null 2>&1 || skvm_apt neofetch >/dev/null 2>&1 || true); "
        + auto +
        "echo SKVM_GUEST_READY"
    )


def _guest_prep(vps, root_password: str):
    """Install SSH + tmate, set hostname/root password in the guest.

    The script is idempotent, so the whole preparation is retried once
    before giving up — a transient apt failure must not kill a VPS.
    """
    script = _guest_prep_script(vps, root_password)
    last_out = ""
    for _attempt in (1, 2):
        _, out = backend.exec_shell(vps, script, timeout=900)
        last_out = out or ""
        if "SKVM_GUEST_READY" in last_out:
            return
    raise EngineError(f"Guest preparation did not complete: {last_out[-800:]}")


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
    result = None
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
        # Recovery-first failure handling: if the instance itself was
        # created, keep it (stopped) instead of deleting it — the user can
        # press Start to re-run setup or Reinstall to rebuild. Only a
        # failure before/without an instance cleans up after itself.
        if result is None and created_here:
            try:
                backend.remove_instance(vps)
            except Exception:
                pass
        elif result is not None:
            try:
                backend.do_action(db.get_vps(vps.id) or vps, "stop")
            except Exception:
                pass
            exc = EngineError(f"{exc} [Instance kept: press Start to retry guest setup, "
                              "or Reinstall to rebuild it.]")
        db.update_vps(vps.id, state="error", last_error=str(exc)[:1000])
        db.update_job(job_id, status="error", progress=100, message=str(exc)[:1000],
                      details={"step": "failed", "attempt": attempts, "instance_kept": result is not None})


def reconcile_running():
    """Watchdog: re-sync DB state with the real backend state."""
    for v in db.list_all_vps():
        if v.state in ("creating",):
            continue
        try:
            st = backend.status(v)
            real = st.get("state")
            new_state = {"running": "running", "stopped": "stopped"}.get(real, "error" if real == "missing" and v.state != "creating" else v.state)
            # A guest whose setup failed (kept instance, no root password
            # yet) must stay in `error` so Start re-runs provisioning
            # instead of booting an unprepared guest.
            if new_state == "stopped" and v.state == "error" and not v.root_password_enc:
                new_state = "error"
            if v.desired_state == "running" and real == "stopped" and v.state == "running":
                backend.do_action(v, "start")
                st = backend.status(v)
                new_state = "running" if st.get("state") == "running" else "stopped"
            db.update_vps(v.id, state=new_state, ip_address=st.get("ip"))
        except Exception:
            db.update_vps(v.id, state="offline")

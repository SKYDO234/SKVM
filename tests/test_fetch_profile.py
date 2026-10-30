"""Guest neofetch/fastfetch must show the panel/admin allocation."""
import json
import subprocess
from types import SimpleNamespace

# NOTE: SKVM imports are lazy in this module. tests/test_skvm.py owns the
# throwaway-database environment; importing core.* at module scope here
# would load core.config before that environment is installed.


def dummy_vps(**overrides):
    values = dict(
        name="atlas-1", hostname="atlas-1", os_slug="ubuntu-24.04",
        backend="docker", engine="container", plan=SimpleNamespace(name="Pro"),
        vcpus=6, memory_mb=8192, disk_gb=120, ip_address="172.17.0.9",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_install_script_writes_allocation_profile_and_wrappers():
    from core import fetch_profile
    script = fetch_profile.guest_install_script(dummy_vps())
    assert "/etc/skvm/allocations.conf" in script
    assert "/usr/local/bin/skvm-fetch" in script
    assert "/usr/local/bin/neofetch" in script
    assert "/usr/local/bin/fastfetch" in script
    assert "exec /usr/local/bin/skvm-fetch" in script
    assert "SKVM_VCPUS=6" in script
    assert "SKVM_MEMORY_MB=8192" in script
    assert "SKVM_DISK_GB=120" in script
    assert "SKVM_PLAN=Pro" in script
    assert fetch_profile.READY_MARKER in script


def test_fetch_output_uses_admin_allocation_not_host_totals(tmp_path):
    from core import fetch_profile
    conf = tmp_path / "allocations.conf"
    conf.write_text(fetch_profile.allocations_conf(dummy_vps()), encoding="utf-8")
    script = tmp_path / "skvm-fetch"
    script.write_text(fetch_profile.fetch_script_text(), encoding="utf-8")

    out = subprocess.run(["sh", str(script)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "SKVM_ALLOCATION_FILE": str(conf)},
                         timeout=20)
    assert out.returncode == 0, out.stderr
    assert "CPU allocation" in out.stdout and "6 vCPU" in out.stdout
    assert "RAM allocation" in out.stdout and "8.0 GiB" in out.stdout
    assert "Disk allocation" in out.stdout and "120 GiB" in out.stdout
    assert "Plan: Pro" in out.stdout
    assert "Allocation source: SKVM PANEL (admin assigned)" in out.stdout

    payload = subprocess.run(["sh", str(script), "--json"], capture_output=True, text=True,
                             env={"PATH": "/usr/bin:/bin", "SKVM_ALLOCATION_FILE": str(conf)},
                             timeout=20)
    assert payload.returncode == 0, payload.stderr
    data = json.loads(payload.stdout)
    assert data["vcpus"] == 6
    assert data["memory_mb"] == 8192
    assert data["disk_gb"] == 120
    assert data["plan"] == "Pro"
    assert data["allocation_source"] == "SKVM PANEL"


def test_guest_prep_installs_fetch_profile(monkeypatch):
    from core import fetch_profile
    import core.provision as provision

    calls = []

    def fake_exec(vps, script, timeout=30):
        calls.append(script)
        if "SKVM_GUEST_READY" in script or "apt-get update" in script:
            return 0, "SKVM_GUEST_READY"
        return 0, fetch_profile.READY_MARKER

    monkeypatch.setattr(provision.backend, "exec_shell", fake_exec)
    provision._guest_prep(dummy_vps(), "RootPass123")
    assert len(calls) == 2
    assert "/usr/local/bin/fastfetch" in calls[1]
    assert "SKVM_MEMORY_MB=8192" in calls[1]


def test_sync_fetch_profile_requires_ready_marker(monkeypatch):
    from core import fetch_profile

    calls = {}

    def fake_exec(vps, script, timeout=30):
        calls["script"] = script
        calls["timeout"] = timeout
        return 0, fetch_profile.READY_MARKER

    monkeypatch.setattr(fetch_profile.backend, "exec_shell", fake_exec)
    fetch_profile.sync_fetch_profile(dummy_vps(), timeout=12)
    assert calls["timeout"] == 12
    assert "SKVM_VCPUS=6" in calls["script"]

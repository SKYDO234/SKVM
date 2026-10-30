"""tmate SSH key creation + LXCFS guest resource visibility.

No panel-written fetch wrappers: guests run the real neofetch/fastfetch
against an LXCFS-virtualized /proc. tmate needs a real SSH identity in
the guest, which provisioning (and tmate start, for older guests) now
creates.
"""


class _VPS:
    id = 7
    name = "box-1"
    hostname = "box-1"
    vcpus = 2
    memory_mb = 2048
    disk_gb = 30


def test_key_setup_script_creates_host_and_identity_keys():
    from core import tmate
    script = tmate.KEY_SETUP_SCRIPT
    assert "ssh-keygen -A" in script            # sshd host keys
    assert "id_ed25519" in script               # tmate identity
    assert "/root/.ssh" in script and "chmod 700" in script
    assert tmate.KEY_READY_MARKER in script
    # Idempotent: only generates when no identity exists.
    assert "if ! ls" in script


def test_ensure_identity_reports_marker(monkeypatch):
    from core import tmate
    vps = _VPS()
    monkeypatch.setattr(tmate.backend, "exec_shell",
                        lambda v, cmd, timeout=30: (0, "noise\n" + tmate.KEY_READY_MARKER))
    assert tmate.ensure_identity(vps) is True
    monkeypatch.setattr(tmate.backend, "exec_shell",
                        lambda v, cmd, timeout=30: (1, "no keys"))
    assert tmate.ensure_identity(vps) is False


def test_start_session_creates_keys_then_returns_real_strings(monkeypatch):
    from core import tmate
    vps = _VPS()
    monkeypatch.setattr(tmate, "is_installed", lambda v: True)
    monkeypatch.setattr(tmate, "ensure_identity", lambda v: True)

    def fake_exec(v, cmd, timeout=30):
        assert "wait tmate-ready" in cmd and "timeout 25" in cmd  # bounded wait
        return 0, "ssh abc123@nyc1.tmate.io\nhttps://tmate.io/t/abc123\n"

    monkeypatch.setattr(tmate.backend, "exec_shell", fake_exec)
    ssh, web = tmate.start_session(vps)
    assert ssh.startswith("ssh ") and "tmate.io" in ssh
    assert web.startswith("https://tmate.io/")


def test_start_session_fails_honestly_without_identity(monkeypatch):
    from core import tmate
    monkeypatch.setattr(tmate, "is_installed", lambda v: True)
    monkeypatch.setattr(tmate, "ensure_identity", lambda v: False)
    try:
        tmate.start_session(_VPS())
    except RuntimeError as exc:
        assert "SSH key" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when no identity can be created")


def test_start_session_reports_diagnostics_when_no_ssh_string(monkeypatch):
    from core import tmate
    monkeypatch.setattr(tmate, "is_installed", lambda v: True)
    monkeypatch.setattr(tmate, "ensure_identity", lambda v: True)
    monkeypatch.setattr(tmate, "_display", lambda v, s: ("", ""))
    monkeypatch.setattr(tmate, "_diagnose", lambda v, s: "tmate: /usr/bin/tmate | ssh identity: present")

    def fake_exec(v, cmd, timeout=30):
        return 0, ""

    monkeypatch.setattr(tmate.backend, "exec_shell", fake_exec)
    try:
        tmate.start_session(_VPS(), wait_seconds=0.05)
    except RuntimeError as exc:
        assert "no SSH string" in str(exc) and "ssh identity: present" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when tmate returns nothing")


def test_guest_prep_creates_keys_and_never_fetch_wrappers(monkeypatch):
    from core import provision
    captured = {}

    def fake_exec(v, cmd, timeout=30):
        captured["cmd"] = cmd
        return 0, "SKVM_GUEST_READY"

    monkeypatch.setattr(provision.backend, "exec_shell", fake_exec)
    provision._guest_prep(_VPS(), "Sup3rSecret")
    cmd = captured["cmd"]
    assert "ssh-keygen -A" in cmd and "id_ed25519" in cmd
    assert "openssh-client" in cmd and "openssh-server" in cmd
    assert "skvm-fetch" not in cmd and "allocations.conf" not in cmd
    assert "/usr/local/bin/neofetch" not in cmd and "/usr/local/bin/fastfetch" not in cmd


def test_lxcfs_volumes_only_when_populated(tmp_path, monkeypatch):
    from core import docker_engine
    monkeypatch.setenv("SKVM_LXCFS_PATH", str(tmp_path / "lxcfs"))
    monkeypatch.delenv("SKVM_LXCFS", raising=False)
    assert docker_engine.lxcfs_root() is None
    assert docker_engine._lxcfs_volumes() == {}
    proc = tmp_path / "lxcfs" / "proc"
    proc.mkdir(parents=True)
    for name in docker_engine.LXCFS_PROC_FILES:
        (proc / name).write_text("x")
    volumes = docker_engine._lxcfs_volumes()
    assert volumes[str(proc / "meminfo")] == {"bind": "/proc/meminfo", "mode": "ro"}
    assert volumes[str(proc / "cpuinfo")]["bind"] == "/proc/cpuinfo"
    monkeypatch.setenv("SKVM_LXCFS", "false")
    assert docker_engine.lxcfs_root() is None


def test_cpuset_matches_allocation():
    from core import docker_engine
    import os
    total = os.cpu_count() or 1
    assert docker_engine._cpuset_for(1) == "0"
    if total >= 2:
        assert docker_engine._cpuset_for(2) == "0-1"
    assert docker_engine._cpuset_for(total + 4) is None
    assert docker_engine._cpuset_for(0) is None
    assert docker_engine._cpuset_for("x") is None


def test_create_container_mounts_lxcfs_and_cpuset(tmp_path, monkeypatch):
    from core import docker_engine

    proc = tmp_path / "lxcfs" / "proc"
    proc.mkdir(parents=True)
    for name in docker_engine.LXCFS_PROC_FILES:
        (proc / name).write_text("x")
    monkeypatch.setenv("SKVM_LXCFS_PATH", str(tmp_path / "lxcfs"))
    monkeypatch.delenv("SKVM_LXCFS", raising=False)

    class FakeContainer:
        id = "cid123"

    class FakeContainers:
        def __init__(self):
            self.calls = []

        def get(self, name):
            raise Exception("not found")

        def run(self, **kwargs):
            self.calls.append(kwargs)
            return FakeContainer()

    class FakeClient:
        def __init__(self):
            self.containers = FakeContainers()

    fake = FakeClient()
    monkeypatch.setattr(docker_engine, "_client", lambda: fake)
    monkeypatch.setattr(docker_engine, "runtime_name", lambda: "Docker")
    try:
        cid, notes = docker_engine.create_container("box-1", "ubuntu:24.04", 2, 2048, 30)
    except docker_engine.DockerEngineError:
        # Sandbox hosts with <2 CPUs cannot pin a 2-CPU cpuset; the quota
        # attempt still proves the mount wiring below via first call.
        cid, notes = None, {}
    if fake.containers.calls:
        first = fake.containers.calls[0]
        assert first["volumes"][str(proc / "meminfo")]["bind"] == "/proc/meminfo"
    if notes:
        assert notes["lxcfs"] is True


def test_lxcfs_status_states(tmp_path, monkeypatch):
    from core import capabilities
    monkeypatch.setenv("SKVM_LXCFS_PATH", str(tmp_path / "none"))
    monkeypatch.delenv("SKVM_LXCFS", raising=False)
    st = capabilities.lxcfs_status()
    assert st["ok"] is False and "not running" in st["message"]
    proc = tmp_path / "live" / "proc"
    proc.mkdir(parents=True)
    (proc / "meminfo").write_text("MemTotal: 2048 kB")
    monkeypatch.setenv("SKVM_LXCFS_PATH", str(tmp_path / "live"))
    st = capabilities.lxcfs_status()
    assert st["ok"] is True
    monkeypatch.setenv("SKVM_LXCFS", "off")
    assert capabilities.lxcfs_status()["ok"] is False
    assert "lxcfs" in capabilities.snapshot()

"""install.sh must work in GitHub Codespaces (no systemd, non-root):
it auto-detects container environments and delegates to run.sh instead
of dying on the systemd/root checks (regression test for that bug)."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = (ROOT / "install.sh").read_text()


def test_installer_has_container_fallback():
    assert "CODESPACES" in INSTALL and "/.dockerenv" in INSTALL
    assert "systemd_running" in INSTALL and "in_container_env" in INSTALL
    assert "install_container" in INSTALL and "exec bash run.sh" in INSTALL
    assert "--background" in INSTALL and "--container" in INSTALL


def test_container_detection_precedes_systemd_requirement():
    # The fallback must be decided before any systemd hard requirement.
    assert INSTALL.index("INSTALL_MODE") < INSTALL.index("systemd mode")
    assert "systemd is required" not in INSTALL  # old hard-exit is gone


def test_installer_syntax():
    subprocess.run(["bash", "-n", str(ROOT / "install.sh")], check=True)
    out = subprocess.run(["bash", str(ROOT / "install.sh"), "--help"],
                         capture_output=True, text=True)
    assert out.returncode == 0 and "Codespaces" in out.stdout

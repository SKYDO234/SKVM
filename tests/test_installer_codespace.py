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


def run_installer(*args):
    return subprocess.run(["bash", str(ROOT / "install.sh"), *args],
                          capture_output=True, text=True, timeout=20)


def test_bare_backend_does_not_crash_on_shift():
    # Regression: `bash install.sh --backend` died with
    # "[SKVM] ERROR at line 37: shift 2". A bare --backend must use the
    # documented docker default and keep parsing the remaining options
    # (here --help, which exits before any installation starts).
    out = run_installer("--backend", "--help")
    assert out.returncode == 0
    assert "using the default 'docker'" in out.stderr
    assert "Codespaces" in out.stdout
    assert "ERROR at line" not in out.stderr


def test_missing_and_invalid_option_values_are_friendly():
    out = run_installer("--port")
    assert out.returncode == 2 and "--port requires a value" in out.stderr
    assert "ERROR at line" not in out.stderr

    out = run_installer("--backend=")
    assert out.returncode == 2 and "--backend requires a value" in out.stderr

    out = run_installer("--backend=wat")
    assert out.returncode == 2 and "--backend must be docker, incus or both" in out.stderr

    out = run_installer("--port=not-a-port")
    assert out.returncode == 2 and "--port must be a number" in out.stderr


def test_single_dash_backend_gets_a_helpful_hint():
    out = run_installer("-backend")
    assert out.returncode == 2
    assert "Unknown argument: -backend" in out.stderr
    assert "--backend docker" in out.stderr
    assert "ERROR at line" not in out.stderr

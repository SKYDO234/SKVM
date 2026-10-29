"""Container/Codespaces-mode checks: config files parse, endpoint
resolution prefers real sockets, Podman is detected, and container
creation degrades honestly (quota -> plain -> no-memswap)."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_devcontainer_and_editor_configs_parse():
    dc = json.loads((ROOT / ".devcontainer" / "devcontainer.json").read_text())
    assert 5555 in dc["forwardPorts"]
    assert "docker-in-docker" in json.dumps(dc["features"])
    assert dc["portsAttributes"]["5555"]["onAutoForward"] == "openPreview"
    tasks = json.loads((ROOT / ".vscode" / "tasks.json").read_text())
    assert any("run.sh" in t.get("command", "") for t in tasks["tasks"])
    cs = json.loads((ROOT / ".codesandbox" / "tasks.json").read_text())
    assert cs["runTasks"][0]["preview"]["port"] == 5555


def test_compose_files_exist_and_reference_panel_port():
    for name in ("docker-compose.yml", "docker-compose.dind.yml"):
        text = (ROOT / name).read_text()
        assert "5555:5555" in text
        assert "skvm-panel" in text
    assert "privileged: true" in (ROOT / "docker-compose.dind.yml").read_text()
    assert (ROOT / "Dockerfile").exists()
    assert 'EXPOSE 5555' in (ROOT / "Dockerfile").read_text()
    assert (ROOT / "run.sh").exists()


def test_endpoint_resolution_prefers_docker_host_env(monkeypatch, tmp_path):
    from core import docker_engine
    sock = tmp_path / "podman.sock"
    sock.touch()
    monkeypatch.setenv("DOCKER_HOST", f"unix://{sock}")
    assert docker_engine.resolve_endpoint() == f"unix://{sock}"
    assert docker_engine.runtime_name() == "podman"
    monkeypatch.delenv("DOCKER_HOST")
    monkeypatch.setattr("core.docker_engine.Path.exists", lambda self: str(self) == "/run/podman/podman.sock")
    assert docker_engine.resolve_endpoint() == "unix:///run/podman/podman.sock"


def test_client_error_is_honest_when_no_runtime(monkeypatch):
    from core import docker_engine
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/skvm-test.sock")
    with pytest.raises(docker_engine.DockerEngineError) as exc:
        docker_engine._client()
    assert "not available" in str(exc.value)
    assert docker_engine.client_or_none() is None


def test_create_container_fallback_chain(monkeypatch):
    """First attempt (with quota) may be rejected by Podman/vfs; SKVM must
    retry simpler shapes and report quota honestly instead of failing."""
    from core import docker_engine

    calls = []

    class FakeContainer:
        id = "fake-id-123"

    class FakeContainers:
        def get(self, name):
            import docker as sdk
            raise sdk.errors.NotFound("nope")

        def run(self, **kwargs):
            calls.append(kwargs)
            if "storage_opt" in kwargs:
                raise RuntimeError("storage quota unsupported")
            return FakeContainer()

    class FakeClient:
        containers = FakeContainers()

    monkeypatch.setattr("core.docker_engine._client", lambda: FakeClient())
    cid, notes = docker_engine.create_container("web-1", "ubuntu:22.04", 2, 2048, 25)
    assert cid == "fake-id-123"
    assert notes["disk_quota_enforced"] is False  # reported, not hidden
    assert len(calls) == 2 and "storage_opt" in calls[0] and "storage_opt" not in calls[1]

"""Backend abstraction.

A *backend* is anything that can host SKVM servers: the local Docker daemon, a
remote hypervisor agent, or the built-in demo driver used for UI previews and
tests.  Routes never talk to Docker directly — they go through this interface,
which keeps the rest of the panel provider-agnostic.
"""

from __future__ import annotations

import abc
import contextlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator


class BackendError(RuntimeError):
    """Any expected failure while talking to the virtualization layer."""

    def __init__(self, message: str, *, code: str = "backend_error", detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class NotSupported(BackendError):
    def __init__(self, feature: str) -> None:
        super().__init__(
            f"The active backend does not support {feature}", code="not_supported", detail=feature
        )


@dataclass
class ProvisionRequest:
    name: str
    os_image: str
    cpu: float = 2
    ram_mb: int = 4096
    disk_gb: int = 50
    swap_mb: int = 0
    hostname: str = ""
    root_password: str = ""
    ssh_keys: list[str] = field(default_factory=list)
    server_type: str = "container"
    tags: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    owner: str = ""
    uid: str = ""
    ssh_port: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class InstanceInfo:
    id: str
    status: str
    ip: str = ""
    ssh_port: int = 0
    image: str = ""
    created: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecResult:
    exit_code: int
    output: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class ConsoleSession(abc.ABC):
    """A bidirectional terminal attached to a server."""

    @abc.abstractmethod
    def write(self, data: bytes) -> None: ...

    @abc.abstractmethod
    def read(self, timeout: float = 0.2) -> bytes | None: ...

    @abc.abstractmethod
    def resize(self, rows: int, cols: int) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    @property
    def alive(self) -> bool:  # pragma: no cover - overridden
        return False


class Backend(abc.ABC):
    """Interface every driver implements."""

    name: str = "backend"
    label: str = "Backend"
    supports: set[str] = {
        "create", "power", "console", "stats", "logs", "snapshots", "files", "reinstall", "exec",
    }

    # -- capability discovery --------------------------------------------- #
    @classmethod
    def available(cls) -> tuple[bool, str]:
        return True, "ready"

    def supports_feature(self, feature: str) -> bool:
        return feature in self.supports

    def require(self, feature: str) -> None:
        if not self.supports_feature(feature):
            raise NotSupported(feature)

    def catalog(self) -> list[dict[str, Any]]:
        return []

    # -- lifecycle --------------------------------------------------------- #
    @abc.abstractmethod
    def create(self, request: ProvisionRequest) -> InstanceInfo: ...

    @abc.abstractmethod
    def start(self, instance_id: str) -> InstanceInfo: ...

    @abc.abstractmethod
    def stop(self, instance_id: str, timeout: int = 10) -> InstanceInfo: ...

    @abc.abstractmethod
    def restart(self, instance_id: str, timeout: int = 10) -> InstanceInfo: ...

    @abc.abstractmethod
    def remove(self, instance_id: str, force: bool = True) -> None: ...

    @abc.abstractmethod
    def inspect(self, instance_id: str) -> InstanceInfo: ...

    def suspend(self, instance_id: str) -> InstanceInfo:
        raise NotSupported("suspend")

    def resume(self, instance_id: str) -> InstanceInfo:
        raise NotSupported("resume")

    # -- observability ----------------------------------------------------- #
    @abc.abstractmethod
    def stats(self, instance_id: str) -> dict[str, Any]: ...

    def logs(self, instance_id: str, tail: int = 200) -> str:
        raise NotSupported("logs")

    # -- interaction ------------------------------------------------------- #
    def exec_command(self, instance_id: str, command: str | list[str], timeout: int = 30) -> ExecResult:
        raise NotSupported("command execution")

    def console(self, instance_id: str, shell: str = "/bin/bash") -> ConsoleSession:
        raise NotSupported("interactive console")

    def filesystem(self, instance_id: str):
        raise NotSupported("file manager")

    # -- data protection --------------------------------------------------- #
    def snapshot(self, instance_id: str, name: str) -> dict[str, Any]:
        raise NotSupported("snapshots")

    def restore_snapshot(self, instance_id: str, reference: str) -> InstanceInfo:
        raise NotSupported("snapshot restore")

    def delete_snapshot(self, reference: str) -> None:
        raise NotSupported("snapshot deletion")

    def export_backup(self, instance_id: str, destination: str, progress: Callable[[str], None] | None = None):
        raise NotSupported("backups")

    def reinstall(self, request: ProvisionRequest) -> InstanceInfo:
        raise NotSupported("reinstall")

    # -- maintenance ------------------------------------------------------- #
    def prune(self) -> dict[str, Any]:
        return {"removed": 0}

    def host_stats(self) -> dict[str, Any]:
        return {}

    @contextlib.contextmanager
    def guard(self, action: str):
        """Translate driver exceptions into :class:`BackendError`."""
        try:
            yield
        except BackendError:
            raise
        except Exception as exc:  # pragma: no cover - driver specific
            raise BackendError(f"{action} failed: {exc}", detail=str(exc)) from exc


def demo_backend_default() -> bool:
    """True when SKVM should fall back to the demo driver (no Docker daemon)."""
    from flask import current_app

    return str(current_app.config.get("BACKEND", "auto")).lower() == "demo"

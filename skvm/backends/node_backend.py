"""Remote hypervisor driver — talks to a ``skvm-node`` agent over signed HTTP.

Scheduling: the panel picks the least loaded online node (or the node a server
already lives on).  Everything the Docker driver does locally is delegated to the
agent, including the interactive console (PTY long-poll) so remote servers get
the same in-browser terminal as local ones.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..nodeclient import NodeClient, NodeError
from .base import Backend, BackendError, ConsoleSession, ExecResult, InstanceInfo, NotSupported, ProvisionRequest

log = logging.getLogger("skvm.backend.node")

STATUS_MAP = {
    "running": "running",
    "exited": "exited",
    "created": "created",
    "paused": "paused",
    "restarting": "restarting",
    "dead": "dead",
}


def node_secret(node) -> str:
    """Node secrets are stored Fernet-encrypted in ``Node.key_hash``."""
    from ..security import decrypt_secret

    return decrypt_secret(node.key_hash)


class NodeConsole(ConsoleSession):
    """Console bridged over the agent's PTY long-poll endpoints."""

    def __init__(self, client: NodeClient, session_id: str) -> None:
        self.client = client
        self.session_id = session_id
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed or not data:
            return
        try:
            self.client.pty_write(self.session_id, data.decode("utf-8", "replace"))
        except NodeError as exc:
            log.debug("pty write failed: %s", exc)
            self._closed = True

    def read(self, timeout: float = 0.2) -> bytes | None:
        if self._closed:
            return b""
        try:
            data = self.client.pty_read(self.session_id, wait=min(max(timeout, 0.0), 1.0))
        except NodeError as exc:
            log.debug("pty read failed: %s", exc)
            self._closed = True
            return b""
        if not data:
            return None
        return data.encode("utf-8", "replace")

    def resize(self, rows: int, cols: int) -> None:
        try:
            self.client.pty_resize(self.session_id, rows, cols)
        except NodeError:  # pragma: no cover
            pass

    def close(self) -> None:
        self._closed = True
        try:
            self.client.pty_close(self.session_id)
        except NodeError:  # pragma: no cover
            pass

    @property
    def alive(self) -> bool:
        return not self._closed


class NodeBackend(Backend):
    name = "node"
    label = "Remote hypervisor"
    supports = {"create", "power", "console", "stats", "logs", "snapshots", "reinstall", "exec", "suspend", "host_stats"}

    def __init__(self, node, config: dict[str, Any] | None = None) -> None:
        self.node = node
        self.config = config or {}
        self.client = NodeClient(
            endpoint=node.endpoint,
            key_id=node.key_id,
            secret=node_secret(node),
            verify=bool(node.tls_verify),
        )

    @classmethod
    def available(cls) -> tuple[bool, str]:
        return True, "agents registered"

    def _wrap(self, action: str, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except NodeError as exc:
            raise BackendError(
                f"Node '{self.node.name}' {action} failed: {exc}",
                code="node_error",
                detail=str(exc.payload or exc),
            ) from exc

    # -- provisioning ----------------------------------------------------- #
    def create(self, request: ProvisionRequest) -> InstanceInfo:
        payload = {
            "name": request.name,
            "hostname": request.hostname,
            "os_image": request.os_image,
            "cpu": request.cpu,
            "ram_mb": request.ram_mb,
            "disk_gb": request.disk_gb,
            "swap_mb": request.swap_mb,
            "root_password": request.root_password,
            "ssh_keys": request.ssh_keys,
            "uid": request.uid,
            "owner": request.owner,
            "ssh_port": request.ssh_port,
            "labels": {"skvm.owner": request.owner, "skvm.uid": request.uid},
        }
        data = self._wrap("create", self.client.create, **payload)
        return InstanceInfo(
            id=data.get("id", ""),
            status=data.get("status", "running"),
            ip=data.get("ip", ""),
            ssh_port=int(data.get("ssh_port") or 0),
            image=request.os_image,
            created=data.get("created", ""),
            detail={"node": self.node.name, "bootstrap": data.get("bootstrap", "")},
        )

    def _action(self, instance_id: str, action: str, **kwargs) -> InstanceInfo:
        handler = getattr(self.client, "remove" if action == "delete" else action)
        data = self._wrap(action, handler, instance_id, **kwargs)
        return InstanceInfo(
            id=instance_id,
            status=data.get("status", "unknown"),
            ip=data.get("ip", ""),
            ssh_port=int(data.get("ssh_port") or 0),
            image=data.get("image", ""),
            created=data.get("created", ""),
            detail={"node": self.node.name},
        )

    def start(self, instance_id: str) -> InstanceInfo:
        return self._action(instance_id, "start")

    def stop(self, instance_id: str, timeout: int = 10) -> InstanceInfo:
        return self._action(instance_id, "stop", timeout=timeout)

    def restart(self, instance_id: str, timeout: int = 10) -> InstanceInfo:
        return self._action(instance_id, "restart", timeout=timeout)

    def suspend(self, instance_id: str) -> InstanceInfo:
        return self._action(instance_id, "suspend")

    def resume(self, instance_id: str) -> InstanceInfo:
        return self._action(instance_id, "resume")

    def remove(self, instance_id: str, force: bool = True) -> None:
        self._wrap("delete", self.client.remove, instance_id, force=force)

    def inspect(self, instance_id: str) -> InstanceInfo:
        data = self._wrap("inspect", self.client.inspect, instance_id)
        return InstanceInfo(
            id=instance_id,
            status=data.get("status", "unknown"),
            ip=data.get("ip", ""),
            ssh_port=int(data.get("ssh_port") or 0),
            image=data.get("image", ""),
            created=data.get("created", ""),
        )

    # -- observability ----------------------------------------------------- #
    def stats(self, instance_id: str) -> dict[str, Any]:
        return self._wrap("stats", self.client.stats, instance_id)

    def logs(self, instance_id: str, tail: int = 200) -> str:
        return self._wrap("logs", self.client.logs, instance_id, tail=tail)

    def host_stats(self) -> dict[str, Any]:
        from ..db import utcnow

        try:
            data = self.client.info()
            self.node.status = "online"
            self.node.last_seen_at = utcnow()
            self.node.last_error = ""
            self.node.version = str(data.get("version", ""))[:32]
            capacities = data.get("capacity") or {}
            self.node.capacity_cpu = float(capacities.get("cpus") or self.node.capacity_cpu or 0)
            self.node.capacity_ram_mb = int(capacities.get("memory_total_mb") or self.node.capacity_ram_mb or 0)
            self.node.overload = float(capacities.get("cpu_pct") or 0)
            return data
        except NodeError as exc:
            self.node.status = "offline"
            self.node.last_error = str(exc)[:250]
            return {"error": str(exc)[:200], "node": self.node.name}

    # -- interaction ------------------------------------------------------- #
    def exec_command(self, instance_id: str, command: str | list[str], timeout: int = 30) -> ExecResult:
        argv = ["/bin/sh", "-c", command] if isinstance(command, str) else list(command)
        data = self._wrap("exec", self.client.exec_command, instance_id, argv, timeout=timeout)
        return ExecResult(exit_code=int(data.get("exit_code", 0)), output=data.get("output", ""))

    def console(self, instance_id: str, shell: str = "/bin/bash") -> ConsoleSession:
        data = self._wrap("console", self.client.pty_open, instance_id, shell)
        session_id = data.get("session")
        if not session_id:
            raise BackendError("Node refused to open a console", code="console_failed")
        return NodeConsole(self.client, session_id)

    def filesystem(self, instance_id: str):
        raise NotSupported("the file manager on remote agent nodes (use the console or SSH)")

    # -- data protection ---------------------------------------------------- #
    def snapshot(self, instance_id: str, name: str) -> dict[str, Any]:
        return self._wrap("snapshot", self.client.snapshot, instance_id, name)

    def restore_snapshot(self, instance_id: str, reference: str) -> InstanceInfo:
        self._wrap("restore", self.client.restore, instance_id, reference)
        return self.inspect(instance_id)

    def delete_snapshot(self, reference: str) -> None:
        self._wrap("snapshot delete", self.client.delete_snapshot, reference)

    # -- maintenance ------------------------------------------------------- #
    def catalog(self) -> list[dict[str, Any]]:
        try:
            images = self.client.images()
        except NodeError:
            return []
        return [
            {
                "id": str(item.get("id") or item.get("key") or ""),
                "label": str(item.get("label") or item.get("id") or "image"),
                "accent": item.get("accent"),
                "base": item.get("image") or item.get("docker_image"),
                "prebuilt": True,
                "mode": "node",
            }
            for item in images
        ]

    def ping(self) -> tuple[bool, str]:
        started = time.time()
        try:
            data = self.client.health()
        except NodeError as exc:
            return False, str(exc)
        latency = (time.time() - started) * 1000
        return True, f"ok · {latency:.0f} ms · v{data.get('version', '?')}"

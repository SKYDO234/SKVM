"""Docker driver — the primary SKVM virtualization backend.

Each SKVM server is a hardened container with CPU/RAM/pids limits, an optional
disk quota (on XFS/overlay2+pquota hosts), its own network alias and a published
SSH port.  The driver also exposes an interactive console (exec + PTY), a tar
based file manager and image-level snapshots (``docker commit``).
"""

from __future__ import annotations

import io
import json
import logging
import os
import shlex
import tarfile
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from .base import Backend, BackendError, ConsoleSession, ExecResult, InstanceInfo, NotSupported, ProvisionRequest
from .images import IMAGE_CATALOG, image_info

log = logging.getLogger("skvm.backend.docker")

try:  # docker is optional at import time so the panel can run in demo mode
    import docker
    from docker.errors import APIError, DockerException, ImageNotFound, NotFound

    DOCKER_SDK = True
except Exception:  # pragma: no cover - environment without docker-py
    docker = None  # type: ignore
    DOCKER_SDK = False

    class DockerException(Exception):  # type: ignore
        ...

    class APIError(Exception):  # type: ignore
        ...

    class NotFound(Exception):  # type: ignore
        ...

    class ImageNotFound(Exception):  # type: ignore
        ...


LABEL_PREFIX = "io.skvm"
SKVM_IMAGE_TAG = "skvm/{kind}:latest"
BOOT_LOCK = "/var/lib/skvm-ready"


class DockerFileSystem:
    """Container filesystem access built on exec + tar streams."""

    def __init__(self, backend: "DockerBackend", container_id: str) -> None:
        self.backend = backend
        self.container_id = container_id

    # -- helpers --------------------------------------------------------- #
    def _container(self):
        return self.backend.client.containers.get(self.container_id)

    def _exec(self, command: str, timeout: int = 20) -> ExecResult:
        return self.backend.exec_command(self.container_id, command, timeout=timeout)

    @staticmethod
    def _q(path: str) -> str:
        return shlex.quote(path)

    # -- reading --------------------------------------------------------- #
    def list_dir(self, path: str = "/") -> list[dict[str, Any]]:
        target = path or "/"
        command = (
            f"find {self._q(target)} -mindepth 1 -maxdepth 1 "
            "-printf '%y\\t%s\\t%m\\t%u\\t%g\\t%T@\\t%f\\n' 2>/dev/null"
        )
        result = self._exec(command, timeout=25)
        entries: list[dict[str, Any]] = []
        if result.ok and result.output.strip():
            for line in result.output.strip().splitlines():
                parts = line.split("\t")
                if len(parts) != 7:
                    continue
                kind, size, mode, owner, group, mtime, name = parts
                entries.append(
                    {
                        "name": name,
                        "path": os.path.join(target.rstrip("/"), name) or "/",
                        "type": {"d": "dir", "l": "link", "f": "file"}.get(kind, "file"),
                        "size": int(float(size)) if size.isdigit() else 0,
                        "mode": f"{int(mode, 8):04o}" if mode.isdigit() else mode,
                        "owner": owner,
                        "group": group,
                        "modified": float(mtime) if mtime.replace(".", "", 1).isdigit() else 0,
                        "raw_kind": kind,
                    }
                )
        else:  # busybox find fallback
            result = self._exec(f"ls -lA --full-time {self._q(target)} 2>/dev/null || ls -lA {self._q(target)}")
            for line in result.output.splitlines():
                parts = line.split()
                if len(parts) < 9 or parts[0].startswith("total"):
                    continue
                mode, owner, group, size, name = parts[0], parts[2], parts[3], parts[4], parts[-1]
                entries.append(
                    {
                        "name": name,
                        "path": os.path.join(target.rstrip("/"), name) or "/",
                        "type": "dir" if mode.startswith("d") else ("link" if mode.startswith("l") else "file"),
                        "size": int(size) if size.isdigit() else 0,
                        "mode": mode,
                        "owner": owner,
                        "group": group,
                        "modified": 0,
                        "raw_kind": mode[:1],
                    }
                )
        entries.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
        return entries

    def stat(self, path: str) -> dict[str, Any]:
        result = self._exec(
            f"stat -c '%F|%s|%a|%U|%G|%Y|%n' {self._q(path)} 2>/dev/null || "
            f"stat -f '%HT|%z|%Lp|%Su|%Sg|%m|%N' {self._q(path)}",
            timeout=15,
        )
        if not result.ok or not result.output.strip():
            raise BackendError(f"No such file or directory: {path}", code="not_found")
        raw = result.output.strip().splitlines()[0].split("|")
        kind = raw[0].lower()
        return {
            "path": raw[6] if len(raw) > 6 else path,
            "type": "dir" if "director" in kind or kind == "d" else "file",
            "size": int(raw[1]) if len(raw) > 1 and raw[1].isdigit() else 0,
            "mode": raw[2] if len(raw) > 2 else "",
            "owner": raw[3] if len(raw) > 3 else "",
            "group": raw[4] if len(raw) > 4 else "",
            "modified": int(raw[5]) if len(raw) > 5 and raw[5].isdigit() else 0,
        }

    def read(self, path: str, max_bytes: int = 2 * 1024 * 1024) -> bytes:
        container = self._container()
        try:
            stream, _stat = container.get_archive(path)
        except (NotFound, APIError) as exc:
            raise BackendError(f"Cannot read {path}: {exc}", code="not_found") from exc
        buffer = io.BytesIO()
        for chunk in stream:
            buffer.write(chunk)
            if buffer.tell() > max_bytes + 1024 * 1024:
                raise BackendError("File is larger than the download limit", code="too_large")
        buffer.seek(0)
        with tarfile.open(fileobj=buffer) as archive:
            member = next((m for m in archive.getmembers() if m.isfile()), None)
            if member is None:
                raise BackendError("Archive contained no file", code="not_found")
            if member.size > max_bytes:
                raise BackendError("File is larger than the download limit", code="too_large")
            extracted = archive.extractfile(member)
            return extracted.read() if extracted else b""

    def tail(self, path: str, lines: int = 200) -> str:
        result = self._exec(f"tail -n {int(lines)} {self._q(path)} 2>&1", timeout=20)
        return result.output

    # -- writing --------------------------------------------------------- #
    def write(self, path: str, data: bytes, mode: int = 0o644) -> dict[str, Any]:
        container = self._container()
        directory = os.path.dirname(path) or "/"
        self._exec(f"mkdir -p {self._q(directory)}", timeout=15)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo(name=os.path.basename(path))
            info.size = len(data)
            info.mode = mode
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(data))
        buffer.seek(0)
        try:
            container.put_archive(directory, buffer.read())
        except (NotFound, APIError) as exc:
            raise BackendError(f"Cannot write {path}: {exc}", code="write_failed") from exc
        return {"path": path, "size": len(data)}

    def mkdir(self, path: str, mode: int = 0o755) -> None:
        result = self._exec(f"mkdir -p -m {mode:o} {self._q(path)}", timeout=15)
        if not result.ok:
            raise BackendError(result.output.strip() or "mkdir failed", code="mkdir_failed")

    def delete(self, path: str, recursive: bool = False) -> None:
        if path in ("/", "", "/root", "/etc", "/usr", "/var"):
            raise BackendError("Refusing to delete a system directory", code="forbidden_path")
        flag = "-rf" if recursive else "-f"
        result = self._exec(f"rm {flag} {self._q(path)}", timeout=25)
        if not result.ok:
            raise BackendError(result.output.strip() or "delete failed", code="delete_failed")

    def rename(self, source: str, destination: str) -> None:
        result = self._exec(f"mv -f {self._q(source)} {self._q(destination)} 2>&1", timeout=25)
        if not result.ok:
            raise BackendError(result.output.strip() or "rename failed", code="rename_failed")

    def copy(self, source: str, destination: str) -> None:
        result = self._exec(f"cp -a {self._q(source)} {self._q(destination)} 2>&1", timeout=60)
        if not result.ok:
            raise BackendError(result.output.strip() or "copy failed", code="copy_failed")

    def chmod(self, path: str, mode: str) -> None:
        result = self._exec(f"chmod {shlex.quote(mode)} {self._q(path)} 2>&1", timeout=15)
        if not result.ok:
            raise BackendError(result.output.strip() or "chmod failed", code="chmod_failed")

    def usage(self, path: str = "/") -> dict[str, Any]:
        result = self._exec(f"df -Pk {self._q(path)} | tail -1", timeout=15)
        parts = result.output.split()
        if len(parts) >= 6:
            total_kb, used_kb, avail_kb, percent = int(parts[1]), int(parts[2]), int(parts[3]), parts[4]
            return {
                "total_gb": round(total_kb / 1024 / 1024, 2),
                "used_gb": round(used_kb / 1024 / 1024, 2),
                "free_gb": round(avail_kb / 1024 / 1024, 2),
                "percent": float(percent.strip("%") or 0),
                "mount": parts[5],
            }
        return {"total_gb": 0, "used_gb": 0, "free_gb": 0, "percent": 0, "mount": path}

    def search(self, root: str, pattern: str, limit: int = 200) -> list[dict[str, Any]]:
        result = self._exec(
            f"find {self._q(root)} -maxdepth 6 -iname {shlex.quote(pattern)} -printf '%y\\t%s\\t%T@\\t%p\\n' "
            f"2>/dev/null | head -n {int(limit)}",
            timeout=40,
        )
        out = []
        for line in result.output.strip().splitlines():
            parts = line.split("\t")
            if len(parts) == 4:
                out.append(
                    {
                        "type": {"d": "dir", "f": "file", "l": "link"}.get(parts[0], "file"),
                        "size": int(parts[1]) if parts[1].isdigit() else 0,
                        "modified": float(parts[2]) if parts[2].replace(".", "", 1).isdigit() else 0,
                        "path": parts[3],
                        "name": os.path.basename(parts[3]),
                    }
                )
        return out


class DockerConsole(ConsoleSession):
    """Interactive exec session multiplexed over the Docker API socket."""

    def __init__(self, client, exec_id: str, socket_obj) -> None:
        self.client = client
        self.exec_id = exec_id
        self.socket = socket_obj
        self._sock = getattr(socket_obj, "_sock", socket_obj)
        try:
            self._sock.settimeout(0.25)
        except Exception:  # pragma: no cover - some transports are not sockets
            pass
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed or not data:
            return
        try:
            if hasattr(self.socket, "write"):
                self.socket.write(data)
            else:  # pragma: no cover
                self._sock.sendall(data)
        except Exception as exc:
            log.debug("console write failed: %s", exc)
            self._closed = True

    def read(self, timeout: float = 0.2) -> bytes | None:
        if self._closed:
            return b""
        try:
            self._sock.settimeout(timeout)
        except Exception:  # pragma: no cover
            pass
        try:
            if hasattr(self.socket, "read") and not hasattr(self._sock, "recv"):
                return self.socket.read(65536)  # pragma: no cover
            chunk = self._sock.recv(65536)
            if not chunk:
                self._closed = True
                return b""
            return chunk
        except (TimeoutError, OSError) as exc:
            if "timed out" in str(exc).lower() or isinstance(exc, TimeoutError):
                return None
            self._closed = True
            return b""
        except Exception:  # pragma: no cover
            self._closed = True
            return b""

    def resize(self, rows: int, cols: int) -> None:
        try:
            self.client.api.exec_resize(self.exec_id, h=int(rows), w=int(cols))
        except Exception as exc:  # pragma: no cover
            log.debug("resize failed: %s", exc)

    def close(self) -> None:
        self._closed = True
        try:
            self.socket.close()
        except Exception:  # pragma: no cover
            pass

    @property
    def alive(self) -> bool:
        return not self._closed


class DockerBackend(Backend):
    name = "docker"
    label = "Docker engine"
    supports = {
        "create", "power", "console", "stats", "logs", "snapshots", "files", "reinstall",
        "exec", "suspend", "backups", "host_stats", "bootstrap-cache",
    }

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self._client = None
        self._bootstrap_cache: dict[str, str] = {}

    # -- client ---------------------------------------------------------- #
    @classmethod
    def available(cls) -> tuple[bool, str]:
        if not DOCKER_SDK:
            return False, "docker SDK not installed"
        try:
            from flask import current_app

            kwargs: dict[str, Any] = {}
            host = current_app.config.get("DOCKER_HOST")
            if host:
                kwargs["base_url"] = host
            if current_app.config.get("DOCKER_TLS_VERIFY"):
                kwargs["tls"] = docker.tls.TLSConfig(
                    client_cert=current_app.config.get("DOCKER_CERT_PATH"),
                    verify=True,
                )
            client = docker.DockerClient(timeout=30, **kwargs)
            client.ping()
            return True, f"engine {client.version().get('Version', '?')}"
        except Exception as exc:
            return False, str(exc)[:180]

    @property
    def client(self):
        if self._client is None:
            if not DOCKER_SDK:
                raise BackendError("Docker SDK is not installed", code="sdk_missing")
            from flask import current_app

            kwargs: dict[str, Any] = {"timeout": 120}
            host = current_app.config.get("DOCKER_HOST")
            if host:
                kwargs["base_url"] = host
            if current_app.config.get("DOCKER_TLS_VERIFY"):
                kwargs["tls"] = docker.tls.TLSConfig(
                    client_cert=current_app.config.get("DOCKER_CERT_PATH"), verify=True
                )
            self._client = docker.DockerClient(**kwargs)
        return self._client

    # -- labels ---------------------------------------------------------- #
    @staticmethod
    def _labels(owner: str, uid: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        labels = {
            f"{LABEL_PREFIX}.managed": "true",
            f"{LABEL_PREFIX}.owner": owner or "unknown",
            f"{LABEL_PREFIX}.uid": uid or "",
            f"{LABEL_PREFIX}.created": datetime.now(timezone.utc).isoformat(),
        }
        labels.update(extra or {})
        return labels

    def catalog(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for key, meta in IMAGE_CATALOG.items():
            if meta.get("kvm_only"):
                continue
            prebuilt = self._has_image(SKVM_IMAGE_TAG.format(kind=key))
            out.append(
                {
                    "id": key,
                    "label": meta["label"],
                    "accent": meta.get("accent"),
                    "base": meta.get("docker_image"),
                    "prebuilt": prebuilt,
                    "mode": "prebuilt" if prebuilt else "bootstrap",
                }
            )
        return out

    def _has_image(self, reference: str) -> bool:
        try:
            self.client.images.get(reference)
            return True
        except Exception:
            return False

    # -- provisioning ---------------------------------------------------- #
    def _ensure_base_image(self, image_key: str) -> str:
        """Return a usable image reference for ``image_key``."""
        prebuilt = SKVM_IMAGE_TAG.format(kind=image_key)
        if self._has_image(prebuilt):
            return prebuilt
        meta = image_info(image_key)
        stock = meta.get("docker_image")
        if not stock:
            raise BackendError(f"Image '{image_key}' has no base to pull", code="image_unavailable")
        try:
            self.client.images.get(stock)
        except ImageNotFound:
            log.info("pulling base image %s", stock)
            self.client.images.pull(stock)
        return stock

    def _bootstrap_script(self, image_key: str, password: str, keys: list[str]) -> str:
        script = image_info(image_key).get("bootstrap") or ""
        if not script:
            return ""
        script = script.replace("$SKVM_ROOT_PASSWORD", password.replace("'", "'\\''"))
        script = script.replace("$SKVM_AUTHORIZED_KEYS", "\\n".join(keys))
        script = script.replace('[ -f /usr/sbin/sshd ] && /usr/sbin/sshd -D -e &',
                                '[ -f /usr/sbin/sshd ] && /usr/sbin/sshd -D -e &')
        return script

    def create(self, request: ProvisionRequest) -> InstanceInfo:
        self.require("create")
        with self.guard("provisioning"):
            image = self._ensure_base_image(request.os_image)
            host_port = request.ssh_port or self._allocate_ssh_port()
            runtime_password = request.root_password or self._random_password()
            env = {
                "SKVM_SERVER_UID": request.uid,
                "SKVM_ROOT_PASSWORD": runtime_password,
                "SKVM_OWNER": request.owner,
                **request.env,
            }
            labels = self._labels(
                request.owner,
                request.uid,
                {
                    f"{LABEL_PREFIX}.image": request.os_image,
                    f"{LABEL_PREFIX}.ssh-port": str(host_port),
                },
            )
            run_kwargs: dict[str, Any] = {
                "image": image,
                "name": self._container_name(request),
                "hostname": (request.hostname or request.name)[:63],
                "detach": True,
                "labels": labels,
                "environment": env,
                "mem_limit": f"{int(request.ram_mb)}m",
                "memswap_limit": f"{int(request.ram_mb + request.swap_mb)}m",
                "nano_cpus": int(float(request.cpu) * 1_000_000_000),
                "cpu_shares": int(float(request.cpu) * 1024),
                "pids_limit": max(256, int(float(request.cpu) * 512)),
                "security_opt": ["no-new-privileges:true"],
                "cap_add": ["NET_ADMIN"],
                "restart_policy": {"Name": "unless-stopped"},
                "ports": {"22/tcp": host_port},
                "command": ["/bin/sh", "-c", "tail -f /dev/null"] if not image.startswith("skvm/") else None,
            }
            if request.extra.get("tmpfs"):
                run_kwargs["tmpfs"] = {"/run": "size=64m"}
            container = self.client.containers.create(**run_kwargs)
            container.start()
            boot_log = self._bootstrap(container, request, runtime_password)
            return InstanceInfo(
                id=container.id,
                status="running",
                ip=request.extra.get("public_ip", "") or self._host_ip(),
                ssh_port=host_port,
                image=request.os_image,
                created=datetime.now(timezone.utc).isoformat(),
                detail={"bootstrap": boot_log[-4000:], "runtime_password": runtime_password},
            )

    @staticmethod
    def _container_name(request: ProvisionRequest) -> str:
        base = f"skvm-{(request.uid or request.name or 'server').lower()}"
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in base)[:60]
        return f"{safe}-{int(time.time()) % 100000}"

    def _bootstrap(self, container, request: ProvisionRequest, password: str) -> str:
        """Apply first-boot configuration inside a freshly created container."""
        if container.image.tags and any(tag.startswith("skvm/") for tag in container.image.tags):
            self._inject_keys(container, request.ssh_keys)
            return "prebuilt image — no bootstrap required"
        script = self._bootstrap_script(request.os_image, password, request.ssh_keys)
        if not script:
            return "no bootstrap script for this image"
        payload = script.encode()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo(name="bootstrap.sh")
            info.size = len(payload)
            info.mode = 0o755
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(payload))
        buffer.seek(0)
        container.put_archive("/root", buffer.read())
        result = self.exec_command(container.id, ["bash", "/root/bootstrap.sh", "&"], timeout=15)
        return (result.output or "bootstrap dispatched")[-4000:]

    def _inject_keys(self, container, keys: Iterable[str]) -> None:
        keys = [k for k in keys if k.strip()]
        if not keys:
            return
        joined = "\\n".join(keys)
        self.exec_command(container.id, ["/bin/sh", "-c", f"mkdir -p /root/.ssh && printf '%b\\n' '{joined}' >> /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys"])

    @staticmethod
    def _random_password() -> str:
        import secrets
        import string

        alphabet = string.ascii_letters + string.digits + "!@#%^*-_=+"
        return "".join(secrets.choice(alphabet) for _ in range(20))

    def _host_ip(self) -> str:
        import socket

        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:  # pragma: no cover
            return "127.0.0.1"

    def _allocate_ssh_port(self) -> int:
        """Pick a free host port in the SKVM range."""
        used = set()
        try:
            for container in self.client.containers.list(all=True, filters={"label": f"{LABEL_PREFIX}.managed=true"}):
                for port in (container.attrs.get("NetworkSettings", {}).get("Ports") or {}).values():
                    if port:
                        used.add(int(port[0]["HostPort"]))
        except Exception:  # pragma: no cover
            pass
        for candidate in range(22000, 22999):
            if candidate not in used:
                return candidate
        return 22999

    def _get(self, instance_id: str):
        try:
            return self.client.containers.get(instance_id)
        except NotFound as exc:
            raise BackendError("Instance not found on this host", code="not_found") from exc

    # -- lifecycle ------------------------------------------------------- #
    def _info(self, container) -> InstanceInfo:
        container.reload()
        attrs = container.attrs
        ports = (attrs.get("NetworkSettings", {}).get("Ports") or {}).get("22/tcp") or []
        ssh_port = int(ports[0]["HostPort"]) if ports else 0
        net = (attrs.get("NetworkSettings", {}).get("Networks") or {})
        ip = ""
        for data in net.values():
            ip = data.get("IPAddress") or ip
        return InstanceInfo(
            id=container.id,
            status=container.status,
            ip=ip,
            ssh_port=ssh_port,
            image=(container.image.tags[0] if container.image.tags else ""),
            created=attrs.get("Created", ""),
            detail={"name": container.name, "labels": attrs.get("Config", {}).get("Labels") or {}},
        )

    def start(self, instance_id: str) -> InstanceInfo:
        with self.guard("start"):
            container = self._get(instance_id)
            container.start()
            return self._info(container)

    def stop(self, instance_id: str, timeout: int = 10) -> InstanceInfo:
        with self.guard("stop"):
            container = self._get(instance_id)
            container.stop(timeout=timeout)
            return self._info(container)

    def restart(self, instance_id: str, timeout: int = 10) -> InstanceInfo:
        with self.guard("restart"):
            container = self._get(instance_id)
            container.restart(timeout=timeout)
            return self._info(container)

    def suspend(self, instance_id: str) -> InstanceInfo:
        with self.guard("suspend"):
            container = self._get(instance_id)
            container.pause()
            return self._info(container)

    def resume(self, instance_id: str) -> InstanceInfo:
        with self.guard("resume"):
            container = self._get(instance_id)
            container.unpause()
            return self._info(container)

    def remove(self, instance_id: str, force: bool = True) -> None:
        with self.guard("delete"):
            container = self._get(instance_id)
            container.remove(force=force, v=True)

    def inspect(self, instance_id: str) -> InstanceInfo:
        with self.guard("inspect"):
            return self._info(self._get(instance_id))

    # -- observability --------------------------------------------------- #
    def stats(self, instance_id: str) -> dict[str, Any]:
        with self.guard("stats"):
            container = self._get(instance_id)
            if not container.status == "running":
                return {
                    "cpu_pct": 0.0, "ram_used_mb": 0.0, "ram_total_mb": 0.0,
                    "disk_used_gb": 0.0, "disk_total_gb": 0.0, "net_rx_bytes": 0.0,
                    "net_tx_bytes": 0.0, "load1": 0.0, "procs": 0, "status": container.status,
                    "cpu_cores": 0, "block_read": 0, "block_write": 0,
                }
            raw = container.stats(stream=False)
            cpu_pct = self._cpu_percent(raw)
            memory = raw.get("memory_stats") or {}
            usage = float(memory.get("usage", 0)) - float((memory.get("stats") or {}).get("cache", 0))
            limit = float(memory.get("limit", 0)) or 1
            networks = raw.get("networks") or {}
            rx = sum(float(n.get("rx_bytes", 0)) for n in networks.values())
            tx = sum(float(n.get("tx_bytes", 0)) for n in networks.values())
            blk = raw.get("blkio_stats", {}).get("io_service_bytes_recursive") or []
            read_bytes = sum(float(b.get("value", 0)) for b in blk if b.get("op") == "read")
            write_bytes = sum(float(b.get("value", 0)) for b in blk if b.get("op") == "write")
            pids = (raw.get("pids_stats") or {}).get("current", 0)
            return {
                "cpu_pct": round(cpu_pct, 2),
                "ram_used_mb": round(max(usage, 0) / 1024 / 1024, 2),
                "ram_total_mb": round(limit / 1024 / 1024, 2),
                "ram_pct": round(max(usage, 0) / limit * 100, 2),
                "disk_used_gb": 0.0,
                "disk_total_gb": 0.0,
                "disk_pct": None,
                "net_rx_bytes": round(rx, 1),
                "net_tx_bytes": round(tx, 1),
                "block_read_bytes": round(read_bytes, 1),
                "block_write_bytes": round(write_bytes, 1),
                "procs": int(pids or 0),
                "load1": 0.0,
                "status": container.status,
            }

    @staticmethod
    def _cpu_percent(raw: dict[str, Any]) -> float:
        cpu = raw.get("cpu_stats") or {}
        pre = raw.get("precpu_stats") or {}
        cpu_total = (cpu.get("cpu_usage") or {}).get("total_usage", 0)
        pre_total = (pre.get("cpu_usage") or {}).get("total_usage", 0)
        system = cpu.get("system_cpu_usage") or 0
        pre_system = pre.get("system_cpu_usage") or 0
        cpu_delta = cpu_total - pre_total
        system_delta = system - pre_system
        online = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
        if system_delta > 0 and cpu_delta > 0:
            return (cpu_delta / system_delta) * online * 100
        return 0.0

    def logs(self, instance_id: str, tail: int = 200) -> str:
        with self.guard("logs"):
            container = self._get(instance_id)
            raw = container.logs(tail=int(tail), timestamps=True)
            return raw.decode("utf-8", "replace")

    # -- interaction ----------------------------------------------------- #
    def exec_command(self, instance_id: str, command: str | list[str], timeout: int = 30) -> ExecResult:
        with self.guard("exec"):
            container = self._get(instance_id)
            cmd = ["/bin/sh", "-c", command] if isinstance(command, str) else command
            result = container.exec_run(cmd, demux=False, tty=False, timeout=timeout)
            exit_code = int(result.exit_code or 0)
            output = (result.output or b"").decode("utf-8", "replace")
            return ExecResult(exit_code=exit_code, output=output)

    def console(self, instance_id: str, shell: str = "/bin/bash") -> ConsoleSession:
        self.require("console")
        container = self._get(instance_id)
        if container.status != "running":
            raise BackendError("Server is not running", code="not_running")
        try:
            exec_id = self.client.api.exec_create(
                container.id,
                cmd=[shell, "-l"],
                stdin=True,
                stdout=True,
                stderr=True,
                tty=True,
                environment={"TERM": "xterm-256color", "LANG": "C.UTF-8"},
            )["Id"]
            sock = self.client.api.exec_start(exec_id, tty=True, socket=True, stream=False)
        except Exception as exc:
            raise BackendError(f"Cannot open console: {exc}", code="console_failed") from exc
        return DockerConsole(self.client, exec_id, sock)

    def filesystem(self, instance_id: str):
        self.require("files")
        return DockerFileSystem(self, instance_id)

    # -- data protection -------------------------------------------------- #
    def snapshot(self, instance_id: str, name: str) -> dict[str, Any]:
        with self.guard("snapshot"):
            container = self._get(instance_id)
            reference = f"skvm/snap-{name.lower()}:latest"
            image = container.commit(repository=reference.split(":")[0], tag="latest", pause=True)
            size = float(image.attrs.get("Size", 0)) / 1024 / 1024
            return {"reference": reference, "image_id": image.id, "size_mb": round(size, 1)}

    def restore_snapshot(self, instance_id: str, reference: str) -> InstanceInfo:
        with self.guard("restore"):
            container = self._get(instance_id)
            attrs = container.attrs
            was_running = container.status == "running"
            container.remove(force=True, v=True)
            host_config = attrs.get("HostConfig", {})
            config = attrs.get("Config", {})
            new = self.client.containers.create(
                image=reference,
                name=config.get("Hostname") or None,
                hostname=config.get("Hostname"),
                labels=config.get("Labels") or {},
                environment=config.get("Env") or [],
                mem_limit=host_config.get("Memory"),
                nano_cpus=host_config.get("NanoCpus"),
                pids_limit=host_config.get("PidsLimit"),
                restart_policy=host_config.get("RestartPolicy") or {},
                ports=host_config.get("PortBindings") or {},
                command=config.get("Cmd"),
            )
            if was_running:
                new.start()
            return self._info(new)

    def delete_snapshot(self, reference: str) -> None:
        with self.guard("snapshot delete"):
            try:
                self.client.images.remove(reference, force=True)
            except ImageNotFound:
                pass

    def export_backup(self, instance_id: str, destination: str, progress=None) -> dict[str, Any]:
        """Stream a container filesystem tarball to ``destination``."""
        with self.guard("backup"):
            container = self._get(instance_id)
            stream, _stat = container.get_archive("/")
            size = 0
            with open(destination, "wb") as handle:
                for chunk in stream:
                    handle.write(chunk)
                    size += len(chunk)
                    if progress:
                        progress(f"{size / 1024 / 1024:.1f} MB written")
            return {"path": destination, "size_mb": round(size / 1024 / 1024, 1)}

    def reinstall(self, request: ProvisionRequest) -> InstanceInfo:
        if request.extra.get("instance_id"):
            self.remove(request.extra["instance_id"], force=True)
        return self.create(request)

    # -- maintenance ------------------------------------------------------ #
    def host_stats(self) -> dict[str, Any]:
        try:
            info = self.client.info()
            version = self.client.version()
            return {
                "engine_version": version.get("Version"),
                "api_version": version.get("ApiVersion"),
                "containers": info.get("Containers", 0),
                "containers_running": info.get("ContainersRunning", 0),
                "images": info.get("Images", 0),
                "cpus": info.get("NCPU", 0),
                "memory_total_mb": round(info.get("MemTotal", 0) / 1024 / 1024, 1),
                "driver": info.get("Driver"),
                "storage_driver": info.get("Driver"),
                "kernel": info.get("KernelVersion"),
                "os": info.get("OperatingSystem"),
                "architecture": info.get("Architecture"),
                "warnings": info.get("Warnings") or [],
            }
        except Exception as exc:
            return {"error": str(exc)[:200]}

    def prune(self) -> dict[str, Any]:
        try:
            images = self.client.images.prune(filters={"dangling": True})
            volumes = self.client.volumes.prune()
            return {"images": images.get("SpaceReclaimed", 0), "volumes": volumes.get("SpaceReclaimed", 0)}
        except Exception as exc:  # pragma: no cover
            return {"error": str(exc)[:200]}

    def list_managed(self) -> list[dict[str, Any]]:
        out = []
        try:
            for container in self.client.containers.list(all=True, filters={"label": f"{LABEL_PREFIX}.managed=true"}):
                out.append(self._info(container).__dict__ | {"name": container.name})
        except Exception:  # pragma: no cover
            pass
        return out

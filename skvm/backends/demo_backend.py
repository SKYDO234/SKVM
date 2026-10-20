"""Simulated backend.

A complete in-memory driver: it provisions instances, answers ``stats`` with
smooth synthetic telemetry, exposes a virtual filesystem for the file manager
and runs a tiny POSIX-looking shell for the web console.  It is what powers the
panel when no Docker daemon is reachable, on a laptop, and in the test suite —
so the whole UI stays fully explorable without a hypervisor.
"""

from __future__ import annotations

import json
import logging
import math
import os
import platform
import random
import secrets
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .base import (
    Backend,
    BackendError,
    ConsoleSession,
    ExecResult,
    InstanceInfo,
    ProvisionRequest,
)
from .images import IMAGE_CATALOG, image_info

log = logging.getLogger("skvm.backend.demo")

PROMPT = "\x1b[38;5;141mroot\x1b[0m@\x1b[38;5;81m{host}\x1b[0m:\x1b[38;5;220m{cwd}\x1b[0m# "
SSH_PORT_BASE = 22100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%b %d %H:%M")


# --------------------------------------------------------------------------- #
# Virtual filesystem
# --------------------------------------------------------------------------- #
@dataclass
class VNode:
    name: str
    kind: str = "dir"  # dir | file
    mode: int = 0o644
    content: str = ""
    mtime: str = field(default_factory=_stamp)
    children: dict[str, "VNode"] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.kind,
            "mode": self.mode,
            "content": self.content,
            "mtime": self.mtime,
            "children": {key: node.as_dict() for key, node in self.children.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VNode":
        node = cls(name=data.get("name", "/"), kind=data.get("kind", "dir"),
                   mode=int(data.get("mode") or 0o644), content=data.get("content", ""),
                   mtime=data.get("mtime") or _stamp())
        node.children = {key: cls.from_dict(value) for key, value in (data.get("children") or {}).items()}
        return node


def _seed_tree(hostname: str, image: str, password_hint: str) -> VNode:
    root = VNode("/", "dir", 0o755)
    for directory in ("bin", "boot", "dev", "etc", "home", "lib", "opt", "proc", "root", "run", "srv",
                      "sys", "tmp", "usr", "var", "etc/ssh", "etc/skvm", "etc/nginx", "var/log",
                      "var/www", "var/www/html", "var/lib", "opt/skvm", "root/.ssh", "home/deploy",
                      "usr/local", "usr/local/bin", "etc/systemd", "etc/systemd/system"):
        _mkdir(root, "/" + directory, 0o755)

    files = {
        "/etc/os-release": f'PRETTY_NAME="{image_info(image)["label"]}"\nNAME="Linux"\nID=skvm\nVERSION_ID="2.0"\n',
        "/etc/hostname": hostname + "\n",
        "/etc/hosts": f"127.0.0.1\tlocalhost\n127.0.1.1\t{hostname}\n::1\tlocalhost ip6-localhost\n",
        "/etc/passwd": "root:x:0:0:root:/root:/bin/bash\ndaemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\nnobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\ndeploy:x:1000:1000:Deploy:/home/deploy:/bin/bash\n",
        "/etc/ssh/sshd_config": "Port 22\nPermitRootLogin yes\nPasswordAuthentication yes\nUsePAM no\nX11Forwarding yes\nClientAliveInterval 60\nSubsystem sftp internal-sftp\n",
        "/etc/skvm/agent.conf": f"# SKVM instance metadata — managed by the panel\ninstance={hostname}\nimage={image}\nroot_password={password_hint}\n",
        "/opt/skvm/agent.conf": "heartbeat_interval=30\napi=http://panel.internal/agent\nmetrics=1\n",
        "/usr/local/bin/skvm": "#!/bin/sh\necho \"SKVM instance helper — try: skvm status\"\n",
        "/root/.bashrc": "export PS1='\\u@\\h:\\w# '\nalias ll='ls -alF'\nalias gs='git status'\nexport EDITOR=nano\n",
        "/root/README.txt": (
            "Welcome to your SKVM instance.\n\n"
            "  * SSH:      ssh root@<ip> -p <port>\n"
            "  * Console:  open the web console from the panel\n"
            "  * Metrics:  streaming at /servers/<uid>/metrics\n\n"
            "Installed: openssh-server, sudo, curl, wget, nano, htop, git.\n"
        ),
        "/etc/nginx/nginx.conf": "worker_processes auto;\nevents { worker_connections 1024; }\nhttp {\n    include /etc/nginx/mime.types;\n    server {\n        listen 80 default_server;\n        root /var/www/html;\n        index index.html;\n    }\n}\n",
        "/var/www/html/index.html": "<!doctype html>\n<title>It works!</title>\n<h1>Your SKVM instance is online</h1>\n",
        "/var/log/syslog": "\n".join(
            f"{_stamp()} {hostname} systemd[1]: Started session {1000 + index} of user root." for index in range(40)
        ) + "\n",
        "/var/log/auth.log": f"{_stamp()} {hostname} sshd[812]: Accepted password for root from 10.0.0.2 port 51922 ssh2\n",
        "/etc/systemd/system/skvm-agent.service": "[Unit]\nDescription=SKVM node agent\nAfter=network.target\n\n[Service]\nExecStart=/usr/local/bin/skvm-agent\nRestart=always\n\n[Install]\nWantedBy=multi-user.target\n",
    }
    for path, content in files.items():
        _write_file(root, path, content, 0o644)
    _mkdir(root, "/root/.ssh", 0o700)
    _write_file(root, "/root/.ssh/authorized_keys", "# your public keys are injected here by the panel\n", 0o600)
    return root


def _split(path: str) -> list[str]:
    return [part for part in path.split("/") if part not in ("", ".")]


def _walk(root: VNode, parts: Iterable[str]) -> VNode | None:
    node = root
    for part in parts:
        if part == "..":
            continue
        child = node.children.get(part)
        if child is None:
            return None
        node = child
    return node


def _resolve(root: VNode, path: str, cwd: str = "/root") -> tuple[VNode | None, str]:
    if not path:
        path = cwd
    if not path.startswith("/"):
        path = os.path.normpath(os.path.join(cwd, path))
    node = _walk(root, _split(path))
    return node, path


def _mkdir(root: VNode, path: str, mode: int = 0o755) -> None:
    parts = _split(path)
    node = root
    for part in parts:
        child = node.children.get(part)
        if child is None:
            child = VNode(part, "dir", mode)
            node.children[part] = child
        node = child


def _write_file(root: VNode, path: str, content: str, mode: int = 0o644) -> None:
    parts = _split(path)
    if not parts:
        return
    parent = _walk(root, parts[:-1]) or root
    parent.children[parts[-1]] = VNode(parts[-1], "file", mode, content)


def _size(node: VNode) -> int:
    if node.kind == "file":
        return len(node.content.encode())
    return sum(_size(child) for child in node.children.values())


def _mode_string(node: VNode) -> str:
    kind = "d" if node.kind == "dir" else "-"
    bits = node.mode
    out = kind
    for shift in (6, 3, 0):
        chunk = (bits >> shift) & 0b111
        out += "r" if chunk & 0b100 else "-"
        out += "w" if chunk & 0b010 else "-"
        out += "x" if chunk & 0b001 else "-"
    return out


# --------------------------------------------------------------------------- #
# Instance state
# --------------------------------------------------------------------------- #
@dataclass
class _Snapshot:
    reference: str
    name: str
    size_mb: float
    created: str
    tree: dict[str, Any]


@dataclass
class _Instance:
    id: str
    name: str
    image: str
    cpu: float
    ram_mb: int
    disk_gb: int
    status: str = "running"
    ip: str = ""
    ssh_port: int = SSH_PORT_BASE
    created: str = field(default_factory=_now)
    started_at: float = field(default_factory=time.time)
    cpu_seconds: float = 0.0
    net_rx_bytes: float = 0.0
    net_tx_bytes: float = 0.0
    tree: VNode = field(default_factory=lambda: VNode("/", "dir", 0o755))
    snapshots: dict[str, _Snapshot] = field(default_factory=dict)
    hostname: str = ""
    owner: str = ""
    seed: int = 0
    events: list[str] = field(default_factory=list)

    # deterministic "personality" per instance
    @property
    def phase(self) -> float:
        return (self.seed % 360) / 57.3

    def log(self, message: str) -> None:
        self.events.insert(0, f"{_now()} {message}")
        del self.events[60:]


_FLEET: dict[str, _Instance] = {}
_STATE_FILE = "demo-fleet.json"


def _state_path() -> str | None:
    try:
        from flask import current_app

        root = current_app.config.get("INSTANCE_DIR")
        if root:
            return os.path.join(str(root), _STATE_FILE)
    except Exception:  # pragma: no cover - outside app context
        pass
    return None


def _persist() -> None:
    path = _state_path()
    if not path:
        return
    try:
        payload = {
            key: {
                "id": item.id, "name": item.name, "image": item.image, "cpu": item.cpu,
                "ram_mb": item.ram_mb, "disk_gb": item.disk_gb, "status": item.status,
                "ip": item.ip, "ssh_port": item.ssh_port, "created": item.created,
                "hostname": item.hostname, "owner": item.owner, "seed": item.seed,
                "cpu_seconds": item.cpu_seconds, "net_rx_bytes": item.net_rx_bytes,
                "net_tx_bytes": item.net_tx_bytes, "tree": item.tree.as_dict(),
                "events": item.events,
            }
            for key, item in _FLEET.items()
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except Exception as exc:  # pragma: no cover - best effort cache
        log.debug("demo persist failed: %s", exc)


def _load() -> None:
    path = _state_path()
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        for key, data in payload.items():
            instance = _Instance(
                id=data["id"], name=data["name"], image=data["image"], cpu=float(data["cpu"]),
                ram_mb=int(data["ram_mb"]), disk_gb=int(data["disk_gb"]), status=data.get("status", "stopped"),
                ip=data.get("ip", ""), ssh_port=int(data.get("ssh_port") or SSH_PORT_BASE),
                created=data.get("created") or _now(), hostname=data.get("hostname", ""),
                owner=data.get("owner", ""), seed=int(data.get("seed") or 0),
                cpu_seconds=float(data.get("cpu_seconds") or 0),
                net_rx_bytes=float(data.get("net_rx_bytes") or 0),
                net_tx_bytes=float(data.get("net_tx_bytes") or 0),
                events=list(data.get("events") or []),
                tree=VNode.from_dict(data["tree"]) if data.get("tree") else VNode("/", "dir", 0o755),
            )
            _FLEET[key] = instance
        log.info("demo fleet restored (%d instance(s))", len(_FLEET))
    except Exception as exc:  # pragma: no cover
        log.warning("demo state ignored: %s", exc)


def _info(instance: _Instance) -> InstanceInfo:
    return InstanceInfo(
        id=instance.id,
        status=instance.status,
        ip=instance.ip,
        ssh_port=instance.ssh_port,
        image=instance.image,
        created=instance.created,
        detail={"name": instance.name, "simulated": True, "hostname": instance.hostname},
    )


# --------------------------------------------------------------------------- #
# Synthetic telemetry
# --------------------------------------------------------------------------- #
def _telemetry(instance: _Instance) -> dict[str, Any]:
    """Smooth, deterministic pseudo-telemetry (a slow sine + harmonics)."""
    now = time.time()
    phase = instance.phase
    running = instance.status == "running"
    spin = 1.0 if running else 0.0

    cpu = 9 + 7 * math.sin(now / 47 + phase) + 4 * math.sin(now / 6.5 + phase * 3) + 2.5 * math.sin(now / 1.7)
    cpu = max(0.4, min(98.0, cpu * spin))
    ram_pct = 32 + 11 * math.sin(now / 95 + phase * 2) + 3 * math.sin(now / 13 + phase)
    ram_pct = max(4.0, min(96.0, ram_pct if running else 3.0))
    disk_used = min(instance.disk_gb * 0.97, 3.2 + instance.disk_gb * 0.11 + instance.cpu_seconds / 4000 * 0.05)
    rx = (140 + 120 * math.sin(now / 23 + phase) + 60 * math.sin(now / 3.1)) * spin
    tx = (90 + 80 * math.sin(now / 31 + phase * 1.4) + 40 * math.sin(now / 4.7)) * spin
    rx, tx = max(0.0, rx), max(0.0, tx)

    instance.cpu_seconds += cpu / 100 * 1.0
    instance.net_rx_bytes += max(0.0, rx) * 1024
    instance.net_tx_bytes += max(0.0, tx) * 1024

    return {
        "cpu_pct": round(cpu, 2),
        "ram_used_mb": round(instance.ram_mb * ram_pct / 100, 1),
        "ram_total_mb": float(instance.ram_mb),
        "ram_pct": round(ram_pct, 2),
        "disk_used_gb": round(disk_used, 2),
        "disk_total_gb": float(instance.disk_gb),
        "disk_pct": round(disk_used / max(instance.disk_gb, 1) * 100, 2),
        "net_rx_kbps": round(rx, 2),
        "net_tx_kbps": round(tx, 2),
        "net_rx_bytes": round(instance.net_rx_bytes, 0),
        "net_tx_bytes": round(instance.net_tx_bytes, 0),
        "load1": round(cpu / 100 * max(instance.cpu, 1) * 1.4, 2),
        "procs": 12 + int(cpu / 4),
        "uptime_s": int(time.time() - instance.started_at) if running else 0,
        "status": instance.status,
        "simulated": True,
    }


def _boot_log(instance: _Instance) -> str:
    host = instance.hostname or instance.name
    lines = [
        f"[    0.000000] Linux version 6.8.0-skvm (root@panel) (gcc 13.2.0) #1 SMP PREEMPT_DYNAMIC",
        f"[    0.412318] Command line: BOOT_IMAGE=/vmlinuz root=/dev/overlay rw quiet skvm.instance={instance.id}",
        "[    0.918447] systemd[1]: System time before build time, advancing clock.",
        "[    1.104322] systemd[1]: Detected virtualization skvm-container.",
        f"[    1.441900] systemd[1]: Set hostname to <{host}>.",
        "[    1.882714] systemd[1]: Started Journal Service.",
        f"[    2.190553] systemd[1]: Started SKVM node agent (heartbeat ok).",
        f"[    2.441002] skvm-bootstrap: applying image {instance.image} ({instance.cpu:g} vCPU / {instance.ram_mb} MB)",
        "[    2.712889] sshd[412]: Server listening on 0.0.0.0 port 22.",
        "[    2.900114] systemd[1]: Reached target Multi-User System.",
        f"[    3.010774] skvm: instance ready — {instance.id}",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Shell
# --------------------------------------------------------------------------- #
def _shell_eval(instance: _Instance, command: str, cwd: str) -> tuple[str, str]:
    """Return (output, new_cwd) for one command line."""
    command = command.strip()
    if not command:
        return "", cwd
    host = instance.hostname or instance.name
    telemetry = _telemetry(instance)
    for separator in ("&&", ";"):
        if separator in command:
            first, _, rest = command.partition(separator)
            out, cwd = _shell_eval(instance, first, cwd)
            out2, cwd = _shell_eval(instance, rest, cwd)
            return (out + out2).rstrip("\n"), cwd
    if "|" in command:
        left, _, right = command.partition("|")
        text, cwd = _shell_eval(instance, left, cwd)
        target = right.strip().split()[0] if right.strip() else ""
        if target in ("grep", "head", "tail", "wc", "sort", "uniq", "cat"):
            sub = " ".join(right.strip().split()[1:])
            if target == "grep":
                needle = sub.strip("'\"")
                return "\n".join(line for line in text.splitlines() if needle and needle in line), cwd
            if target == "head":
                count = int(sub.split("-n")[-1]) if "-n" in sub else 10
                return "\n".join(text.splitlines()[:count]), cwd
            if target == "tail":
                count = int(sub.split("-n")[-1]) if "-n" in sub else 10
                return "\n".join(text.splitlines()[-count:]), cwd
            if target == "wc":
                return f"{len(text.splitlines())} {len(text.split())} {len(text)}", cwd
            return text, cwd
        return f"bash: {target}: command not found", cwd

    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return "", cwd
    verb, args = parts[0], parts[1:]

    def target_path(arg: str) -> str:
        return arg if arg.startswith("/") else os.path.normpath(os.path.join(cwd, arg))

    if verb in ("exit", "logout"):
        return "__EXIT__", cwd
    if verb == "clear":
        return "\x1b[2J\x1b[H", cwd
    if verb == "pwd":
        return cwd, cwd
    if verb == "whoami":
        return "root", cwd
    if verb == "id":
        return "uid=0(root) gid=0(root) groups=0(root)", cwd
    if verb == "hostname":
        return host, cwd
    if verb == "date":
        return datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S UTC %Y"), cwd
    if verb == "uname":
        if "-a" in args:
            return f"Linux {host} 6.8.0-skvm #1 SMP PREEMPT_DYNAMIC x86_64 GNU/Linux", cwd
        return "Linux", cwd
    if verb in ("cd", "chdir"):
        destination = target_path(args[0]) if args else "/root"
        node, resolved = _resolve(instance.tree, destination, cwd)
        if node is None or node.kind != "dir":
            return f"bash: cd: {destination}: No such file or directory", cwd
        return "", resolved
    if verb in ("ls", "ll", "dir"):
        paths = [arg for arg in args if not arg.startswith("-")]
        long_form = verb == "ll" or any("l" in arg for arg in args if arg.startswith("-"))
        node, resolved = _resolve(instance.tree, paths[0] if paths else cwd, cwd)
        if node is None:
            return f"ls: cannot access '{paths[0] if paths else cwd}': No such file or directory", cwd
        if node.kind == "file":
            return node.name, cwd
        rows = []
        for name in sorted(node.children):
            child = node.children[name]
            if long_form:
                rows.append(f"{_mode_string(child)} 1 root root {_size(child):>8} {child.mtime} {name}")
            else:
                rows.append(name + ("/" if child.kind == "dir" else ""))
        return "\n".join(rows), cwd
    if verb == "cat":
        if not args:
            return "cat: missing operand", cwd
        out = []
        for arg in args:
            node, resolved = _resolve(instance.tree, arg, cwd)
            if node is None:
                out.append(f"cat: {arg}: No such file or directory")
            elif node.kind == "dir":
                out.append(f"cat: {arg}: Is a directory")
            else:
                out.append(node.content)
        return "".join(out).rstrip("\n"), cwd
    if verb in ("head", "tail"):
        count = 10
        files = []
        index = 0
        while index < len(args):
            if args[index] == "-n":
                count = int(args[index + 1]); index += 2; continue
            if args[index].startswith("-") and args[index][1:].isdigit():
                count = int(args[index][1:]); index += 1; continue
            files.append(args[index]); index += 1
        if not files:
            return f"{verb}: missing operand", cwd
        node, _ = _resolve(instance.tree, files[0], cwd)
        if node is None or node.kind != "file":
            return f"{verb}: cannot open '{files[0]}' for reading", cwd
        lines = node.content.rstrip("\n").splitlines()
        return "\n".join(lines[:count] if verb == "head" else lines[-count:]), cwd
    if verb == "echo":
        text = " ".join(args)
        redirect = None
        if ">" in args:
            position = args.index(">")
            text = " ".join(args[:position])
            redirect = args[position + 1] if len(args) > position + 1 else None
        if redirect:
            path = target_path(redirect)
            parent = os.path.dirname(path) or "/"
            if _resolve(instance.tree, parent, cwd)[0] is None:
                return f"bash: {redirect}: No such file or directory", cwd
            _write_file(instance.tree, path, text + "\n")
            return "", cwd
        return text.replace("\\n", "\n"), cwd
    if verb in ("mkdir",):
        if not args:
            return "mkdir: missing operand", cwd
        _mkdir(instance.tree, target_path(args[-1]), 0o755)
        return "", cwd
    if verb in ("touch",):
        if args:
            _write_file(instance.tree, target_path(args[-1]), "")
        return "", cwd
    if verb in ("rm", "rmdir"):
        if not args:
            return "rm: missing operand", cwd
        path = target_path(args[-1])
        node, resolved = _resolve(instance.tree, path, cwd)
        if node is None:
            return f"rm: cannot remove '{args[-1]}': No such file or directory", cwd
        parent = _walk(instance.tree, _split(resolved)[:-1]) or instance.tree
        parent.children.pop(_split(resolved)[-1], None)
        return "", cwd
    if verb in ("free",):
        total = instance.ram_mb
        return (
            f"               total        used        free      shared  buff/cache   available\n"
            f"Mem:        {total:>9} {telemetry['ram_used_mb']:>11.0f} {total - telemetry['ram_used_mb']:>11.0f}"
            f"        1024 {int(total * 0.18):>11} {int(total * 0.62):>11}"
        ), cwd
    if verb == "df":
        used = telemetry["disk_used_gb"]
        return (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            f"/dev/overlay    {instance.disk_gb}G  {used:.1f}G  {max(0.0, instance.disk_gb - used):.1f}G "
            f"{int(used / max(instance.disk_gb, 1) * 100):>3}% /\n"
            "tmpfs            64M     0   64M   0% /dev/shm"
        ), cwd
    if verb == "uptime":
        seconds = max(1, int(time.time() - instance.started_at))
        return (
            f" {datetime.now(timezone.utc).strftime('%H:%M:%S')} up {seconds // 3600}:{(seconds % 3600) // 60:02d},"
            f"  1 user,  load average: {telemetry['load1']}, {telemetry['load1'] * 0.8:.2f}, {telemetry['load1'] * 0.6:.2f}"
        ), cwd
    if verb in ("ps", "top", "htop"):
        rows = [
            "  PID TTY          TIME CMD",
            "    1 ?        00:00:01 systemd",
            "  412 ?        00:00:00 sshd",
            "  518 pts/0    00:00:00 bash",
            f"{600 + int(telemetry['cpu_pct'])} ?        00:00:0{max(1, int(telemetry['cpu_pct'] / 40))} nginx: worker process",
            f"{700 + int(telemetry['ram_pct'])} ?        00:00:01 node /srv/app/index.js",
        ]
        return "\n".join(rows), cwd
    if verb == "systemctl":
        unit = args[-1] if args else "skvm-agent"
        if "status" in args:
            return (
                f"● {unit}.service - {unit}\n"
                f"     Loaded: loaded (/etc/systemd/system/{unit}.service; enabled)\n"
                f"     Active: active (running) since {_stamp()}\n"
                f"   Main PID: 812 ({unit.split('.')[0]})\n"
                f"      Tasks: 3 (limit: 4915)\n"
                f"     Memory: {telemetry['ram_used_mb'] * 0.1:.1f}M"
            ), cwd
        return f"==== AUTHENTICATING FOR org.freedesktop.systemd1.manage-units ====\nOperation completed for {unit}", cwd
    if verb in ("sh", "bash", "sudo"):
        if verb == "sudo":
            return _shell_eval(instance, " ".join(args), cwd)
        return "", cwd
    if verb in ("apt", "apt-get", "dnf", "apk", "pacman"):
        packages = [arg for arg in args if not arg.startswith("-") and arg not in ("install", "update", "upgrade")]
        if "install" in args and packages:
            return (
                f"Reading package lists... Done\nBuilding dependency tree... Done\n"
                f"The following NEW packages will be installed:\n  {' '.join(packages)}\n"
                "0 upgraded, 1 newly installed, 0 to remove and 0 not upgraded.\n"
                f"Setting up {' '.join(packages)} ... done"
            ), cwd
        return "Reading package lists... Done\nBuilding dependency tree... Done\nAll packages are up to date.", cwd
    if verb in ("ip", "ifconfig"):
        return (
            f"1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n    inet 127.0.0.1/8 scope host lo\n"
            f"2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n    inet {instance.ip}/24 brd {instance.ip.rsplit('.', 1)[0]}.255 scope global eth0"
        ), cwd
    if verb in ("ping", "curl", "wget"):
        endpoint = args[-1] if args else "https://example.com"
        if verb == "ping":
            return (
                f"PING {endpoint} ({endpoint}) 56(84) bytes of data.\n"
                f"64 bytes from {endpoint}: icmp_seq=1 ttl=57 time=11.4 ms\n"
                f"64 bytes from {endpoint}: icmp_seq=2 ttl=57 time=10.9 ms\n\n"
                f"--- {endpoint} ping statistics ---\n2 packets transmitted, 2 received, 0% packet loss"
            ), cwd
        return f"HTTP/2 200\ncontent-type: text/html\n\n<!doctype html><title>OK</title>", cwd
    if verb in ("env", "printenv"):
        return (
            f"HOSTNAME={host}\nHOME=/root\nPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
            f"SKVM_INSTANCE={instance.id}\nSKVM_IMAGE={instance.image}\nTERM=xterm-256color"
        ), cwd
    if verb == "neofetch":
        return (
            "      ▄▄▄▄▄▄▄▄        root@" + host + "\n"
            "    ▄██████████▄      OS: " + image_info(instance.image)["label"] + "\n"
            "   ████▀    ▀████     Kernel: 6.8.0-skvm\n"
            "  ████   ▄▄   ████    Uptime: " + f"{int((time.time() - instance.started_at) // 60)} mins\n"
            "  ████   ▀▀   ████    Shell: bash 5.2.21\n"
            "   ████▄    ▄████     CPU: " + f"{instance.cpu:g} vCPU (skvm-container)\n"
            "    ▀██████████▀      Memory: " + f"{telemetry['ram_used_mb']:.0f}MB / {instance.ram_mb}MB\n"
            "      ▀▀▀▀▀▀▀▀        Disk: " + f"{telemetry['disk_used_gb']:.1f}G / {instance.disk_gb}G"
        ), cwd
    if verb == "skvm":
        return (
            f"SKVM instance helper\n  instance : {instance.id}\n  status   : {instance.status}\n"
            f"  image    : {instance.image}\n  ssh      : root@{instance.ip} -p {instance.ssh_port}"
        ), cwd
    if verb == "history":
        return "\n".join(instance.events[:10]) or "no history", cwd
    if verb in ("man", "info"):
        return f"No manual entry for {args[0] if args else verb}", cwd
    if verb == "true":
        return "", cwd
    if verb == "false":
        return "", cwd
    return f"bash: {verb}: command not found", cwd


# --------------------------------------------------------------------------- #
# Filesystem facade (mirrors DockerFileSystem)
# --------------------------------------------------------------------------- #
class DemoFileSystem:
    def __init__(self, instance: _Instance) -> None:
        self.instance = instance

    def _entry(self, node: VNode, path: str) -> dict[str, Any]:
        return {
            "name": node.name if node.name != "/" else "/",
            "path": path,
            "type": "dir" if node.kind == "dir" else "file",
            "size": _size(node),
            "mode": _mode_string(node),
            "modified": node.mtime,
            "owner": "root",
        }

    def list_dir(self, path: str = "/") -> list[dict[str, Any]]:
        node, resolved = _resolve(self.instance.tree, path)
        if node is None:
            raise BackendError(f"no such directory: {path}", code="not_found")
        if node.kind != "dir":
            raise BackendError(f"not a directory: {path}", code="not_a_directory")
        base = resolved.rstrip("/") or "/"
        out = []
        for name in sorted(node.children, key=str.lower):
            child = node.children[name]
            child_path = f"{base.rstrip('/')}/{name}" if base != "/" else f"/{name}"
            out.append(self._entry(child, child_path))
        return out

    def stat(self, path: str) -> dict[str, Any]:
        node, resolved = _resolve(self.instance.tree, path)
        if node is None:
            raise BackendError(f"no such file or directory: {path}", code="not_found")
        entry = self._entry(node, resolved)
        entry["children"] = len(node.children) if node.kind == "dir" else 0
        return entry

    def read(self, path: str, max_bytes: int = 2 * 1024 * 1024) -> bytes:
        node, _ = _resolve(self.instance.tree, path)
        if node is None:
            raise BackendError(f"no such file: {path}", code="not_found")
        if node.kind == "dir":
            raise BackendError(f"is a directory: {path}", code="is_a_directory")
        return node.content.encode()[:max_bytes]

    def tail(self, path: str, lines: int = 200) -> str:
        node, _ = _resolve(self.instance.tree, path)
        if node is None or node.kind != "file":
            raise BackendError(f"no such file: {path}", code="not_found")
        return "\n".join(node.content.splitlines()[-lines:])

    def write(self, path: str, data: bytes, mode: int = 0o644) -> dict[str, Any]:
        parent = os.path.dirname(path) or "/"
        if _resolve(self.instance.tree, parent)[0] is None:
            raise BackendError(f"no such directory: {parent}", code="not_found")
        _write_file(self.instance.tree, path, data.decode("utf-8", "replace"), mode)
        _persist()
        return self.stat(path)

    def mkdir(self, path: str, mode: int = 0o755) -> None:
        if _resolve(self.instance.tree, path)[0] is not None:
            raise BackendError(f"already exists: {path}", code="exists")
        _mkdir(self.instance.tree, path, mode)
        _persist()

    def delete(self, path: str, recursive: bool = False) -> None:
        node, resolved = _resolve(self.instance.tree, path)
        if node is None:
            raise BackendError(f"no such file or directory: {path}", code="not_found")
        if node.kind == "dir" and node.children and not recursive:
            raise BackendError(f"directory not empty: {path}", code="not_empty")
        parent = _walk(self.instance.tree, _split(resolved)[:-1]) or self.instance.tree
        parent.children.pop(_split(resolved)[-1], None)
        _persist()

    def rename(self, source: str, destination: str) -> None:
        node, resolved = _resolve(self.instance.tree, source)
        if node is None:
            raise BackendError(f"no such file or directory: {source}", code="not_found")
        parent = _walk(self.instance.tree, _split(resolved)[:-1]) or self.instance.tree
        parent.children.pop(_split(resolved)[-1], None)
        target_dir = os.path.dirname(destination) or "/"
        if _resolve(self.instance.tree, target_dir)[0] is None:
            raise BackendError(f"no such directory: {target_dir}", code="not_found")
        _write_file(self.instance.tree, source, node.content) if node.kind == "file" else None
        if node.kind == "dir":
            _mkdir(self.instance.tree, destination, node.mode)
            restored = _resolve(self.instance.tree, destination)[0]
            if restored is not None:
                restored.children = node.children
        _persist()

    def copy(self, source: str, destination: str) -> None:
        node, _ = _resolve(self.instance.tree, source)
        if node is None:
            raise BackendError(f"no such file or directory: {source}", code="not_found")
        if node.kind == "dir":
            _mkdir(self.instance.tree, destination, node.mode)
            target = _resolve(self.instance.tree, destination)[0]
            if target is not None:
                target.children = {key: VNode.from_dict(value.as_dict()) for key, value in node.children.items()}
        else:
            _write_file(self.instance.tree, destination, node.content, node.mode)
        _persist()

    def chmod(self, path: str, mode: str) -> None:
        node, _ = _resolve(self.instance.tree, path)
        if node is None:
            raise BackendError(f"no such file or directory: {path}", code="not_found")
        try:
            node.mode = int(str(mode)[-3:], 8)
        except ValueError as exc:
            raise BackendError(f"invalid mode: {mode}", code="bad_mode") from exc
        _persist()

    def usage(self, path: str = "/") -> dict[str, Any]:
        node, _ = _resolve(self.instance.tree, path)
        if node is None:
            raise BackendError(f"no such directory: {path}", code="not_found")
        used = _size(node)
        total = self.instance.disk_gb * 1024 ** 3
        return {"path": path, "used_bytes": used, "total_bytes": total,
                "percent": round(used / total * 100, 3) if total else 0}

    def search(self, root: str, pattern: str, limit: int = 200) -> list[dict[str, Any]]:
        node, resolved = _resolve(self.instance.tree, root)
        if node is None:
            raise BackendError(f"no such directory: {root}", code="not_found")
        needle = (pattern or "").lower().strip("*")
        found: list[dict[str, Any]] = []

        def walk(current: VNode, current_path: str) -> None:
            if len(found) >= limit:
                return
            for name, child in sorted(current.children.items()):
                child_path = f"{current_path.rstrip('/')}/{name}"
                if not needle or needle in name.lower():
                    found.append(self._entry(child, child_path))
                if child.kind == "dir":
                    walk(child, child_path)

        walk(node, resolved if resolved != "/" else "")
        return found[:limit]


# --------------------------------------------------------------------------- #
# Console session
# --------------------------------------------------------------------------- #
class DemoConsole(ConsoleSession):
    """A line-based miniature shell with ANSI colours and history."""

    def __init__(self, instance: _Instance) -> None:
        self.instance = instance
        self.cwd = "/root"
        self.buffer: list[str] = []
        self._line: str = ""
        self.history: list[str] = []
        self.history_index = 0
        self.closed = False
        self._prompt()

    # -- helpers ---------------------------------------------------------- #
    def _prompt(self) -> None:
        host = self.instance.hostname or self.instance.name
        self.buffer.append(PROMPT.format(host=host, cwd=self.cwd))

    def _print(self, text: str) -> None:
        """Append complete lines (CRLF terminated) to the outbound buffer."""
        if not text:
            return
        for line in str(text).split("\n"):
            self.buffer.append(line + "\r\n")

    def _raw(self, text: str) -> None:
        self.buffer.append(text)

    # -- ConsoleSession --------------------------------------------------- #
    def write(self, data: bytes) -> None:
        if self.closed:
            return
        for char in data.decode("utf-8", "replace"):
            if char in ("\r", "\n"):
                line, self._line = self._line, ""
                self._raw("\r\n")
                if line.strip():
                    self.history.append(line.strip())
                output, self.cwd = _shell_eval(self.instance, line, self.cwd)
                if output == "__EXIT__":
                    self._print("logout")
                    self.closed = True
                    return
                self._print(output)
                self._prompt()
            elif char == "\x7f":  # backspace
                if self._line:
                    self._line = self._line[:-1]
                    self._raw("\b \b")
            elif char == "\x03":  # ctrl-c
                self._line = ""
                self._print("^C")
                self._prompt()
            elif char == "\x04":  # ctrl-d
                if self._line:
                    continue
                self._print("logout")
                self.closed = True
                return
            elif char == "\x1b" or char < " ":  # escape sequences / control chars
                continue
            else:
                self._line += char
                self._raw(char)

    def read(self, timeout: float = 0.2) -> bytes | None:
        if not self.buffer:
            return None
        chunk = "".join(self.buffer)
        self.buffer = []
        return chunk.encode()

    def resize(self, rows: int, cols: int) -> None:  # pragma: no cover - cosmetic
        return None

    def close(self) -> None:
        self.closed = True

    @property
    def alive(self) -> bool:
        return not self.closed


# --------------------------------------------------------------------------- #
# Backend
# --------------------------------------------------------------------------- #
class DemoBackend(Backend):
    name = "demo"
    label = "Demo (simulated)"
    supports = {"create", "power", "console", "stats", "logs", "snapshots", "files", "reinstall", "exec", "suspend",
                "backups", "host_stats"}

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self._loaded = False
        self._port_floor = SSH_PORT_BASE
        self.rng = random.Random(1337)

    # -- lifecycle -------------------------------------------------------- #
    def _ensure_loaded(self) -> None:
        if not self._loaded:
            _load()
            self._loaded = True

    @classmethod
    def available(cls) -> tuple[bool, str]:
        return True, "simulated fleet"

    def catalog(self) -> list[dict[str, Any]]:
        out = []
        for key, meta in IMAGE_CATALOG.items():
            if meta.get("kvm_only"):
                continue
            out.append({
                "id": key,
                "label": meta["label"],
                "accent": meta.get("accent"),
                "base": meta.get("docker_image"),
                "prebuilt": False,
                "mode": "simulated",
            })
        return out

    def create(self, request: ProvisionRequest) -> InstanceInfo:
        self._ensure_loaded()
        instance_id = request.uid or f"demo-{secrets.token_hex(5)}"
        ip = f"10.{self.rng.randint(20, 60)}.{self.rng.randint(0, 255)}.{self.rng.randint(2, 250)}"
        port = SSH_PORT_BASE + len(_FLEET)
        hostname = request.hostname or f"{request.name}.skvm.local"
        instance = _Instance(
            id=instance_id,
            name=request.name,
            image=request.os_image,
            cpu=float(request.cpu),
            ram_mb=int(request.ram_mb),
            disk_gb=int(request.disk_gb),
            status="running",
            ip=ip,
            ssh_port=port,
            hostname=hostname,
            owner=request.owner,
            seed=abs(hash((request.name, hostname))) % 10_000,
            tree=_seed_tree(hostname, request.os_image, "•••"),
        )
        if request.ssh_keys:
            _write_file(instance.tree, "/root/.ssh/authorized_keys",
                        "\n".join(key.strip() for key in request.ssh_keys) + "\n", 0o600)
        instance.log(f"instance created ({request.os_image}, {request.cpu:g} vCPU, {request.ram_mb} MB)")
        _FLEET[instance_id] = instance
        _persist()
        # Prime the metrics ring buffer so the first dashboard paint has history.
        self.stats(instance_id)
        return _info(instance)

    def _get(self, instance_id: str) -> _Instance:
        self._ensure_loaded()
        instance = _FLEET.get(instance_id)
        if instance is None:
            raise BackendError(f"demo instance {instance_id} is not tracked", code="not_found")
        return instance

    def start(self, instance_id: str) -> InstanceInfo:
        instance = self._get(instance_id)
        instance.status = "running"
        instance.started_at = time.time()
        instance.log("power on")
        _persist()
        return _info(instance)

    def stop(self, instance_id: str, timeout: int = 10) -> InstanceInfo:
        instance = self._get(instance_id)
        instance.status = "exited"
        instance.log("power off")
        _persist()
        return _info(instance)

    def restart(self, instance_id: str, timeout: int = 10) -> InstanceInfo:
        instance = self._get(instance_id)
        instance.status = "running"
        instance.started_at = time.time()
        instance.log("restarted")
        _persist()
        return _info(instance)

    def suspend(self, instance_id: str) -> InstanceInfo:
        instance = self._get(instance_id)
        instance.status = "paused"
        instance.log("suspended")
        _persist()
        return _info(instance)

    def resume(self, instance_id: str) -> InstanceInfo:
        instance = self._get(instance_id)
        instance.status = "running"
        instance.started_at = time.time()
        instance.log("resumed")
        _persist()
        return _info(instance)

    def remove(self, instance_id: str, force: bool = True) -> None:
        self._get(instance_id)
        _FLEET.pop(instance_id, None)
        _persist()

    def inspect(self, instance_id: str) -> InstanceInfo:
        return _info(self._get(instance_id))

    def reinstall(self, request: ProvisionRequest) -> InstanceInfo:
        instance = self._get(request.uid or "")
        instance.image = request.os_image
        instance.tree = _seed_tree(instance.hostname or instance.name, request.os_image, "•••")
        if request.ssh_keys:
            _write_file(instance.tree, "/root/.ssh/authorized_keys",
                        "\n".join(request.ssh_keys) + "\n", 0o600)
        instance.status = "running"
        instance.started_at = time.time()
        instance.log(f"reinstalled with {request.os_image}")
        _persist()
        return _info(instance)

    # -- observability ---------------------------------------------------- #
    def stats(self, instance_id: str) -> dict[str, Any]:
        return _telemetry(self._get(instance_id))

    def logs(self, instance_id: str, tail: int = 200) -> str:
        instance = self._get(instance_id)
        lines = _boot_log(instance).splitlines() + list(reversed(instance.events))
        return "\n".join(lines[-tail:]) + "\n"

    def exec_command(self, instance_id: str, command: str | list[str], timeout: int = 30) -> ExecResult:
        instance = self._get(instance_id)
        if isinstance(command, (list, tuple)):
            line = " ".join(shlex.quote(str(part)) for part in command)
            if len(command) >= 3 and str(command[0]).endswith(("sh", "bash")) and command[1] == "-c":
                line = str(command[2])
        else:
            line = str(command)
        if instance.status != "running":
            return ExecResult(exit_code=1, output=f"container is {instance.status}")
        output, _ = _shell_eval(instance, line, "/root")
        exit_code = 127 if "command not found" in output else 0
        return ExecResult(exit_code=exit_code, output=output)

    def console(self, instance_id: str, shell: str = "/bin/bash") -> ConsoleSession:
        return DemoConsole(self._get(instance_id))

    def filesystem(self, instance_id: str) -> DemoFileSystem:
        return DemoFileSystem(self._get(instance_id))

    # -- data protection -------------------------------------------------- #
    def snapshot(self, instance_id: str, name: str) -> dict[str, Any]:
        instance = self._get(instance_id)
        reference = f"demo-snap-{secrets.token_hex(6)}"
        size_mb = round(_size(instance.tree) / 1024 ** 2 + 220, 1)
        instance.snapshots[reference] = _Snapshot(reference, name, size_mb, _now(), instance.tree.as_dict())
        instance.log(f"snapshot {name} created")
        _persist()
        return {"reference": reference, "size_mb": size_mb, "created": _now(), "name": name}

    def restore_snapshot(self, instance_id: str, reference: str) -> InstanceInfo:
        instance = self._get(instance_id)
        snapshot = instance.snapshots.get(reference)
        if snapshot is None:
            raise BackendError(f"snapshot {reference} is not available on this node", code="not_found")
        instance.tree = VNode.from_dict(snapshot.tree)
        instance.log(f"restored {snapshot.name}")
        _persist()
        return _info(instance)

    def delete_snapshot(self, reference: str) -> None:
        self._ensure_loaded()
        for instance in _FLEET.values():
            if instance.snapshots.pop(reference, None) is not None:
                instance.log(f"snapshot {reference} removed")
        _persist()

    def export_backup(self, instance_id: str, destination: str, progress=None) -> dict[str, Any]:
        instance = self._get(instance_id)
        payload = {
            "instance": instance.id,
            "created": _now(),
            "files": len(instance.tree.as_dict().__str__()),
            "tree": instance.tree.as_dict(),
        }
        blob = json.dumps(payload, indent=1).encode()
        try:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, "wb") as handle:
                handle.write(blob)
        except OSError as exc:  # pragma: no cover - disk issues
            raise BackendError(f"cannot write backup: {exc}", code="io_error") from exc
        if progress:
            progress("simulated archive written")
        return {"path": destination, "size_mb": round(len(blob) / 1024 ** 2, 3), "checksum_source": blob}

    # -- maintenance ------------------------------------------------------ #
    def host_stats(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "driver": "demo",
            "platform": platform.platform(),
            "python": platform.python_version(),
            "instances": len(_FLEET),
            "simulated": True,
        }
        try:
            import psutil

            memory = psutil.virtual_memory()
            data.update({
                "cpu_count": psutil.cpu_count(logical=True),
                "cpu_pct": psutil.cpu_percent(interval=None),
                "memory_total_mb": round(memory.total / 1024 ** 2, 1),
                "memory_used_mb": round((memory.total - memory.available) / 1024 ** 2, 1),
                "disk_total_gb": round(psutil.disk_usage("/").total / 1024 ** 3, 1),
                "disk_used_gb": round(psutil.disk_usage("/").used / 1024 ** 3, 1),
                "load1": round(psutil.getloadavg()[0], 2),
            })
        except Exception as exc:  # pragma: no cover - psutil optional
            log.debug("psutil unavailable: %s", exc)
        return data

    def prune(self) -> dict[str, Any]:
        return {"removed": 0, "simulated": True}


__all__ = ["DemoBackend", "DemoConsole", "DemoFileSystem"]

"""OS image catalog.

Each entry describes a bootable "VPS template":  the stock Docker base image it
derives from, the family (so the provisioning script knows which package manager
to use) and where the hand-rolled image Dockerfile lives.  Operators may rebuild
the templates with ``setup.sh`` for faster boots, otherwise SKVM pulls the stock
image and runs the bootstrap script on first start (``image_mode = bootstrap``).
"""

from __future__ import annotations

from typing import Any

from ..config import BASE_DIR

IMAGE_DIR = BASE_DIR / "vm-images"


def _bootstrap(packages: str, install: str, extra: str = "") -> str:
    """Cloud-init style first-boot script shared by every Linux template."""
    return f"""#!/bin/bash
# SKVM first-boot bootstrap -----------------------------------------------
set -e
export DEBIAN_FRONTEND=noninteractive
{install}
$PKG_MANAGER update -y || true
$PKG_MANAGER install -y openssh-server sudo curl wget nano htop {packages} || true
mkdir -p /var/run/sshd /root/.ssh
chmod 700 /root/.ssh
sed -i 's/^#\\?\\s*PermitRootLogin\\s\\+.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sed -i 's/^#\\?\\s*PasswordAuthentication\\s\\+.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
echo "root:$SKVM_ROOT_PASSWORD" | chpasswd
if [ -n "$SKVM_AUTHORIZED_KEYS" ]; then printf '%s\\n' "$SKVM_AUTHORIZED_KEYS" > /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys; fi
mkdir -p /etc/ssh/sshd_config.d
printf 'Port 22\\nPermitRootLogin yes\\nPasswordAuthentication yes\\nX11Forwarding yes\\nClientAliveInterval 60\\n' > /etc/ssh/sshd_config.d/00-skvm.conf
[ -f /usr/sbin/sshd ] && /usr/sbin/sshd -D -e &
{extra}
touch /var/lib/skvm-ready
exec sleep infinity
"""


DEBIAN_INSTALL = "PKG_MANAGER=apt-get"
RHEL_INSTALL = "PKG_MANAGER=dnf"
ALPINE_INSTALL = "PKG_MANAGER=apk"
ARCH_INSTALL = "PKG_MANAGER=pacman"

IMAGE_CATALOG: dict[str, dict[str, Any]] = {
    "ubuntu-24.04": {
        "label": "Ubuntu 24.04 LTS",
        "docker_image": "ubuntu:24.04",
        "family": "debian",
        "dockerfile": "Dockerfile.ubuntu2404",
        "accent": "#e95420",
        "default": True,
        "bootstrap": _bootstrap("unzip git", DEBIAN_INSTALL),
    },
    "ubuntu-22.04": {
        "label": "Ubuntu 22.04 LTS",
        "docker_image": "ubuntu:22.04",
        "family": "debian",
        "dockerfile": "Dockerfile.ubuntu2204",
        "accent": "#e95420",
        "bootstrap": _bootstrap("unzip git", DEBIAN_INSTALL),
    },
    "debian-12": {
        "label": "Debian 12 (Bookworm)",
        "docker_image": "debian:12",
        "family": "debian",
        "dockerfile": "Dockerfile.debian12",
        "accent": "#d70a53",
        "bootstrap": _bootstrap("unzip git", DEBIAN_INSTALL),
    },
    "debian-13": {
        "label": "Debian 13 (Trixie)",
        "docker_image": "debian:13",
        "family": "debian",
        "dockerfile": "Dockerfile.debian13",
        "accent": "#d70a53",
        "bootstrap": _bootstrap("unzip git", DEBIAN_INSTALL),
    },
    "alpine-3.20": {
        "label": "Alpine 3.20 (ultra-light)",
        "docker_image": "alpine:3.20",
        "family": "alpine",
        "dockerfile": "Dockerfile.alpine320",
        "accent": "#0d597f",
        "bootstrap": _bootstrap("bash", ALPINE_INSTALL, extra="apk add --no-cache openrc || true"),
    },
    "rocky-9": {
        "label": "Rocky Linux 9",
        "docker_image": "rockylinux/rockylinux:9",
        "family": "rhel",
        "dockerfile": "Dockerfile.rocky9",
        "accent": "#10b981",
        "bootstrap": _bootstrap("unzip git", RHEL_INSTALL),
    },
    "arch-latest": {
        "label": "Arch Linux (rolling)",
        "docker_image": "archlinux:latest",
        "family": "arch",
        "dockerfile": "Dockerfile.arch",
        "accent": "#1793d1",
        "bootstrap": _bootstrap("unzip git", ARCH_INSTALL),
    },
    "windows-2022": {
        "label": "Windows Server 2022 (KVM only)",
        "docker_image": "",
        "family": "windows",
        "dockerfile": "",
        "accent": "#00a4ef",
        "kvm_only": True,
        "bootstrap": "",
    },
}

# Convenience presets surfaced in the create wizard.
PLAN_PRESETS: list[dict[str, Any]] = [
    {"id": "nano", "label": "Nano", "cpu": 1, "ram_mb": 1024, "disk_gb": 10, "price": 4, "blurb": "Dev sandbox, bots"},
    {"id": "starter", "label": "Starter", "cpu": 2, "ram_mb": 4096, "disk_gb": 40, "price": 9, "blurb": "Small apps, staging"},
    {"id": "pro", "label": "Pro", "cpu": 4, "ram_mb": 8192, "disk_gb": 80, "price": 19, "blurb": "Production workloads"},
    {"id": "business", "label": "Business", "cpu": 8, "ram_mb": 16384, "disk_gb": 160, "price": 39, "blurb": "Busy services, CI"},
    {"id": "scale", "label": "Scale", "cpu": 16, "ram_mb": 32768, "disk_gb": 320, "price": 79, "blurb": "Databases, clusters"},
    {"id": "custom", "label": "Custom", "cpu": 2, "ram_mb": 4096, "disk_gb": 50, "price": 0, "blurb": "Choose every resource"},
]

REGIONS = [
    {"id": "local", "label": "Local hypervisor", "flag": "◆"},
    {"id": "eu-central", "label": "EU Central · Frankfurt", "flag": "🇩🇪"},
    {"id": "eu-west", "label": "EU West · Amsterdam", "flag": "🇳🇱"},
    {"id": "us-east", "label": "US East · Ashburn", "flag": "🇺🇸"},
    {"id": "us-west", "label": "US West · Hillsboro", "flag": "🇺🇸"},
    {"id": "ap-south", "label": "Asia Pacific · Mumbai", "flag": "🇮🇳"},
    {"id": "ap-southeast", "label": "Asia Pacific · Singapore", "flag": "🇸🇬"},
]


def image_info(key: str) -> dict[str, Any]:
    data = IMAGE_CATALOG.get(key) or IMAGE_CATALOG["ubuntu-24.04"]
    return {"id": key, **data}


def image_options(include_kvm_only: bool = False) -> list[dict[str, Any]]:
    out = []
    for key, meta in IMAGE_CATALOG.items():
        if meta.get("kvm_only") and not include_kvm_only:
            continue
        out.append({"id": key, **meta})
    return out


def default_image() -> str:
    for key, meta in IMAGE_CATALOG.items():
        if meta.get("default"):
            return key
    return next(iter(IMAGE_CATALOG))

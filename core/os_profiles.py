"""Supported guest images for SKVM PANEL.

VPS-only scope: Ubuntu and Debian releases that both Incus
(images: remote) and Docker Hub publish and maintain. Each profile maps
one logical OS to its Incus image and its Docker image so the same panel
works on either backend without lying about what will boot.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class OSProfile:
    slug: str
    label: str
    image: str          # Incus image ref (backwards-compatible attribute)
    family: str         # guest family for provisioning ("debian")
    docker_image: str = ""
    container: bool = True
    vm: bool = True


OS_PROFILES = [
    OSProfile("ubuntu-20.04", "Ubuntu 20.04 LTS", "images:ubuntu/20.04", "debian", "ubuntu:20.04"),
    OSProfile("ubuntu-22.04", "Ubuntu 22.04 LTS", "images:ubuntu/22.04", "debian", "ubuntu:22.04"),
    OSProfile("ubuntu-24.04", "Ubuntu 24.04 LTS", "images:ubuntu/24.04", "debian", "ubuntu:24.04"),
    OSProfile("debian-11", "Debian 11 (Bullseye)", "images:debian/11", "debian", "debian:11"),
    OSProfile("debian-12", "Debian 12 (Bookworm)", "images:debian/12", "debian", "debian:12"),
]

BY_SLUG = {p.slug: p for p in OS_PROFILES}


def get_profile(slug: str) -> OSProfile:
    try:
        return BY_SLUG[slug]
    except KeyError as exc:
        raise ValueError(f"Unsupported OS profile: {slug}") from exc

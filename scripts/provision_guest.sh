#!/usr/bin/env bash
set -Eeuo pipefail
# Generic guest post-install. The host engine calls this after the instance is reachable.
case "$(. /etc/os-release && echo "${ID_LIKE:-$ID}")" in
  *debian*|*ubuntu*)
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y sudo curl ca-certificates openssh-server unattended-upgrades apt-listchanges
    systemctl enable --now ssh || true
    cat >/etc/apt/apt.conf.d/52skvm-unattended <<EOF
Unattended-Upgrade::Automatic-Reboot "false";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
    systemctl enable --now apt-daily-upgrade.timer || true
    ;;
  rhel|fedora|"fedora")
    dnf -y upgrade
    dnf -y install sudo curl ca-certificates openssh-server dnf-automatic
    systemctl enable --now sshd || true
    systemctl enable --now dnf-automatic.timer || true
    ;;
  alpine*)
    apk update && apk upgrade && apk add sudo curl ca-certificates openssh
    rc-update add crond default || true
    mkdir -p /etc/periodic/daily
    printf '#!/bin/sh\napk update && apk upgrade\n' >/etc/periodic/daily/skvm-auto-update
    chmod +x /etc/periodic/daily/skvm-auto-update
    ;;
  arch*)
    pacman -Syu --noconfirm
    pacman -S --noconfirm sudo curl ca-certificates openssh
    systemctl enable --now sshd || true
    ;;
  *) echo "Unsupported guest family; base OS remains available without package automation." ;;
esac

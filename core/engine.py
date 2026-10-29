from __future__ import annotations
import ipaddress, json, os, re, shlex, shutil, subprocess, time
from pathlib import Path
from .os_profiles import get_profile

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,54}$")

class EngineError(RuntimeError): pass

def safe_name(name: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip('-')
    if not NAME_RE.match(name): raise ValueError("Instance name must be 3-55 lowercase letters, digits, or hyphens")
    return name

def run(cmd, timeout=90, input_text=None, check=True):
    try:
        p = subprocess.run(cmd, input=input_text, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)
    except FileNotFoundError as e: raise EngineError(f"Missing command: {cmd[0]}") from e
    except subprocess.TimeoutExpired as e: raise EngineError(f"Command timed out after {timeout}s: {' '.join(shlex.quote(x) for x in cmd)}") from e
    out = (p.stdout or '').strip()
    if check and p.returncode != 0: raise EngineError(out[-4000:] or f"Command failed ({p.returncode})")
    return p.returncode, out

def incus_available(): return shutil.which('incus') is not None

def instance_exists(name):
    rc,_ = run(['incus','info',name],timeout=10,check=False)
    return rc == 0

def create_instance(name, image, engine, vcpus, memory_mb, disk_gb):
    if not incus_available(): raise EngineError("Incus is not installed. Run setup.sh first.")
    name = safe_name(name); profile=get_profile(image)
    image_ref = profile.image
    if engine not in ('container','vm'): raise ValueError("Engine must be container or vm")
    if instance_exists(name):
        # Idempotent recovery: a host reboot must not turn a half-completed job into a duplicate-instance failure.
        run(['incus','config','set',name,'limits.cpu',str(int(vcpus))],timeout=30)
        run(['incus','config','set',name,'limits.memory',f'{int(memory_mb)}MiB'],timeout=30)
        run(['incus','config','device','override',name,'root',f'size={int(disk_gb)}GiB'],timeout=60,check=False)
        st=instance_status(name)
        if st.get('state')=='stopped': run(['incus','start',name],timeout=120)
    else:
        args=['incus','launch',image_ref,name]
        if engine=='vm': args += ['--vm']
        args += ['-c',f'limits.cpu={int(vcpus)}','-c',f'limits.memory={int(memory_mb)}MiB','-d',f'root,size={int(disk_gb)}GiB']
        run(args, timeout=600)
    wait_ready(name, timeout=180 if engine=='vm' else 90)
    set_hostname(name,name)
    try:
        run(['incus','config','set',name,'user.skvm_managed','true'],timeout=10)
        run(['incus','config','set',name,'user.skvm_auto_update','true'],timeout=10)
    except EngineError: pass

def wait_ready(name, timeout=120):
    end=time.time()+timeout; last=''
    while time.time()<end:
        rc,out=run(['incus','exec',name,'--','true'],timeout=15,check=False)
        if rc==0: return
        last=out; time.sleep(3)
    raise EngineError(f"Instance did not become executable: {last[-500:]}")

def set_hostname(name, hostname):
    safe_name(hostname)
    cmds=[['hostnamectl','set-hostname',hostname],['sh','-c',f'echo {shlex.quote(hostname)} > /etc/hostname']]
    for cmd in cmds:
        run(['incus','exec',name,'--',*cmd],timeout=20,check=False)
    host_line=f'127.0.1.1 {hostname}'
    run(['incus','exec',name,'--','sh','-lc',f"grep -qxF {shlex.quote(host_line)} /etc/hosts || printf '%s\n' {shlex.quote(host_line)} >> /etc/hosts"],timeout=20,check=False)

def guest_exec(name, command, timeout=300):
    return run(['incus','exec',name,'--','sh','-lc',command],timeout=timeout)

def guest_file(name, path, content, mode='0644'):
    # Use stdin to avoid shell quoting user content.
    script=f"cat > {shlex.quote(path)} && chmod {shlex.quote(mode)} {shlex.quote(path)}"
    return run(['incus','exec',name,'--','sh','-lc',script],timeout=30,input_text=content)

def install_guest_support(name, family, auto_update=True):
    base = "export DEBIAN_FRONTEND=noninteractive;"
    if family=='debian':
        guest_exec(name, base+" apt-get update -y && apt-get install -y sudo curl ca-certificates openssh-server",timeout=420)
        if auto_update:
            guest_exec(name, base+" apt-get install -y unattended-upgrades apt-listchanges && systemctl enable --now unattended-upgrades.service 2>/dev/null || true; systemctl enable --now apt-daily-upgrade.timer 2>/dev/null || true",timeout=420)
            guest_file(name,'/etc/apt/apt.conf.d/52skvm-unattended', 'Unattended-Upgrade::Automatic-Reboot "false";\nAPT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n')
    elif family=='rhel':
        guest_exec(name,"dnf -y upgrade && dnf -y install sudo curl ca-certificates openssh-server dnf-automatic; systemctl enable --now sshd 2>/dev/null || true",timeout=600)
        if auto_update: guest_exec(name,"systemctl enable --now dnf-automatic.timer 2>/dev/null || true",timeout=120)
    elif family=='alpine':
        guest_exec(name,"apk update && apk upgrade && apk add sudo curl ca-certificates openssh",timeout=420)
        guest_exec(name,"rc-update add crond default 2>/dev/null || true; mkdir -p /etc/periodic/daily; printf '#!/bin/sh\napk update && apk upgrade\n' > /etc/periodic/daily/skvm-auto-update; chmod +x /etc/periodic/daily/skvm-auto-update",timeout=60)
    elif family=='arch':
        guest_exec(name,"pacman -Syu --noconfirm && pacman -S --noconfirm sudo curl ca-certificates openssh",timeout=600)
        guest_file(name,'/etc/systemd/system/skvm-auto-update.service','''[Unit]\nDescription=Omega automatic OS update\nAfter=network-online.target\n\n[Service]\nType=oneshot\nExecStart=/usr/bin/pacman -Syu --noconfirm\n''')
        guest_file(name,'/etc/systemd/system/skvm-auto-update.timer','''[Unit]\nDescription=Omega daily Arch updates\n\n[Timer]\nOnCalendar=daily\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n''')
        guest_exec(name,"systemctl daemon-reload && systemctl enable --now skvm-auto-update.timer",timeout=60)

def get_ip(name):
    rc,out=run(['incus','list',name,'--format','json'],timeout=20,check=False)
    if rc!=0: return None
    try:
        data=json.loads(out)
        if not data: return None
        state=data[0].get('state',{})
        for iface in state.get('network',{}).values():
            for addr in iface.get('addresses',[]):
                if addr.get('family')=='inet' and addr.get('scope')=='global':
                    ip=addr.get('address')
                    try:
                        if not ipaddress.ip_address(ip).is_loopback: return ip
                    except ValueError: pass
    except Exception: pass
    return None

def instance_status(name):
    rc,out=run(['incus','info',name],timeout=15,check=False)
    if rc!=0: return {'state':'missing','raw':out[-500:]}
    state='running' if 'Status: RUNNING' in out or 'Status: Running' in out else 'stopped'
    return {'state':state,'ip':get_ip(name),'raw':out[-2000:]}

def resize_instance(name, vcpus, memory_mb, disk_gb):
    safe_name(name)
    run(['incus','config','set',name,'limits.cpu',str(int(vcpus))],timeout=30)
    run(['incus','config','set',name,'limits.memory',f'{int(memory_mb)}MiB'],timeout=30)
    # Incus supports overriding the root device size on supported storage drivers.
    run(['incus','config','device','override',name,'root',f'size={int(disk_gb)}GiB'],timeout=60)
    return instance_status(name)

def action(name, op):
    safe_name(name)
    if op not in ('start','stop','restart'): raise ValueError('Unsupported action')
    if op=='start': run(['incus','start',name],timeout=120)
    elif op=='stop': run(['incus','stop',name,'--force'],timeout=120)
    else:
        run(['incus','restart',name,'--force'],timeout=180)
    return instance_status(name)

def remove(name):
    safe_name(name)
    run(['incus','delete',name,'--force'],timeout=180)

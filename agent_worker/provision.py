"""Unattended Xubuntu installation and repair of the guest toolchain."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import shutil
import signal
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, NoReturn

import yaml

from agent_worker.guest.setup import legacy_helper, setup
from agent_worker.settings import Settings
from agent_worker.shares import Shares
from agent_worker.system import SudoSession, file_lock, host_environment
from agent_worker.vm import GUEST_DIR, VM


def load_packages(path: Path) -> list[str]:
    packages = []
    for line in path.read_text().splitlines():
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(:[a-z0-9][a-z0-9-]*)?", value):
            raise ValueError(f"Invalid package name: {value}")
        packages.append(value)
    if not packages:
        raise ValueError(f"Package list is empty: {path}")
    return packages


def private_write(path: Path, text: str) -> None:
    # Create with private permissions before writing sensitive data.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(text)


def autoinstall(settings: Settings, packages: list[str], password_hash: str) -> dict[str, Any]:
    home = f"/home/{settings.agent_user}"
    files = []
    for name, path, permissions in (
        ("codex.config.toml", f"{home}/.codex/config.toml", "0600"),
        ("tmux.conf", f"{home}/.tmux.conf", "0644"),
        ("tmux.conf.local", f"{home}/.tmux.conf.local", "0644"),
    ):
        files.append(
            {
                "path": path,
                "owner": f"{settings.agent_user}:{settings.agent_user}",
                "permissions": permissions,
                "encoding": "b64",
                "content": base64.b64encode((settings.resource_dir / name).read_bytes()).decode(),
            }
        )
    files.append(
        {
            "path": f"/etc/sudoers.d/90-{settings.agent_user}",
            "owner": "root:root",
            "permissions": "0440",
            "content": f"{settings.agent_user} ALL=(ALL:ALL) NOPASSWD: ALL\n",
        }
    )
    files.append(
        {
            "path": "/usr/local/lib/agent-worker/project.py",
            "owner": "root:root",
            "permissions": "0644",
            "content": (GUEST_DIR / "project.py").read_text(),
        }
    )
    for action in ("mount", "unmount"):
        files.append(
            {
                "path": f"/usr/local/bin/{settings.workspace_helper_prefix}-{action}",
                "owner": "root:root",
                "permissions": "0755",
                "content": legacy_helper(action, settings.agent_user, settings.share_tag),
            }
        )
    data = {
        "autoinstall": {
            "version": 1,
            "source": {"id": "xubuntu-desktop-minimal", "search_drivers": False},
            "locale": settings.locale,
            "keyboard": {"layout": settings.keyboard_layout, "variant": settings.keyboard_variant},
            "timezone": settings.timezone,
            "identity": {"hostname": settings.guest_hostname, "username": settings.agent_user, "password": password_hash},
            "ssh": {
                "install-server": True,
                "allow-pw": False,
                "authorized-keys": [settings.ssh_key.with_suffix(".pub").read_text().strip()],
            },
            "storage": {"layout": {"name": "direct"}},
            "packages": packages,
            "updates": "security",
            "shutdown": "poweroff",
            "user-data": {
                "ssh_pwauth": False,
                "disable_root": True,
                "write_files": files,
                "runcmd": [
                    ["chown", f"{settings.agent_user}:{settings.agent_user}", f"{home}/.codex"],
                    ["chmod", "0700", f"{home}/.codex"],
                    ["mkdir", "-p", f"{home}/workspace"],
                    ["chown", f"{settings.agent_user}:{settings.agent_user}", f"{home}/workspace"],
                    ["ln", "-sf", "/usr/bin/fdfind", "/usr/local/bin/fd"],
                    ["systemctl", "enable", "--now", "qemu-guest-agent.service"],
                    ["npm", "install", "-g", "@openai/codex@latest"],
                    ["touch", "/var/lib/agent-worker-ready"],
                ],
            },
        }
    }
    settings.seed_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.seed_dir.chmod(0o700)
    private_write(settings.seed_dir / "user-data", "#cloud-config\n" + yaml.safe_dump(data, sort_keys=False))
    private_write(
        settings.seed_dir / "meta-data",
        yaml.safe_dump(
            {
                "instance-id": f"{settings.vm_name}-{time.time_ns()}",
                "local-hostname": settings.guest_hostname,
            }
        ),
    )
    return data


class Provisioner:
    def __init__(self, vm: VM) -> None:
        self.vm = vm
        self.settings = vm.settings
        self.runner = vm.runner

    def network(self) -> None:
        vm = self.vm
        settings = self.settings
        self.runner.run("sudo", "systemctl", "enable", "--now", "libvirtd.service")
        if vm.virsh("net-info", "default", capture=True, check=False).returncode:
            network_definition = Path("/usr/share/libvirt/networks/default.xml")
            if not network_definition.is_file():
                network_definition = settings.resource_dir / "default_network.xml"
            vm.virsh("net-define", network_definition)
        vm.virsh("net-autostart", "default")
        active_networks = vm.virsh("net-list", "--name", capture=True).stdout.splitlines()
        if "default" not in active_networks:
            vm.virsh("net-start", "default")

    def download_iso(self) -> Path:
        settings = self.settings
        release = f"https://cdimage.ubuntu.com/xubuntu/releases/{settings.xubuntu_series}/release"

        def fetch(url: str) -> str:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read().decode()

        listing = fetch(release + "/")
        matches = set(re.findall(rf"xubuntu-{re.escape(settings.xubuntu_series)}(?:\.\d+)?-minimal-amd64\.iso", listing))
        if not matches:
            raise RuntimeError(f"Could not determine a Xubuntu Minimal ISO from {release}")
        name = max(matches, key=lambda value: tuple(map(int, re.findall(r"\d+", value))))
        iso_dir = settings.image_dir / "iso"
        path = iso_dir / name
        self.runner.run("sudo", "install", "-d", "-m", "0755", "--", iso_dir)
        if not path.is_file():
            partial = path.with_suffix(".iso.part")
            self.runner.run("sudo", "curl", "-fL", "--progress-bar", "-o", partial, f"{release}/{name}")
            self.runner.run("sudo", "mv", "--", partial, path)
            self.runner.run("sudo", "chmod", "0644", "--", path)
        expected = next(
            (line.split()[0] for line in fetch(release + "/SHA256SUMS").splitlines() if len(line.split()) == 2 and line.split()[1].lstrip("*") == name),
            None,
        )
        if expected is None:
            raise RuntimeError(f"Could not find checksum for {name}")
        with path.open("rb") as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
        if actual != expected:
            raise RuntimeError(f"Checksum mismatch for {path}")
        return path

    def credentials(self, recreate: bool) -> str:
        settings = self.settings
        if not settings.ssh_key.is_file():
            self.runner.run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "agent-worker", "-f", settings.ssh_key)
        settings.ssh_key.chmod(0o600)
        if recreate or not settings.password_file.is_file():
            private_write(settings.password_file, secrets.token_hex(12) + "\n")
        settings.password_file.chmod(0o600)
        # Use stdin so the password never appears in the host process listing.
        return self.runner.run("openssl", "passwd", "-6", "-stdin", capture=True, input=settings.password_file.read_text()).stdout.strip()

    def install(self, iso: Path) -> None:
        settings = self.settings
        self.runner.run("sudo", "install", "-d", "-m", "0755", "--", settings.image_dir)
        self.storage_access(iso)
        print(f"Installing '{settings.vm_name}'. Progress: sudo virsh console {settings.vm_name}", flush=True)
        self.runner.run(
            "sudo",
            "virt-install",
            "--connect",
            settings.libvirt_uri,
            "--name",
            settings.vm_name,
            "--memory",
            settings.ram_mb,
            "--vcpus",
            settings.vcpus,
            "--cpu",
            "host-passthrough",
            "--machine",
            "q35",
            "--boot",
            "uefi",
            "--disk",
            f"path={settings.disk_path},size={settings.disk_gb},format=qcow2,bus=virtio,discard=unmap",
            "--location",
            f"{iso},kernel=casper/vmlinuz,initrd=casper/initrd",
            "--extra-args",
            "autoinstall console=ttyS0,115200n8",
            "--cloud-init",
            f"user-data={settings.seed_dir}/user-data,meta-data={settings.seed_dir}/meta-data",
            "--network",
            "network=default,model=virtio",
            "--graphics",
            "spice",
            "--video",
            "virtio",
            "--console",
            "pty,target.type=serial",
            "--channel",
            "unix,target.type=virtio,target.name=org.qemu.guest_agent.0",
            "--memorybacking",
            "source.type=memfd,access.mode=shared",
            "--osinfo",
            "detect=on,require=off",
            "--noreboot",
            "--noautoconsole",
            "--wait=-1",
        )
        self.vm.virsh("start", settings.vm_name)

    def storage_access(self, iso: Path) -> None:
        """Allow the configured QEMU account to reach images in private paths."""
        capabilities = ET.fromstring(self.vm.virsh("capabilities", readonly=True, capture=True).stdout)
        label = capabilities.findtext("./host/secmodel[model='dac']/baselabel[@type='kvm']", default="")
        identity = re.fullmatch(r"\+?(\d+):\+?(\d+)", label.strip())
        if identity is None:
            raise RuntimeError("Cannot determine QEMU's UID:GID from libvirt capabilities; cannot prepare storage permissions.")
        uid, gid = identity.groups()

        def accessible(flag: str, path: Path) -> bool:
            return self.runner.run("sudo", "-u", f"#{uid}", "-g", f"#{gid}", "--", "test", flag, path, capture=True, check=False).returncode == 0

        def grant(path: Path, permission: str, flag: str) -> None:
            if accessible(flag, path):
                return
            print(f"Granting QEMU (UID {uid}) {permission} access to {path}", flush=True)
            self.runner.run("sudo", "setfacl", "-m", f"u:{uid}:{permission}", "--", path)
            if not accessible(flag, path):
                raise RuntimeError(f"QEMU (UID:GID {uid}:{gid}) still cannot access {path} after setting its ACL.")

        # Resolve symlinks and walk from root down so each access check can reach
        # its target. Never grant listing/write access to parent directories or
        # recurse into data/state, which contains private SSH keys and seeds.
        directories = set(self.settings.disk_path.resolve().parents) | set(iso.resolve().parents)
        for directory in sorted(directories, key=lambda path: (len(path.parts), str(path))):
            grant(directory, "--x", "-x")
        grant(iso.resolve(), "r--", "-r")
        # Libvirt creates and labels the VM disk itself during virt-install.

    def wait(self) -> str:
        deadline = time.monotonic() + self.settings.ready_timeout
        while time.monotonic() < deadline:
            try:
                ip = self.vm.ip(readonly=False)
                result = self.vm.ssh("true", ip=ip, capture=True, check=False)
            except RuntimeError:
                time.sleep(3)
                continue
            if result.returncode == 0:
                # A provisioning failure is final; do not hide it as an SSH retry.
                self.vm.ssh("sudo cloud-init status --wait", ip=ip)
                return ip
            time.sleep(3)
        raise RuntimeError(f"VM did not become reachable over SSH. Check: sudo virsh console {self.settings.vm_name}")

    def create(self, recreate: bool = False) -> None:
        settings = self.settings
        if os.geteuid() == 0:
            raise RuntimeError("Run creation as your normal user, not as root. It uses sudo where needed.")
        if not Path("/dev/kvm").exists():
            raise RuntimeError("/dev/kvm does not exist. Enable Intel VT-x/AMD-V and load the KVM modules.")
        if not shutil.which("pacman"):
            raise RuntimeError("Creation currently targets an Arch Linux host (pacman not found).")
        if (os.getuid(), os.getgid()) != (1000, 1000):
            print("WARNING: Host UID:GID differs from the guest's usual 1000:1000; virtiofs preserves numeric ownership.")
        packages = load_packages(settings.resource_dir / "packages_list")
        host_packages = load_packages(settings.resource_dir / "host_packages_list")
        for name in ("tmux.conf", "tmux.conf.local", "codex.config.toml"):
            (settings.resource_dir / name).read_bytes()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings.state_dir.chmod(0o700)

        def terminate(*_: object) -> NoReturn:
            raise SystemExit(143)

        previous = signal.signal(signal.SIGTERM, terminate)
        try:
            with SudoSession(self.runner), file_lock(settings.state_dir / "project-shares.lock"):
                self.runner.run("sudo", "pacman", "-S", "--needed", "--noconfirm", *host_packages)
                self.network()
                exists = self.vm.virsh("dominfo", settings.vm_name, capture=True, check=False).returncode == 0
                if exists and not recreate:
                    raise RuntimeError(f"VM '{settings.vm_name}' already exists. Re-run with --recreate to replace it.")
                iso = self.download_iso()
                password_hash = self.credentials(recreate)
                autoinstall(settings, packages, password_hash)
                if exists:
                    self.vm.remove_domain()
                if recreate:
                    Shares(self.vm).cleanup()
                    self.runner.run("sudo", "rm", "-f", "--", settings.disk_path)
                self.install(iso)
                ip = self.wait()
                version = self.vm.ssh("codex --version", ip=ip, capture=True, check=False).stdout.strip()
                assert isinstance(version, str)
                print(
                    f"\nAgent Worker is ready\nVM: {settings.vm_name}\nIP: {ip}\nUser: {settings.agent_user}\nCodex: {version}"
                    f"\nGUI password: {settings.password_file.read_text().strip()}\nSSH key: {settings.ssh_key}"
                    "\n\nConnect: agent_worker vm connect\nAuthenticate Codex once inside the VM."
                    "\nShare projects: agent_worker project attach /path/to/project"
                )
        finally:
            signal.signal(signal.SIGTERM, previous)

    def setup_guest(self) -> None:
        settings = self.settings
        setup(
            load_packages(settings.resource_dir / "packages_list"),
            settings.agent_user,
            settings.workspace_helper_prefix,
            settings.share_tag,
            (GUEST_DIR / "project.py").read_text(),
            environment=host_environment(),
        )

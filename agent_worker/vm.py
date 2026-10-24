"""Libvirt lifecycle and dedicated SSH connections."""

from __future__ import annotations

import io
import ipaddress
import shlex
import subprocess
import tarfile
from pathlib import Path
from typing import Unpack

from agent_worker.settings import Settings
from agent_worker.system import (
    CommandArg,
    Runner,
    RunOptions,
    TextRunOptions,
    file_lock,
)

GUEST_DIR = Path(__file__).parent / "guest"


class VM:
    def __init__(self, settings: Settings, runner: Runner | None = None) -> None:
        self.settings = settings
        self.runner = runner or Runner()

    def virsh(self, *args: CommandArg, readonly: bool = False, **kwargs: Unpack[TextRunOptions]) -> subprocess.CompletedProcess[str]:
        prefix = ["virsh", "--readonly"] if readonly else ["sudo", "virsh"]
        return self.runner.run(*prefix, "-c", self.settings.libvirt_uri, *args, **kwargs)

    def ip(self, *, readonly: bool = True) -> str:
        addresses = self.virsh("domifaddr", self.settings.vm_name, "--source", "agent", readonly=readonly, capture=True, check=False)
        for line in addresses.stdout.splitlines():
            columns = line.split()
            if len(columns) >= 4 and columns[2] == "ipv4":
                if address := self._usable_ip(columns[3]):
                    return address
        interfaces = self.virsh("domiflist", self.settings.vm_name, readonly=readonly, capture=True)
        mac = next((c[4] for line in interfaces.stdout.splitlines() if len(c := line.split()) >= 5 and c[1] == "network"), None)
        if mac:
            leases = self.virsh("net-dhcp-leases", "default", readonly=readonly, capture=True)
            for line in leases.stdout.splitlines():
                c = line.split()
                if len(c) >= 5 and c[2].lower() == mac.lower() and c[3] == "ipv4":
                    if address := self._usable_ip(c[4]):
                        return address
        raise RuntimeError(f"No guest IP found for '{self.settings.vm_name}'. Check: agent_worker vm status")

    @staticmethod
    def _usable_ip(value: str) -> str | None:
        try:
            address = ipaddress.IPv4Address(value.split("/")[0])
        except ipaddress.AddressValueError:
            return None
        return str(address) if not (address.is_loopback or address.is_link_local or int(address) >> 24 == 0) else None

    def ssh(
        self, *args: CommandArg, ip: str | None = None, tty: bool = False, **kwargs: Unpack[RunOptions]
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        settings = self.settings
        if not settings.ssh_key.is_file():
            raise RuntimeError(f"Dedicated SSH key not found: {settings.ssh_key}. Check STATE_DIR or create the VM first.")
        if ip is None:
            self.virsh("dominfo", settings.vm_name, readonly=True, capture=True)
            ip = self.ip()
        return self.runner.run(
            "ssh",
            *(["-t"] if tty else []),
            "-i",
            settings.ssh_key,
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={settings.known_hosts}",
            f"{settings.agent_user}@{ip}",
            *args,
            **kwargs,
        )

    def start(self) -> None:
        settings = self.settings
        if self.virsh("domstate", settings.vm_name, capture=True).stdout.strip() == "running":
            print(f"VM '{settings.vm_name}' is already running.")
        else:
            self.virsh("start", settings.vm_name)
            print("VM started. Once SSH is ready, connect with: agent_worker vm connect")

    def shutdown(self) -> None:
        from agent_worker.shares import Shares

        settings = self.settings
        try:
            old_shares = any(share.staged for share in Shares(self).records())
        except (OSError, UnicodeError):
            # Missing/corrupt tracking must not prevent shutting down the VM.
            old_shares = True
        if old_shares:
            print(
                "WARNING: Old project bind mounts may survive VM shutdown. Run agent_worker project detach --all "
                "before deleting or moving runtime data; deleting staging contents deletes original project files."
            )
        if self.virsh("domstate", settings.vm_name, capture=True).stdout.strip() == "shut off":
            print(f"VM '{settings.vm_name}' is already shut off.")
        else:
            self.virsh("shutdown", settings.vm_name)
            print("Graceful shutdown requested. Check completion with: agent_worker vm status")

    def status(self) -> None:
        from agent_worker.shares import Shares

        settings = self.settings
        self.virsh("dominfo", settings.vm_name, readonly=True)
        try:
            ip = self.ip()
        except RuntimeError:
            ip = "unavailable"
        print(f"\nGuest IP:      {ip}\nGuest user:    {settings.agent_user}\nData directory: {settings.data_dir}")
        records = Shares(self).records()
        if not records:
            print("Host project:  none attached")
        for share in records:
            print(f"Host project:  {share.project}\nGuest project: /home/{settings.agent_user}/workspace/{share.name}")

    def remove_domain(self) -> None:
        settings = self.settings
        if self.virsh("domstate", settings.vm_name, capture=True).stdout.strip() not in ("shut off", "shutoff"):
            self.virsh("destroy", settings.vm_name)
        if self.virsh("undefine", settings.vm_name, "--nvram", capture=True, check=False).returncode:
            self.virsh("undefine", settings.vm_name)

    def destroy(self, yes: bool = False) -> None:
        from agent_worker.shares import Shares

        settings = self.settings
        with file_lock(settings.state_dir / "project-shares.lock"):
            if self.virsh("dominfo", settings.vm_name, capture=True, check=False).returncode == 0:
                if not yes and input(f"Destroy VM '{settings.vm_name}' and delete {settings.disk_path}? [y/N] ").lower() != "y":
                    return
                self.remove_domain()
            else:
                print(f"VM '{settings.vm_name}' is not defined; cleaning any leftover disk/share state.")
            Shares(self).cleanup()
            self.runner.run("sudo", "rm", "-f", "--", settings.disk_path)
        print(f"Removed '{settings.vm_name}'. Kept the downloaded ISO and dedicated VM SSH key.")

    def connect(self) -> None:
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            for name in (
                "workspace.py",
                "dashboard.py",
                "codex_usage.py",
                "configure_codex.py",
                "github_workflow.py",
                "github_client.py",
                "git_credential.py",
                "github_review.py",
            ):
                archive.add(GUEST_DIR / name, arcname=name)
            for name in ("tmux.conf", "tmux.conf.local", "codex.config.toml"):
                archive.add(self.settings.resource_dir / name, arcname=name)
        self.ssh(
            'umask 077; mkdir -p "$HOME/.local/lib/agent-worker" && tar -xf - -C "$HOME/.local/lib/agent-worker"',
            input=payload.getvalue(),
        )
        self.ssh('exec python3 "$HOME/.local/lib/agent-worker/workspace.py"', tty=True)

    def usage(self, json: bool = False) -> None:
        command = shlex.join(["python3", "-", *(["--json"] if json else [])])
        self.ssh(command, input=(GUEST_DIR / "codex_usage.py").read_text())

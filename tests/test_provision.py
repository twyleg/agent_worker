from __future__ import annotations

import hashlib
import io
import json
import subprocess
from contextlib import ExitStack
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

import pytest

from agent_worker.provision import Provisioner, load_packages
from agent_worker.settings import Settings
from agent_worker.system import CommandError, Runner
from agent_worker.vm import VM


def provisioner(tmp_path: Path) -> Provisioner:
    settings = Settings.load(overrides={"data_dir": tmp_path}, environ={})
    runner = Runner()
    setattr(runner, "run", Mock(return_value=subprocess.CompletedProcess([], 0, "$6$password-hash\n", "")))
    return Provisioner(VM(settings, runner))


def test_iso_selects_latest_and_verifies_cached_bytes(tmp_path: Path) -> None:
    p = provisioner(tmp_path)
    iso_dir = p.settings.image_dir / "iso"
    iso_dir.mkdir(parents=True)
    name = "xubuntu-26.04.10-minimal-amd64.iso"
    iso = iso_dir / name
    iso.write_bytes(b"verifiable image")
    checksum = hashlib.sha256(iso.read_bytes()).hexdigest()
    listing = "xubuntu-26.04.2-minimal-amd64.iso\n" + name
    for expected, succeeds in ((checksum, True), ("0" * 64, False)):
        responses = [io.BytesIO(listing.encode()), io.BytesIO(f"{expected} *{name}\n".encode())]
        with patch("urllib.request.urlopen", side_effect=responses):
            if succeeds:
                assert p.download_iso() == iso
            else:
                with pytest.raises(RuntimeError, match="Checksum mismatch"):
                    p.download_iso()
    assert not any("curl" in call.args for call in cast(Mock, p.runner.run).call_args_list)


def test_cloud_init_failure_is_reported_immediately(tmp_path: Path) -> None:
    p = provisioner(tmp_path)
    setattr(p.vm, "ip", Mock(return_value="192.168.122.10"))
    failure = CommandError(["ssh"], subprocess.CompletedProcess([], 1, "", "cloud-init failed"))
    setattr(p.vm, "ssh", Mock(side_effect=[subprocess.CompletedProcess([], 0, "", ""), failure]))
    with patch("time.sleep") as sleep, pytest.raises(CommandError):
        p.wait()
    sleep.assert_not_called()


def test_wait_retries_until_ssh_is_ready(tmp_path: Path) -> None:
    p = provisioner(tmp_path)
    setattr(p.vm, "ip", Mock(side_effect=[RuntimeError("not ready"), "192.168.122.10", "192.168.122.10"]))
    setattr(
        p.vm,
        "ssh",
        Mock(
            side_effect=[
                subprocess.CompletedProcess([], 255, "", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
        ),
    )
    with patch("time.sleep"):
        assert p.wait() == "192.168.122.10"
    assert cast(Mock, p.vm.ssh).call_args.args == ("sudo cloud-init status --wait",)


def test_wait_timeout(tmp_path: Path) -> None:
    p = provisioner(tmp_path)
    with (
        patch("time.monotonic", side_effect=[0, p.settings.ready_timeout + 1]),
        pytest.raises(RuntimeError, match="reachable"),
    ):
        p.wait()


def prepare_create(p: Provisioner) -> None:
    p.settings.state_dir.mkdir(parents=True)
    p.settings.ssh_key.write_text("private-key-placeholder")
    p.settings.ssh_key.with_suffix(".pub").write_text("ssh-ed25519 public-key")
    setattr(p, "network", Mock())
    setattr(p, "download_iso", Mock(return_value=p.settings.image_dir / "iso/test.iso"))
    setattr(p, "install", Mock())
    setattr(p, "wait", Mock(return_value="192.168.122.10"))
    setattr(p.vm, "ssh", Mock(return_value=subprocess.CompletedProcess([], 0, "codex test-version", "")))


def host_patches() -> ExitStack:
    exists = Path.exists
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("os.geteuid", return_value=1000))
    stack.enter_context(patch("os.getuid", return_value=1000))
    stack.enter_context(patch("os.getgid", return_value=1000))
    stack.enter_context(patch("shutil.which", return_value="/usr/bin/pacman"))
    stack.enter_context(patch.object(Path, "exists", lambda path: True if str(path) == "/dev/kvm" else exists(path)))
    return stack


def test_create_pipeline_and_private_credentials(tmp_path: Path) -> None:
    p = provisioner(tmp_path)
    prepare_create(p)
    setattr(p.vm, "virsh", Mock(return_value=subprocess.CompletedProcess([], 1, "", "undefined")))
    with host_patches():
        p.create()
    cast(Mock, p.install).assert_called_once_with(cast(Mock, p.download_iso).return_value)
    cast(Mock, p.runner.run).assert_any_call("sudo", "pacman", "-S", "--needed", "--noconfirm", *load_packages(p.settings.resource_dir / "host_packages_list"))
    assert p.settings.password_file.stat().st_mode & 0o777 == 0o600
    assert p.settings.state_dir.stat().st_mode & 0o777 == 0o700
    assert p.settings.ssh_key.stat().st_mode & 0o777 == 0o600
    assert (p.settings.seed_dir / "user-data").is_file()
    identity = json.loads((p.settings.data_dir / "vm.json").read_text())
    assert identity == {"vm_name": p.settings.vm_name, "libvirt_uri": p.settings.libvirt_uri, "agent_user": p.settings.agent_user}
    assert p.runner.noninteractive_sudo is False
    openssl_call = next(c for c in cast(Mock, p.runner.run).call_args_list if c.args[:2] == ("openssl", "passwd"))
    assert p.settings.password_file.read_text().strip() not in openssl_call.args


def test_existing_vm_requires_explicit_recreate(tmp_path: Path) -> None:
    p = provisioner(tmp_path)
    prepare_create(p)
    setattr(p.vm, "virsh", Mock(return_value=subprocess.CompletedProcess([], 0, "exists", "")))
    setattr(p.vm, "remove_domain", Mock())
    with host_patches(), pytest.raises(RuntimeError, match="--recreate"):
        p.create()
    cast(Mock, p.vm.remove_domain).assert_not_called()
    cast(Mock, p.install).assert_not_called()
    cast(Mock, p.download_iso).assert_not_called()
    assert not (p.settings.data_dir / "vm.json").exists()
    assert p.runner.noninteractive_sudo is False


def test_recreate_removes_domain_and_disk_but_keeps_ssh_key(tmp_path: Path) -> None:
    p = provisioner(tmp_path)
    prepare_create(p)
    setattr(p.vm, "virsh", Mock(return_value=subprocess.CompletedProcess([], 0, "exists", "")))
    setattr(p.vm, "remove_domain", Mock())
    with host_patches(), patch("agent_worker.provision.Shares") as shares:
        p.create(recreate=True)
    cast(Mock, p.vm.remove_domain).assert_called_once()
    shares.return_value.cleanup.assert_called_once()
    cast(Mock, p.runner.run).assert_any_call("sudo", "rm", "-f", "--", p.settings.disk_path)
    assert p.settings.ssh_key.read_text() == "private-key-placeholder"


def test_network_uses_resource_fallback_when_system_definition_is_missing(tmp_path: Path) -> None:
    p = provisioner(tmp_path)
    is_file = Path.is_file
    with (
        patch.object(Path, "is_file", lambda path: False if str(path) == "/usr/share/libvirt/networks/default.xml" else is_file(path)),
        patch.object(p.vm, "virsh") as virsh,
    ):
        virsh.side_effect = lambda action, *args, **kwargs: subprocess.CompletedProcess([], 1 if action == "net-info" else 0, "default\n", "")
        p.network()
    virsh.assert_any_call("net-define", p.settings.resource_dir / "default_network.xml")

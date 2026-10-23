from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from agent_worker.provision import Provisioner
from agent_worker.settings import Settings
from agent_worker.system import Runner
from agent_worker.vm import VM


CAPABILITIES = """<capabilities><host>
<secmodel><model>none</model></secmodel>
<secmodel><model>dac</model><baselabel type="kvm">+959:+959</baselabel></secmodel>
</host></capabilities>"""


@pytest.fixture
def provisioner(tmp_path: Path) -> Provisioner:
    runner = Mock(spec=Runner)
    runner.run.return_value = subprocess.CompletedProcess([], 0, "", "")
    settings = Settings.load(overrides={"data_dir": tmp_path / "private home/.local/share/agent-worker"}, environ={})
    vm = VM(settings, runner)
    setattr(vm, "virsh", Mock(return_value=subprocess.CompletedProcess([], 0, CAPABILITIES, "")))
    return Provisioner(vm)


def test_private_parent_access_is_repaired_without_opening_state(provisioner: Provisioner) -> None:
    p = provisioner
    iso = p.settings.image_dir / "iso/test.iso"
    private_parents = {p.settings.data_dir.parent, p.settings.data_dir.parent.parent}
    inaccessible = set(private_parents)
    granted = []

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        path = cast(Path, args[-1])
        if args[:2] == ("sudo", "setfacl"):
            granted.append(path)
            assert args[3] == "u:959:--x"
            inaccessible.remove(path)
        else:
            assert args[:9] == ("sudo", "-u", "#959", "-g", "#959", "--", "test", "-x" if path != iso else "-r", path)
        return subprocess.CompletedProcess([], int(any(parent in inaccessible for parent in (path, *path.parents))), "", "")

    cast(Mock, p.runner.run).side_effect = run
    p.storage_access(iso)
    assert set(granted) == private_parents
    assert len(granted[0].parts) < len(granted[1].parts)
    assert p.settings.state_dir not in granted
    cast(Mock, p.vm.virsh).assert_called_once_with("capabilities", readonly=True, capture=True)


def test_accessible_paths_need_no_acl_changes(provisioner: Provisioner) -> None:
    provisioner.storage_access(provisioner.settings.image_dir / "iso/test.iso")
    assert all("setfacl" not in call.args for call in cast(Mock, provisioner.runner.run).call_args_list)


def test_private_iso_gets_read_access_only(provisioner: Provisioner) -> None:
    iso = provisioner.settings.image_dir / "iso/private.iso"
    granted = False

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal granted
        if args[:2] == ("sudo", "setfacl"):
            assert args == ("sudo", "setfacl", "-m", "u:959:r--", "--", iso)
            granted = True
        return subprocess.CompletedProcess([], int(args[-1] == iso and not granted), "", "")

    cast(Mock, provisioner.runner.run).side_effect = run
    provisioner.storage_access(iso)
    assert granted


def test_symlink_and_custom_image_directory_use_real_storage_paths(provisioner: Provisioner, tmp_path: Path) -> None:
    images = tmp_path / "separate images"
    images.mkdir()
    link = tmp_path / "images-link"
    link.symlink_to(images, target_is_directory=True)
    settings = Settings.load(overrides={"data_dir": tmp_path / "data", "image_dir": link}, environ={})
    provisioner.settings = settings
    provisioner.storage_access(link / "test.iso")
    paths = {call.args[-1] for call in cast(Mock, provisioner.runner.run).call_args_list}
    assert images in paths
    assert images / "test.iso" in paths
    assert link not in paths
    assert settings.state_dir not in paths


@pytest.mark.parametrize("label", ["", "qemu:qemu", "+959", "-1:-1"])
def test_unknown_qemu_identity_stops_before_changing_permissions(provisioner: Provisioner, label: str) -> None:
    cast(Mock, provisioner.vm.virsh).return_value.stdout = CAPABILITIES.replace("+959:+959", label)
    with pytest.raises(RuntimeError, match="Cannot determine QEMU"):
        provisioner.storage_access(provisioner.settings.image_dir / "test.iso")
    cast(Mock, provisioner.runner.run).assert_not_called()


def test_failed_access_repair_stops_before_disk_creation(provisioner: Provisioner) -> None:
    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], int("test" in args), "", "")

    cast(Mock, provisioner.runner.run).side_effect = run
    with pytest.raises(RuntimeError, match="still cannot access"):
        provisioner.install(provisioner.settings.image_dir / "test.iso")
    assert all("virt-install" not in call.args for call in cast(Mock, provisioner.runner.run).call_args_list)


def test_storage_is_prepared_before_virt_install(provisioner: Provisioner) -> None:
    provisioner.install(provisioner.settings.image_dir / "test.iso")
    commands = [call.args for call in cast(Mock, provisioner.runner.run).call_args_list]
    install_index = next(index for index, args in enumerate(commands) if "virt-install" in args)
    checks = [index for index, args in enumerate(commands) if "test" in args]
    assert checks and max(checks) < install_index

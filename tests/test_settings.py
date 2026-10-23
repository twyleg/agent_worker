from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_worker.main import Application
from agent_worker.settings import Settings


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "logs"))
    for name in ("DATA_DIR", "STATE_DIR", "IMAGE_DIR", "VM_NAME", "LIBVIRT_URI", "AGENT_USER", "CODEX_USER"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_user_directory_is_used_without_local_data(workspace: Path) -> None:
    settings = Settings.load(environ={})
    assert settings.data_dir == workspace / "home/.local/share/agent-worker"
    assert settings.vm_name == "agent-xubuntu"
    assert not settings.data_dir.exists()


def test_local_data_overrides_xdg_default(workspace: Path) -> None:
    (workspace / ".data").mkdir()
    settings = Settings.load(environ={"XDG_DATA_HOME": str(workspace / "xdg")})
    assert settings.data_dir == workspace / ".data"
    assert settings.vm_name != "agent-xubuntu"
    assert settings.state_dir == workspace / ".data/state"
    assert settings.image_dir == workspace / ".data/images"
    assert list(settings.data_dir.iterdir()) == []


def test_local_symlink_selects_same_vm_as_its_target(workspace: Path) -> None:
    target = workspace / "home/.local/share/agent-worker"
    target.mkdir(parents=True)
    (workspace / ".data").symlink_to(target, target_is_directory=True)
    selected = Settings.load(environ={})
    assert selected.data_dir == target
    assert selected.vm_name == Settings.load(overrides={"data_dir": target}, environ={}).vm_name == "agent-xubuntu"


def test_current_directory_only_is_searched(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (workspace / ".data").mkdir()
    child = workspace / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    assert Settings.load(environ={}).data_dir == workspace / "home/.local/share/agent-worker"


@pytest.mark.parametrize("kind", ["file", "broken-symlink"])
def test_invalid_local_data_does_not_silently_target_global_vm(workspace: Path, kind: str) -> None:
    local = workspace / ".data"
    if kind == "file":
        local.write_text("not a directory")
    else:
        local.symlink_to(workspace / "missing")
    with pytest.raises(ValueError, match="not a directory"):
        Settings.load(environ={})
    assert Settings.load(overrides={"data_dir": workspace / "explicit"}, environ={}).data_dir == workspace / "explicit"


def test_explicit_settings_keep_precedence_over_local_discovery(workspace: Path) -> None:
    (workspace / ".data").mkdir()
    config = {"data_dir": str(workspace / "config"), "vm_name": "configured"}
    env = {"DATA_DIR": str(workspace / "env"), "VM_NAME": "environment"}
    assert Settings.load(config, environ={}).data_dir == workspace / "config"
    assert Settings.load(config, environ=env).data_dir == workspace / "env"
    settings = Settings.load(config, {"data_dir": workspace / "cli", "vm_name": "explicit"}, environ=env)
    assert settings.data_dir == workspace / "cli"
    assert settings.vm_name == "explicit"


def test_two_data_directories_have_independent_vm_names_and_files(workspace: Path) -> None:
    first = Settings.load(overrides={"data_dir": workspace / "first/.data"}, environ={})
    second = Settings.load(overrides={"data_dir": workspace / "second/.data"}, environ={})
    assert first.vm_name != second.vm_name
    assert first.disk_path != second.disk_path
    assert first.ssh_key != second.ssh_key
    assert first.seed_dir != second.seed_dir
    assert Settings.load(overrides={"data_dir": first.data_dir}, environ={}).vm_name == first.vm_name


def test_saved_identity_survives_directory_move(workspace: Path) -> None:
    data = workspace / ".data"
    data.mkdir()
    original = Settings.load(overrides={"vm_name": "custom", "agent_user": "coder", "libvirt_uri": "qemu+ssh://host/system"}, environ={})
    original.save_identity()
    moved = workspace / "moved"
    data.rename(moved)
    restored = Settings.load(overrides={"data_dir": moved}, environ={})
    assert restored.vm_name == "custom"
    assert restored.agent_user == "coder"
    assert restored.libvirt_uri == "qemu+ssh://host/system"
    assert restored.state_dir == moved / "state"
    assert restored.image_dir == moved / "images"
    assert (moved / "vm.json").stat().st_mode & 0o777 == 0o600
    assert not list(moved.glob(".vm-*"))
    assert Settings.load({"vm_name": "configured"}, {"data_dir": moved}, environ={}).vm_name == "configured"
    assert Settings.load(overrides={"data_dir": moved}, environ={"VM_NAME": "env"}).vm_name == "env"
    assert Settings.load(overrides={"data_dir": moved, "vm_name": "cli"}, environ={"VM_NAME": "env"}).vm_name == "cli"


def test_existing_vm_is_adopted_from_disk_name(workspace: Path) -> None:
    images = workspace / ".data/images"
    images.mkdir(parents=True)
    (images / "previous-vm.qcow2").touch()
    assert Settings.load(environ={}).vm_name == "previous-vm"
    assert not (workspace / ".data/vm.json").exists()
    (images / "other.qcow2").touch()
    with pytest.raises(ValueError, match="Multiple VM disks"):
        Settings.load(environ={})
    assert Settings.load(overrides={"vm_name": "previous-vm"}, environ={}).vm_name == "previous-vm"


@pytest.mark.parametrize("identity", [[], {}, {"vm_name": "wrong"}, {"vm_name": 1, "libvirt_uri": "qemu:///system", "agent_user": "agent"}])
def test_invalid_identity_does_not_fall_back_to_another_vm(workspace: Path, identity: object) -> None:
    data = workspace / ".data"
    data.mkdir()
    (data / "vm.json").write_text(json.dumps(identity))
    with pytest.raises(ValueError, match="Invalid VM identity"):
        Settings.load(environ={})


def test_corrupt_identity_reports_its_path(workspace: Path) -> None:
    data = workspace / ".data"
    data.mkdir()
    identity = data / "vm.json"
    identity.write_text("{broken")
    with pytest.raises(ValueError, match="Invalid VM identity") as exc:
        Settings.load(environ={})
    assert str(identity) in str(exc.value)


def test_cli_targets_local_vm_without_repeated_options(workspace: Path) -> None:
    data = workspace / ".data"
    data.mkdir()
    selected = Settings.load(overrides={"vm_name": "my-project-vm"}, environ={})
    selected.save_identity()
    with patch("agent_worker.main.VM") as vm:
        assert Application().start(["vm", "status"]) == 0
        assert vm.call_args.args[0].data_dir == data
        assert vm.call_args.args[0].vm_name == "my-project-vm"


def test_changing_working_directory_changes_cli_target(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    targets = []
    for name in ("first-project", "second-project"):
        project = workspace / name
        (project / ".data").mkdir(parents=True)
        monkeypatch.chdir(project)
        with patch("agent_worker.main.VM") as vm:
            assert Application().start(["vm", "status"]) == 0
            targets.append(vm.call_args.args[0])
    assert targets[0].data_dir != targets[1].data_dir
    assert targets[0].vm_name != targets[1].vm_name

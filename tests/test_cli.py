from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from agent_worker import __version__
from agent_worker.main import Application, main
from agent_worker.settings import Settings
from agent_worker.system import CommandError, Runner
from agent_worker.vm import VM


def test_settings_precedence_and_legacy_aliases(tmp_path: Path) -> None:
    config = {"data_dir": str(tmp_path / "config"), "vm_name": "config-vm", "ram_mb": 1024}
    env = {
        "DATA_DIR": str(tmp_path / "env"),
        "VM_NAME": "env-vm",
        "CODEX_USER": "codex",
        "CODEX_EXPORT_DIR": str(tmp_path / "old-export"),
        "CODEX_SHARE_TAG": "old-tag",
        "RAM_MB": "2048",
    }
    settings = Settings.load(config, {"vm_name": "cli-vm", "ram_mb": 4096}, environ=env)
    assert settings.vm_name == "cli-vm"
    assert settings.ram_mb == 4096
    assert settings.agent_user == "codex"
    assert settings.state_dir == tmp_path / "env/state"
    assert settings.export_dir == tmp_path / "old-export"
    assert settings.share_tag == "old-tag"
    env["AGENT_USER"] = "agent"
    assert Settings.load(config, environ=env).agent_user == "agent"


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["vm"],
        ["vmm", "start"],
        ["vm", "start", "--typo"],
        ["vm", "destroy", "--yess"],
        ["project", "detach", "name", "--all"],
    ],
)
def test_parser_rejects_invalid_arguments(args: list[str]) -> None:
    application = Application()
    parser = application.root_command.parser
    application.add_arguments(parser)
    with pytest.raises((argparse.ArgumentError, SystemExit)) as exc:
        parser.parse_args(args)
    if isinstance(exc.value, SystemExit):
        assert exc.value.code == 2


def test_cli_help_and_version_output(capsys: pytest.CaptureFixture[str]) -> None:
    for args in (["--help"], ["vm", "--help"], ["vm", "create", "--help"], ["--version"]):
        with pytest.raises(SystemExit) as exc:
            main(args)
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert (__version__ if args == ["--version"] else "usage:") in output


def test_framework_config_schema_and_cli_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for key in ("DATA_DIR", "VM_NAME", "RAM_MB"):
        monkeypatch.delenv(key, raising=False)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"vm_name": "configured-vm", "data_dir": str(tmp_path / "data")}))
    with patch("agent_worker.main.VM") as vm:
        app = Application()
        assert app.start(["--config", str(config), "--vm-name", "cli-vm", "vm", "status"]) == 0
        settings = vm.call_args.args[0]
        assert settings.vm_name == "cli-vm"
        assert settings.state_dir == tmp_path / "data/state"
        assert app.application_config_filepath == str(config)
        assert app.application_config == {"vm_name": "configured-vm", "data_dir": str(tmp_path / "data")}
        assert app.application_config_filepath_source is app.ConfigFilepathSource.CLI_ARG
    config.write_text('{"unexpected": true}')
    with patch("agent_worker.main.VM") as vm:
        assert Application().start(["--config", str(config), "vm", "status"]) != 0
        vm.assert_not_called()


def test_missing_config_and_command_failure_exit_nonzero(tmp_path: Path) -> None:
    for args in (["--config", str(tmp_path / "missing"), "vm", "status"], ["--data-dir", str(tmp_path), "vm", "ssh"]):
        with pytest.raises(SystemExit) as exc:
            main(args)
        assert exc.value.code != 0


def test_remote_exit_status_is_preserved(tmp_path: Path) -> None:
    result = subprocess.CompletedProcess(["ssh"], 7, "", "remote failure")
    with patch.object(VM, "ssh", side_effect=CommandError(["ssh"], result)), pytest.raises(SystemExit) as exc:
        main(["vm", "ssh", "false"])
    assert exc.value.code == 7


def test_usage_json_stdout_is_unpolluted(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(VM, "usage", side_effect=lambda value: print('{"rateLimits": {}}')):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), pytest.raises(SystemExit) as exc:
            main(["codex", "usage", "--json"])
    assert exc.value.code == 0
    assert json.loads(stdout.getvalue()) == {"rateLimits": {}}
    assert capsys.readouterr().err.startswith("VM: ")


def test_vm_context_precedes_command_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with patch("agent_worker.main.VM") as vm:
        vm.return_value.status.side_effect = lambda: print("command started", file=sys.stderr)
        assert Application().start(["--data-dir", str(tmp_path), "--vm-name", "selected-vm", "vm", "status"]) == 0
    assert capsys.readouterr().err.splitlines() == [
        f"VM: selected-vm | Data: {tmp_path} | URI: qemu:///system",
        "command started",
    ]


def vm_with_runner(tmp_path: Path) -> tuple[VM, Mock]:
    settings = Settings.load(overrides={"data_dir": tmp_path}, environ={})
    runner = Mock(spec=Runner)
    runner.run.return_value = subprocess.CompletedProcess([], 0, "", "")
    return VM(settings, runner), runner


@pytest.mark.parametrize(("action", "state"), [("start", "running"), ("shutdown", "shut off")])
def test_idempotent_lifecycle(tmp_path: Path, action: str, state: str) -> None:
    vm, runner = vm_with_runner(tmp_path)
    runner.run.return_value.stdout = state + "\n"
    getattr(vm, action)()
    assert runner.run.call_count == 1


def test_ip_filters_guest_agent_addresses_and_falls_back_to_dhcp(tmp_path: Path) -> None:
    vm, runner = vm_with_runner(tmp_path)
    responses = [
        "lo - ipv4 127.0.0.1/8\neth0 - ipv4 169.254.1.2/16\neth0 - ipv4 0.1.2.3/8",
        "vnet0 network default virtio AA:BB:CC:DD:EE:FF",
        "2026-10-21 12:00:00 aa:bb:cc:dd:ee:ff ipv4 192.168.122.42/24 host -",
    ]
    runner.run.side_effect = [subprocess.CompletedProcess([], 0, output, "") for output in responses]
    assert vm.ip() == "192.168.122.42"
    for call in runner.run.call_args_list:
        assert call.args[:2] == ("virsh", "--readonly")


def test_destroy_decline_changes_nothing(tmp_path: Path) -> None:
    vm, runner = vm_with_runner(tmp_path)
    with patch("builtins.input", return_value="n"):
        vm.destroy()
    assert runner.run.call_count == 1


def test_installed_default_never_uses_site_packages_as_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_worker import settings

    monkeypatch.setattr(settings, "__file__", str(tmp_path / "site-packages/agent_worker/settings.py"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))
    assert settings.default_data_dir() == tmp_path / "user-data/agent-worker"


@pytest.mark.parametrize(
    "arguments,adapter,method,expected",
    [
        (["vm", "create", "--recreate", "--vcpus", "4"], "provisioner", "create", (True,)),
        (["vm", "start"], "vm", "start", ()),
        (["vm", "shutdown"], "vm", "shutdown", ()),
        (["vm", "status"], "vm", "status", ()),
        (["vm", "destroy", "--yes"], "vm", "destroy", (True,)),
        (["vm", "connect"], "vm", "connect", ()),
        (["project", "attach", "project with spaces", "alias"], "shares", "attach", ("project with spaces", "alias")),
        (["project", "detach", "alias"], "shares", "detach", ("alias", False)),
        (["project", "detach", "--all"], "shares", "detach", (None, True)),
        (["project", "status"], "shares", "status", (False, "auto")),
        (["codex", "usage", "--json"], "vm", "usage", (True,)),
        (["guest", "setup"], "provisioner", "setup_guest", ()),
    ],
)
def test_framework_dispatches_registered_commands(arguments: list[str], adapter: str, method: str, expected: tuple[object, ...]) -> None:
    from pathlib import Path

    if method == "attach":
        assert isinstance(expected[0], str)
        expected = (Path(expected[0]), expected[1])
    with patch("agent_worker.main.VM") as vm, patch("agent_worker.main.Provisioner") as provisioner, patch("agent_worker.main.Shares") as shares:
        assert Application().start(arguments) == 0
        target = {"vm": vm, "provisioner": provisioner, "shares": shares}[adapter]
        getattr(target.return_value, method).assert_called_once_with(*expected)
        if method == "create":
            assert vm.call_args.args[0].vcpus == 4


def test_framework_dispatches_ssh_with_remote_arguments_and_tty() -> None:
    with patch("agent_worker.main.VM") as vm:
        assert Application().start(["vm", "ssh", "--tty", "--", "python3", "-", "--json"]) == 0
        vm.return_value.ssh.assert_called_once_with("python3", "-", "--json", tty=True)


@pytest.mark.parametrize(
    ("options", "verbose", "color"),
    [([], False, "auto"), (["--verbose"], True, "auto"), (["-v", "--color", "always"], True, "always")],
)
def test_project_status_options(options: list[str], verbose: bool, color: str) -> None:
    with patch("agent_worker.main.Shares") as shares, pytest.raises(SystemExit) as exc:
        main(["project", "status", *options])
    assert exc.value.code == 0
    shares.return_value.status.assert_called_once_with(verbose, color)

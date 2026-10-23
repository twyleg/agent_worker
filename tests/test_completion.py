from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from agent_worker.completion import project_selectors
from agent_worker.settings import Settings
from agent_worker.shares import Share

ROOT = Path(__file__).resolve().parents[1]


def complete(line: str, tmp_path: Path, overrides: dict[str, str] | None = None) -> list[str]:
    output = tmp_path / "completions"
    environment = os.environ.copy()
    for field in fields(Settings):
        environment.pop(field.name.upper(), None)
    for name in ("CODEX_USER", "CODEX_EXPORT_DIR", "CODEX_SHARE_TAG"):
        environment.pop(name, None)
    environment.update(
        HOME=str(tmp_path / "home"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_DATA_HOME=str(tmp_path / "data"),
        PYTHONPATH=str(ROOT),
        _ARGCOMPLETE="1",
        _ARGCOMPLETE_SHELL="bash",
        _ARGCOMPLETE_SUPPRESS_SPACE="1",
        _ARGCOMPLETE_IFS="\n",
        _ARGCOMPLETE_STDOUT_FILENAME=str(output),
        COMP_LINE=line,
        COMP_POINT=str(len(line)),
    )
    environment.update(overrides or {})
    result = subprocess.run([sys.executable, "-m", "agent_worker"], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    return [shlex.split(candidate)[0] for candidate in output.read_text().splitlines()]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("agent_worker ", {"vm", "project", "codex", "guest", "completion"}),
        ("agent_worker vm ", {"create", "start", "shutdown", "status", "destroy", "connect", "ssh"}),
        ("agent_worker project ", {"attach", "detach", "status"}),
        ("agent_worker project status --", {"--help", "--verbose", "--color"}),
        ("agent_worker project status --color ", {"auto", "always", "never"}),
        ("agent_worker completion ", {"bash", "zsh", "fish"}),
    ],
)
def test_parser_completion(tmp_path: Path, line: str, expected: set[str]) -> None:
    assert expected <= set(complete(line, tmp_path))
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("argument", ["--data-dir", "--resource-dir", "project attach"])
def test_directory_completion_excludes_files(tmp_path: Path, argument: str) -> None:
    (tmp_path / "sample directory").mkdir()
    (tmp_path / "sample-file").write_text("not a directory")
    candidates = complete(f"agent_worker {argument} sample", tmp_path)
    assert candidates == ["sample directory/"]


@pytest.mark.parametrize("source", ["cwd", "home", "explicit", "environment", "cli", "local-data"])
def test_project_completion_uses_selected_settings(tmp_path: Path, source: str) -> None:
    data_dir = tmp_path / (".data" if source == "local-data" else "selected-data")
    record = data_dir / "state/shares/sample"
    Share(record, tmp_path / "sample source", "sample project", tmp_path / "export", "sample-tag", record / "device.xml").save()
    line = "agent_worker "
    environment = {}
    if source in ("cwd", "home", "explicit"):
        config_dir = tmp_path / "home" if source == "home" else tmp_path
        config_dir.mkdir(exist_ok=True)
        config_path = config_dir / ("custom.json" if source == "explicit" else "agent_worker_config.json")
        config_path.write_text(json.dumps({"data_dir": str(data_dir)}))
        if source == "explicit":
            line += f"--config {shlex.quote(str(config_path))} "
    elif source == "environment":
        environment["DATA_DIR"] = str(data_dir)
    elif source == "cli":
        # The CLI data directory overrides the value found in local configuration.
        (tmp_path / "agent_worker_config.json").write_text(json.dumps({"data_dir": str(tmp_path / "other-data")}))
        line += f"--data-dir {shlex.quote(str(data_dir))} "
    assert complete(line + "project detach sample", tmp_path, environment) == ["sample project"]
    assert complete(line + f"project detach {shlex.quote(str(tmp_path))}/sample", tmp_path, environment) == [str(tmp_path / "sample source")]
    assert not (tmp_path / "state").exists()
    assert not (data_dir / "state/project-shares.lock").exists()


def test_parser_completion_ignores_invalid_configuration(tmp_path: Path) -> None:
    (tmp_path / "agent_worker_config.json").write_text("not JSON")
    assert "status" in complete("agent_worker vm st", tmp_path)
    assert complete("agent_worker project detach missing", tmp_path) == []
    assert not (tmp_path / "state").exists()


def test_incomplete_share_records_do_not_break_completion(tmp_path: Path) -> None:
    record = tmp_path / "state/shares/incomplete"
    record.mkdir(parents=True)
    (record / "project.path").write_text("/missing/project")
    args = argparse.Namespace(data_dir=tmp_path)
    assert project_selectors("", args, None) == []


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_command_generates_shell_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], shell: str) -> None:
    from agent_worker.main import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        main(["completion", shell])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    script = captured.out
    assert captured.err == ""
    assert "agent_worker" in script
    if shell == "fish":
        assert "complete --command agent_worker" in script
    else:
        assert "_python_argcomplete" in script
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)


def test_fish_completion_loaded_in_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fish = shutil.which("fish")
    if fish is None:
        pytest.skip("Fish is not installed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "agent_worker"
    executable.write_text(f"#!{sys.executable}\nfrom agent_worker.main import main\nmain()\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "sample-file").touch()
    script = "agent_worker completion fish | source; complete -C 'agent_worker '; complete -C 'agent_worker vm st'"
    result = subprocess.run([fish, "--no-config", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=True, timeout=15)
    candidates = {line.split("\t", 1)[0] for line in result.stdout.splitlines()}
    assert {"vm", "project", "codex", "guest", "completion", "start", "status"} <= candidates
    assert "sample-file" not in candidates
    assert result.stderr == ""

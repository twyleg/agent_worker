from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from agent_worker.settings import Settings
from agent_worker.shares import Share, Shares
from agent_worker.system import Runner
from agent_worker.vm import VM


@pytest.fixture
def shares(tmp_path: Path) -> Shares:
    settings = Settings.load(overrides={"data_dir": tmp_path}, environ={})
    runner = Mock(spec=Runner)
    runner.run.return_value = subprocess.CompletedProcess([], 0, "", "")
    vm = VM(settings, runner)
    setattr(vm, "ssh", Mock(return_value=subprocess.CompletedProcess([], 0, "", "")))
    for name in ("first project", "second"):
        record = settings.state_dir / "shares" / name
        Share(record, tmp_path / "projects" / name, name, tmp_path / "exports" / name, name, record / "device.xml").save()
    return Shares(vm)


def test_compact_status_lists_one_row_per_project(shares: Shares, capsys: pytest.CaptureFixture[str]) -> None:
    shares.status()
    output = capsys.readouterr().out
    lines = output.splitlines()
    assert len(lines) == 3
    assert lines[0].split() == ["PROJECT", "HOST", "GUEST", "HOST", "PATH"]
    for share in shares.records():
        row = next(line for line in lines[1:] if line.startswith(share.name))
        assert row.count("mounted") == 2
        assert str(share.project) in row
        assert str(share.export) not in output
    assert "\033[" not in output


def test_verbose_status_preserves_mount_details(shares: Shares, capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(shares.runner, "run", wraps=shares.runner.run) as run:
        shares.status(verbose=True)
    output = capsys.readouterr().out
    for share in shares.records():
        assert f"Project:       {share.name}" in output
        assert f"Host project:  {share.project}" in output
        assert f"Staging dir:   {share.export}" in output
        assert f"Guest path:    {shares.guest_path(share)}" in output
        run.assert_any_call("findmnt", "-M", share.export)
    assert output.count("Guest mount:   mounted") == 2


@pytest.mark.parametrize(("returncode", "connection_error", "guest_state"), [(1, False, "not mounted"), (255, False, "unavailable"), (0, True, "unavailable")])
def test_status_distinguishes_missing_mounts_and_unreachable_guest(
    shares: Shares, capsys: pytest.CaptureFixture[str], returncode: int, connection_error: bool, guest_state: str
) -> None:
    with (
        patch.object(shares, "mounted", return_value=False),
        patch.object(
            shares.vm,
            "ssh",
            return_value=subprocess.CompletedProcess([], returncode, "", ""),
            side_effect=RuntimeError("VM is offline") if connection_error else None,
        ),
    ):
        shares.status(color="always")
    output = capsys.readouterr().out
    assert "\033[31mnot mounted" in output
    assert ("\033[33munavailable" if guest_state == "unavailable" else "\033[31mnot mounted") in output
    assert output.count(guest_state) == (2 if guest_state == "unavailable" else 4)


@pytest.mark.parametrize(
    ("color", "terminal", "no_color", "term", "expected"),
    [
        ("auto", True, False, "xterm", True),
        ("auto", False, False, "xterm", False),
        ("auto", True, True, "xterm", False),
        ("auto", True, False, "dumb", False),
        ("always", False, True, "dumb", True),
        ("never", True, False, "xterm", False),
    ],
)
def test_status_color_detection(
    shares: Shares,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    color: str,
    terminal: bool,
    no_color: bool,
    term: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("TERM", term)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)
    with patch("agent_worker.shares.sys.stdout.isatty", return_value=terminal):
        shares.status(color=color)
    output = capsys.readouterr().out
    assert ("\033[32m" in output) == expected


def test_status_without_projects(shares: Shares, capsys: pytest.CaptureFixture[str]) -> None:
    for share in shares.records():
        share.remove()
    shares.status()
    assert capsys.readouterr().out == "No projects are currently attached.\n"

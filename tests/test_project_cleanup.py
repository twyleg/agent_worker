from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from agent_worker.settings import Settings
from agent_worker.shares import Share, Shares
from agent_worker.system import Runner
from agent_worker.vm import VM


@pytest.fixture
def shares(tmp_path: Path) -> Shares:
    runner = Mock(spec=Runner)
    runner.run.return_value = subprocess.CompletedProcess([], 0, "", "")
    return Shares(VM(Settings.load(overrides={"data_dir": tmp_path / "data"}, environ={}), runner))


def save_share(shares: Shares, project: Path, export: Path) -> Share:
    record = shares.settings.state_dir / "shares/project"
    share = Share(record, project, "project", export, "project-tag", record / "device.xml")
    share.save()
    return share


def test_destroy_never_unmounts_direct_project_filesystem(shares: Shares, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    sentinel = project / "uncommitted.txt"
    sentinel.write_text("keep me")
    save_share(shares, project, project)
    shares.cleanup()
    assert sentinel.read_text() == "keep me"
    assert shares.records() == []
    assert all(str(project) not in map(str, call.args) for call in cast(Mock, shares.runner.run).call_args_list)


def test_cleanup_finds_old_staging_mounts_without_records(shares: Shares) -> None:
    export = Path(f"{shares.settings.export_dir}.d") / "orphan"
    export.mkdir(parents=True)
    shares.cleanup()
    cast(Mock, shares.runner.run).assert_any_call("sudo", "umount", "--", export)


def test_busy_cleanup_preserves_record_and_does_not_lazy_unmount(shares: Shares, tmp_path: Path) -> None:
    share = save_share(shares, tmp_path / "project", shares.settings.export_dir)

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("sudo", "umount"):
            raise RuntimeError("busy")
        return subprocess.CompletedProcess([], 0, "", "")

    cast(Mock, shares.runner.run).side_effect = run
    with pytest.raises(RuntimeError, match="busy"):
        shares.cleanup()
    assert (share.record / "project.path").is_file()
    assert all("-l" not in call.args for call in cast(Mock, shares.runner.run).call_args_list)


def test_shutdown_warns_about_surviving_old_mounts(shares: Shares, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    save_share(shares, tmp_path / "project", shares.settings.export_dir)
    shares.vm.shutdown()
    output = capsys.readouterr().out
    assert "bind mounts may survive VM shutdown" in output
    assert "project detach --all" in output
    assert "deletes original project files" in output


def test_damaged_share_tracking_does_not_prevent_shutdown(shares: Shares, tmp_path: Path) -> None:
    share = save_share(shares, tmp_path / "project", shares.settings.export_dir)
    (share.record / "export.path").unlink()
    shares.vm.shutdown()
    cast(Mock, shares.runner.run).assert_any_call("sudo", "virsh", "-c", shares.settings.libvirt_uri, "shutdown", shares.settings.vm_name)

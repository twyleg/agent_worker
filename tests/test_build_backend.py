from pathlib import Path
from unittest.mock import patch

import pytest

from tools._build_backend import build_sdist


@pytest.mark.parametrize("existing_link", [False, True])
def test_source_build_links_returned_archive(tmp_path: Path, existing_link: bool) -> None:
    archive_name = "agent_worker-1.2.3.tar.gz"
    (tmp_path / archive_name).write_bytes(b"source archive")
    latest_link = tmp_path / "agent_worker_latest.tar.gz"
    if existing_link:
        latest_link.symlink_to("agent_worker-previous.tar.gz")
    with patch("tools._build_backend.build_meta.build_sdist", return_value=archive_name) as build:
        assert build_sdist(str(tmp_path)) == archive_name
    build.assert_called_once_with(str(tmp_path), None)
    assert latest_link.readlink() == Path(archive_name)
    assert latest_link.read_bytes() == b"source archive"


def test_failed_source_build_preserves_previous_link(tmp_path: Path) -> None:
    latest_link = tmp_path / "agent_worker_latest.tar.gz"
    previous_archive = Path("agent_worker-previous.tar.gz")
    latest_link.symlink_to(previous_archive)
    with patch("tools._build_backend.build_meta.build_sdist", side_effect=RuntimeError("build failed")), pytest.raises(RuntimeError, match="build failed"):
        build_sdist(str(tmp_path))
    assert latest_link.readlink() == previous_archive

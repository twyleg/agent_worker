from pathlib import Path
from unittest.mock import patch

import pytest

from tools._build_backend import build_sdist


@pytest.mark.parametrize("existing_link", [False, True])
def test_source_build_links_returned_archive(tmp_path: Path, existing_link: bool) -> None:
    archive_name = "agent_worker-1.2.3.tar.gz"
    (tmp_path / archive_name).write_bytes(b"source archive")
    latest_link = tmp_path / "agent_worker-latest.tar.gz"
    if existing_link:
        latest_link.symlink_to("agent_worker-previous.tar.gz")
    with patch("tools._build_backend.build_meta.build_sdist", return_value=archive_name) as build:
        assert build_sdist(str(tmp_path)) == archive_name
    build.assert_called_once_with(str(tmp_path), None)
    assert latest_link.readlink() == Path(archive_name)
    assert latest_link.read_bytes() == b"source archive"


def test_failed_source_build_preserves_previous_link(tmp_path: Path) -> None:
    latest_link = tmp_path / "agent_worker-latest.tar.gz"
    previous_archive = Path("agent_worker-previous.tar.gz")
    latest_link.symlink_to(previous_archive)
    with patch("tools._build_backend.build_meta.build_sdist", side_effect=RuntimeError("build failed")), pytest.raises(RuntimeError, match="build failed"):
        build_sdist(str(tmp_path))
    assert latest_link.readlink() == previous_archive


@pytest.fixture(autouse=True)
def completion_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from tools import _build_backend

    directory = tmp_path / "completions"
    monkeypatch.setattr(_build_backend, "COMPLETIONS_DIR", directory)
    return directory


@pytest.mark.parametrize("hook", ["build_sdist", "build_wheel", "build_editable"])
def test_build_hooks_generate_completion_scripts_before_packaging(tmp_path: Path, completion_directory: Path, hook: str) -> None:
    from tools import _build_backend

    fish_script = completion_directory / "fish/agent_worker.fish"
    fish_script.parent.mkdir(parents=True)
    fish_script.write_text("obsolete script")
    arguments: tuple[object, ...] = (str(tmp_path), {"example": "value"})
    if hook != "build_sdist":
        arguments += ("metadata",)

    def package(*args: object) -> str:
        assert (completion_directory / "bash/agent_worker").is_file()
        assert (completion_directory / "zsh/_agent_worker").is_file()
        assert "complete --command agent_worker" in fish_script.read_text()
        return "agent_worker-1.2.3.tar.gz" if hook == "build_sdist" else "agent_worker-1.2.3-py3-none-any.whl"

    with patch.object(_build_backend.build_meta, hook, side_effect=package) as build:
        getattr(_build_backend, hook)(*arguments)
    build.assert_called_once_with(*arguments)


def test_distributions_ship_completion_files(tmp_path: Path) -> None:
    import subprocess
    import sys
    import tarfile
    import zipfile

    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(tmp_path)], cwd=root, capture_output=True, text=True, check=True, timeout=60
    )
    with zipfile.ZipFile(next(tmp_path.glob("*.whl"))) as wheel:
        for relative_path in ("bash-completion/completions/agent_worker", "zsh/site-functions/_agent_worker", "fish/vendor_completions.d/agent_worker.fish"):
            assert any(name.endswith(f".data/data/share/{relative_path}") for name in wheel.namelist())
    archive_path = next(path for path in tmp_path.glob("*.tar.gz") if not path.is_symlink())
    with tarfile.open(archive_path) as archive:
        for relative_path in ("bash/agent_worker", "zsh/_agent_worker", "fish/agent_worker.fish"):
            assert any(name.endswith(f"/completions/{relative_path}") for name in archive.getnames())

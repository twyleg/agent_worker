"""Generate shell integration, delegate packaging, and link the last source build."""

from pathlib import Path

import argcomplete
from setuptools import build_meta

COMPLETIONS_DIR = Path(__file__).resolve().parent.parent / "completions"
ConfigSettings = dict[str, str | list[str]] | None

get_requires_for_build_wheel = build_meta.get_requires_for_build_wheel
get_requires_for_build_editable = build_meta.get_requires_for_build_editable
get_requires_for_build_sdist = build_meta.get_requires_for_build_sdist
prepare_metadata_for_build_wheel = build_meta.prepare_metadata_for_build_wheel
prepare_metadata_for_build_editable = build_meta.prepare_metadata_for_build_editable


def _generate_completions() -> None:
    for shell, filename in (("bash", "agent_worker"), ("zsh", "_agent_worker"), ("fish", "agent_worker.fish")):
        path = COMPLETIONS_DIR / shell / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(argcomplete.shellcode(["agent_worker"], shell=shell), encoding="utf-8")


def build_wheel(wheel_directory: str, config_settings: ConfigSettings = None, metadata_directory: str | None = None) -> str:
    _generate_completions()
    return build_meta.build_wheel(wheel_directory, config_settings, metadata_directory)


def build_editable(wheel_directory: str, config_settings: ConfigSettings = None, metadata_directory: str | None = None) -> str:
    _generate_completions()
    return build_meta.build_editable(wheel_directory, config_settings, metadata_directory)


def build_sdist(sdist_directory: str, config_settings: ConfigSettings = None) -> str:
    _generate_completions()
    archive_name = build_meta.build_sdist(sdist_directory, config_settings)
    latest_link = Path(sdist_directory) / "agent_worker-latest.tar.gz"
    latest_link.unlink(missing_ok=True)
    latest_link.symlink_to(archive_name)
    return archive_name

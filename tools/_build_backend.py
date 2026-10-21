"""Delegate packaging to setuptools and keep a link to the last source build."""

from pathlib import Path

from setuptools import build_meta

build_wheel = build_meta.build_wheel
build_editable = build_meta.build_editable
get_requires_for_build_wheel = build_meta.get_requires_for_build_wheel
get_requires_for_build_editable = build_meta.get_requires_for_build_editable
get_requires_for_build_sdist = build_meta.get_requires_for_build_sdist
prepare_metadata_for_build_wheel = build_meta.prepare_metadata_for_build_wheel
prepare_metadata_for_build_editable = build_meta.prepare_metadata_for_build_editable


def build_sdist(sdist_directory: str, config_settings: dict[str, str | list[str]] | None = None) -> str:
    archive_name = build_meta.build_sdist(sdist_directory, config_settings)
    latest_link = Path(sdist_directory) / "agent_worker-latest.tar.gz"
    latest_link.unlink(missing_ok=True)
    latest_link.symlink_to(archive_name)
    return archive_name

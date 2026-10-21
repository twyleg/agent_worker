# Copyright (C) 2024 twyleg
from __future__ import annotations
from pathlib import Path

from setuptools import find_packages, setup

import versioneer

FILE_DIR = Path(__file__).parent


def read(relative_filepath: str) -> str:
    return (FILE_DIR / relative_filepath).read_text(encoding="utf-8")


# fmt: off
setup(
    name="agent_worker",
    version=versioneer.get_version(),
    cmdclass=versioneer.get_cmdclass(),
    author="Torsten Wylegala",
    author_email="mail@twyleg.de",
    description="Manage an isolated Xubuntu development VM for coding agents",
    license="GPL 3.0",
    keywords="codex vm qemu libvirt development",
    url="https://github.com/twyleg/agent_worker",
    packages=find_packages(),
    python_requires=">=3.11",
    long_description=read("README.md"),
    long_description_content_type="text/markdown",
    include_package_data=True,
    data_files=[
        ("share/bash-completion/completions", ["completions/bash/agent_worker"]),
        ("share/zsh/site-functions", ["completions/zsh/_agent_worker"]),
        ("share/fish/vendor_completions.d", ["completions/fish/agent_worker.fish"]),
    ],
    install_requires=[
        "simple-python-app==0.4.1",
        "PyYAML~=6.0.2",
        "argcomplete~=3.7.2",
    ],
    entry_points={
        "console_scripts": [
            "agent_worker = agent_worker.main:main"
        ]
    },
)

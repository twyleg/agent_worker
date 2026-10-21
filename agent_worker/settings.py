"""Settings shared by lifecycle, SSH, provisioning and project sharing."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Self

RESOURCE_DIR = Path(__file__).parent / "resources"


def default_data_dir() -> Path:
    # Source checkouts retain their existing data; wheels never write into site-packages.
    root = Path(__file__).resolve().parent.parent
    if (root / "setup.py").is_file() and (root / "scripts").is_dir():
        return root / ".data"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "agent-worker"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    state_dir: Path
    image_dir: Path
    export_dir: Path
    resource_dir: Path = RESOURCE_DIR
    vm_name: str = "agent-xubuntu"
    agent_user: str = "agent"
    libvirt_uri: str = "qemu:///system"
    share_tag: str = "agent-project"
    workspace_helper_prefix: str = "agent-workspace"
    ram_mb: int = 12288
    vcpus: int = 6
    disk_gb: int = 64
    xubuntu_series: str = "26.04"
    timezone: str = "Europe/Berlin"
    locale: str = "en_US.UTF-8"
    keyboard_layout: str = "de"
    keyboard_variant: str = ""
    guest_hostname: str = "agent-worker"
    ready_timeout: int = 900

    @classmethod
    def load(cls, config: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None, environ: Mapping[str, str] | None = None) -> Self:
        env = os.environ if environ is None else environ
        values = dict(config or {})
        aliases = {
            "agent_user": ("AGENT_USER", "CODEX_USER"),
            "export_dir": ("AGENT_EXPORT_DIR", "CODEX_EXPORT_DIR"),
            "share_tag": ("AGENT_SHARE_TAG", "CODEX_SHARE_TAG"),
        }
        for field in fields(cls):
            for name in aliases.get(field.name, (field.name.upper(),)):
                if env.get(name):
                    values[field.name] = env[name]
                    break
        values.update({key: value for key, value in (overrides or {}).items() if value is not None})
        values.setdefault("data_dir", default_data_dir())
        values["data_dir"] = Path(values["data_dir"]).expanduser().resolve()
        for name, default in (("state_dir", "state"), ("image_dir", "images"), ("export_dir", "share/current")):
            values.setdefault(name, values["data_dir"] / default)
        for name in ("state_dir", "image_dir", "export_dir", "resource_dir"):
            if name in values:
                values[name] = Path(values[name]).expanduser().resolve()
        for name in ("ram_mb", "vcpus", "disk_gb", "ready_timeout"):
            if name in values:
                values[name] = int(values[name])
                if values[name] <= 0:
                    raise ValueError(f"{name} must be positive")
        result = cls(**values)
        for name in ("vm_name", "agent_user", "guest_hostname", "workspace_helper_prefix"):
            if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*", getattr(result, name)):
                raise ValueError(f"Invalid {name}")
        if not re.fullmatch(r"\d+\.\d+", result.xubuntu_series):
            raise ValueError("Invalid xubuntu_series")
        return result

    @property
    def ssh_key(self) -> Path:
        return self.state_dir / "id_ed25519"

    @property
    def known_hosts(self) -> Path:
        return self.state_dir / "known_hosts"

    @property
    def disk_path(self) -> Path:
        return self.image_dir / f"{self.vm_name}.qcow2"

    @property
    def password_file(self) -> Path:
        return self.state_dir / f"{self.vm_name}.password"

    @property
    def seed_dir(self) -> Path:
        return self.state_dir / "autoinstall"

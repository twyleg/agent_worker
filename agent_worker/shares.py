"""Independent, recoverable virtiofs shares using the original tracking format."""

from __future__ import annotations

import hashlib
import os
import shlex
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from agent_worker.system import file_lock
from agent_worker.vm import GUEST_DIR, VM


@dataclass
class Share:
    record: Path
    project: Path
    name: str
    export: Path
    tag: str
    xml: Path
    legacy: bool = False

    def write_xml(self) -> None:
        fs = ET.Element("filesystem", type="mount", accessmode="passthrough")
        ET.SubElement(fs, "driver", type="virtiofs", queue="1024")
        ET.SubElement(fs, "source", dir=str(self.export))
        ET.SubElement(fs, "target", dir=self.tag)
        ET.ElementTree(fs).write(self.xml, encoding="unicode")

    def save(self) -> None:
        self.record.mkdir(parents=True, exist_ok=True)
        for name, value in (
            ("project.path", self.project),
            ("project.name", self.name),
            ("export.path", self.export),
            ("device.tag", self.tag),
        ):
            (self.record / name).write_text(f"{value}\n")
        self.write_xml()

    def remove(self) -> None:
        if self.legacy:
            self.xml.unlink(missing_ok=True)
            (self.record / "current-share.path").unlink(missing_ok=True)
        else:
            for name in ("project.path", "project.name", "export.path", "device.tag", "device.xml", "device.detached"):
                (self.record / name).unlink(missing_ok=True)
            self.record.rmdir()


class Shares:
    def __init__(self, vm: VM) -> None:
        self.vm = vm
        self.settings = vm.settings
        self.runner = vm.runner

    def records(self) -> list[Share]:
        state = self.settings.state_dir
        result = []
        if (state / "current-share.path").is_file():
            project = Path((state / "current-share.path").read_text().rstrip("\n"))
            result.append(
                Share(
                    state,
                    project,
                    project.name,
                    self.settings.export_dir,
                    self.settings.share_tag,
                    state / "current-share.xml",
                    legacy=True,
                )
            )
        for path in sorted((state / "shares").glob("*/project.path")):
            record = path.parent

            def read(name: str) -> str:
                return (record / name).read_text().rstrip("\n")

            result.append(
                Share(
                    record,
                    Path(read("project.path")),
                    read("project.name"),
                    Path(read("export.path")),
                    read("device.tag"),
                    record / "device.xml",
                )
            )
        return result

    def mounted(self, path: Path) -> bool:
        return self.runner.run("findmnt", "-rn", "-M", path, capture=True, check=False).returncode == 0

    def require_running(self) -> None:
        state = self.vm.virsh("domstate", self.settings.vm_name, readonly=True, capture=True).stdout.strip()
        if state != "running":
            raise RuntimeError(f"VM '{self.settings.vm_name}' is not running.")

    def live_export(self, share: Share) -> Path | None:
        result = self.vm.virsh("dumpxml", self.settings.vm_name, readonly=True, capture=True)
        root = ET.fromstring(result.stdout)
        matches = [fs for fs in root.findall("./devices/filesystem") if (target := fs.find("target")) is not None and target.get("dir") == share.tag]
        if len(matches) > 1:
            raise RuntimeError("Multiple live devices use the tracked project tag")
        if not matches:
            return None
        fs = matches[0]
        source = fs.find("source")
        driver = fs.find("driver")
        if driver is None or driver.get("type") != "virtiofs" or source is None or not source.get("dir"):
            raise RuntimeError("The tracked tag belongs to a different filesystem type or has no directory source")
        directory = source.get("dir")
        assert directory is not None
        export = Path(directory)
        if export.resolve() != share.export.resolve():
            raise RuntimeError(f"Tag '{share.tag}' is used by a different export: {export}")
        return export

    def guest_path(self, share: Share) -> str:
        return f"/home/{self.settings.agent_user}/workspace/{share.name}"

    def guest(self, share: Share, action: str) -> None:
        workspace = f"/home/{self.settings.agent_user}/workspace"
        extra = workspace if action == "mount" or share.legacy else ""
        command = shlex.join(["sudo", "python3", "-", action, self.guest_path(share), share.tag, extra])
        self.vm.ssh(command, input=(GUEST_DIR / "project.py").read_text())

    def stage(self, share: Share) -> None:
        self.runner.run("sudo", "install", "-d", "-m", "0755", "--", share.export)
        self.runner.run("sudo", "mount", "--bind", "--", share.project, share.export)

    def attach(self, project: str | Path, name: str | None = None) -> None:
        with file_lock(self.settings.state_dir / "project-shares.lock"):
            self.require_running()
            project = Path(project).expanduser().resolve(strict=True)
            name = name or project.name
            if not project.is_dir():
                raise ValueError(f"Not a directory: {project}")
            if not name or name in (".", "..") or "/" in name or "\n" in name or "\n" in str(project):
                raise ValueError("Invalid project name or path.")
            for share in self.records():
                if share.project == project:
                    if share.name != name:
                        raise RuntimeError(f"Project is already attached as '{share.name}'.")
                    if not share.legacy and (share.record / "device.detached").exists():
                        raise RuntimeError(f"Project detach is incomplete; finish detaching '{name}' before attaching again.")
                    live = self.live_export(share)
                    if not self.mounted(share.export):
                        self.stage(share)
                    if live is None:
                        share.write_xml()
                        self.vm.virsh("attach-device", self.settings.vm_name, share.xml, "--live")
                    self.guest(share, "mount")
                    print(f"Already attached: {project} -> {self.guest_path(share)}")
                    return
                if share.name == name:
                    raise RuntimeError(f"Guest project name '{name}' is already used. Pass a different NAME to attach.")
            identity = hashlib.sha256(str(project).encode()).hexdigest()
            record = self.settings.state_dir / "shares" / identity
            share = Share(
                record,
                project,
                name,
                Path(f"{self.settings.export_dir}.d") / identity,
                f"project-{identity[:28]}",
                record / "device.xml",
            )
            if self.mounted(share.export):
                raise RuntimeError(f"{share.export} is already mounted; clean up the stale export first.")
            share.save()
            try:
                self.stage(share)
                self.vm.virsh("attach-device", self.settings.vm_name, share.xml, "--live")
            except Exception:
                (record / "device.detached").touch()
                if self.mounted(share.export):
                    # If rollback fails, preserve tracking so detach can recover it.
                    self.runner.run("sudo", "umount", "--", share.export)
                share.remove()
                raise
            try:
                self.guest(share, "mount")
            except Exception as exc:
                raise RuntimeError(
                    f"Device attached, but guest mounting failed. Retry: agent_worker project attach " f"{shlex.quote(str(project))} {shlex.quote(name)}"
                ) from exc
            print(f"Ready: {project} -> {self.guest_path(share)}")

    def detach(self, selector: str | None = None, all: bool = False) -> None:
        with file_lock(self.settings.state_dir / "project-shares.lock"):
            self.require_running()
            records = self.records()
            if not records:
                raise RuntimeError("No tracked project shares are attached.")
            if not all:
                if selector is None:
                    if len(records) != 1:
                        raise RuntimeError("Multiple projects are attached. Use detach PATH_OR_NAME or detach --all.")
                else:
                    canonical = Path(selector).expanduser().resolve()
                    records = [share for share in records if share.name == selector or share.project == canonical]
                    if not records:
                        raise RuntimeError(f"No attached project matches '{selector}'.")
                    if len(records) > 1:
                        raise RuntimeError("Ambiguous project selector; use the full host path.")
            for share in records:
                live = self.live_export(share)
                self.guest(share, "unmount")
                if live is not None:
                    self.vm.virsh("detach-device", self.settings.vm_name, share.xml, "--live")
                if not share.legacy:
                    (share.record / "device.detached").touch()
                if self.mounted(share.export):
                    self.runner.run("sudo", "umount", "--", share.export)
                share.remove()
                print(f"Detached: {share.project}")

    def status(self, verbose: bool = False, color: str = "auto") -> None:
        use_color = color == "always" or (color == "auto" and sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb")

        def highlight(state: str, width: int = 0) -> str:
            text = state.ljust(width)
            if not use_color:
                return text
            code = {"mounted": "32", "not mounted": "31", "unavailable": "33"}[state]
            return f"\033[{code}m{text}\033[0m"

        with file_lock(self.settings.state_dir / "project-shares.lock"):
            records = self.records()
            if not records:
                print("No projects are currently attached.")
                return
            name_width = max(len("PROJECT"), *(len(share.name) for share in records))
            if not verbose:
                print(f"{'PROJECT':<{name_width}}  {'HOST':<11}  {'GUEST':<11}  HOST PATH")
            for share in records:
                host_mounted = self.mounted(share.export)
                if verbose:
                    print(
                        f"Project:       {share.name}\nHost project:  {share.project}\nStaging dir:   {share.export}"
                        f"\nGuest path:    {self.guest_path(share)}"
                    )
                    if host_mounted:
                        self.runner.run("findmnt", "-M", share.export)
                try:
                    result = self.vm.ssh(
                        shlex.join(["findmnt", "-rn", "-M", self.guest_path(share), "-t", "virtiofs"]),
                        capture=True,
                        check=False,
                    )
                    if result.returncode == 255:
                        guest_state = "unavailable"
                    else:
                        guest_state = "mounted" if result.returncode == 0 else "not mounted"
                except (RuntimeError, OSError):
                    guest_state = "unavailable"
                if verbose:
                    print(f"Guest mount:   {highlight(guest_state)}")
                else:
                    host_state = "mounted" if host_mounted else "not mounted"
                    print(f"{share.name:<{name_width}}  {highlight(host_state, 11)}  {highlight(guest_state, 11)}  {share.project}")

    def cleanup(self) -> None:
        # Only call after domain removal, when no guest can access these exports.
        for share in self.records():
            self._unmount_stale(share.export)
            share.remove()
        self._unmount_stale(self.settings.export_dir)
        for name in ("current-share.xml", "current-share.path"):
            (self.settings.state_dir / name).unlink(missing_ok=True)

    def _unmount_stale(self, path: Path) -> None:
        if self.mounted(path):
            if self.runner.run("sudo", "umount", "--", path, check=False).returncode:
                self.runner.run("sudo", "umount", "-l", "--", path)

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

MOCK = """#!/usr/bin/env python3
import json, os, pathlib, shlex, sys, xml.etree.ElementTree as ET
name = pathlib.Path(sys.argv[0]).name
args = [arg for arg in sys.argv[1:] if arg != '--']
statefile = pathlib.Path(os.environ['MOCK_STATE'])
state = json.loads(statefile.read_text())
def save():
    statefile.write_text(json.dumps(state))
if name == 'sudo':
    os.execvp(args[0], args)
elif name == 'virsh':
    if 'domstate' in args:
        print(os.environ.get('VM_STATE', 'running'))
    elif 'dominfo' in args:
        if os.environ.get('VM_UNDEFINED'): sys.exit(1)
        print('Name: agent-xubuntu')
    elif 'domifaddr' in args:
        print('vnet0 aa:bb:cc:dd:ee:ff ipv4 192.168.122.10/24')
    elif 'dumpxml' in args:
        if os.environ.get('INSPECT_FAIL'): sys.exit(1)
        root = ET.Element('domain')
        devices = ET.SubElement(root, 'devices')
        for tag, export in state['devices'].items():
            fs = ET.SubElement(devices, 'filesystem')
            ET.SubElement(fs, 'driver', type='virtiofs')
            ET.SubElement(fs, 'source', dir=export)
            ET.SubElement(fs, 'target', dir=tag)
        print(ET.tostring(root, encoding='unicode'))
    elif 'attach-device' in args or 'detach-device' in args:
        action = 'attach-device' if 'attach-device' in args else 'detach-device'
        xml = ET.parse(args[args.index(action) + 2]).getroot()
        tag = xml.find('target').get('dir')
        if action == 'attach-device':
            if os.environ.get('ATTACH_FAIL'): sys.exit(1)
            if tag in state['devices']: sys.exit(2)
            state['devices'][tag] = xml.find('source').get('dir')
        else:
            if tag not in state['devices']: sys.exit(3)
            del state['devices'][tag]
        save()
    else:
        sys.exit(98)
elif name == 'mount':
    raise AssertionError('Project sharing must never create host mounts')
elif name == 'umount':
    if os.environ.get('HOST_BUSY'): sys.exit(1)
    state['exports'].pop(args[-1], None)
    save()
elif name == 'findmnt':
    path = args[args.index('-M') + 1] if '-M' in args else args[-1]
    sys.exit(0 if path in state['exports'] else 1)
elif name == 'ssh':
    body = sys.stdin.read()
    command = shlex.split(args[-1])
    if 'python3' not in command:
        print('Guest mount: mounted')
        sys.exit(0)
    action, path, tag, extra = command[command.index('-') + 1:]
    if action == 'unmount':
        if os.environ.get('GUEST_BUSY'): sys.exit(1)
        for candidate in (path, extra):
            if state['guest'].get(candidate) == tag:
                del state['guest'][candidate]
    else:
        if os.environ.get('GUEST_MOUNT_FAIL'): sys.exit(1)
        assert tag in state['devices'], (tag, state)
        state['guest'][path] = tag
    save()
else:
    sys.exit(99)
"""


class ProjectShareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        for name in ("sudo", "virsh", "mount", "umount", "findmnt", "ssh"):
            path = self.bin / name
            path.write_text(MOCK)
            path.chmod(0o755)
        self.data = self.directory / "data with spaces & apostrophe'"
        self.state_dir = self.data / "state"
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "id_ed25519").touch()
        self.statefile = self.directory / "mock.json"
        self.statefile.write_text(json.dumps({"exports": {}, "devices": {}, "guest": {}}))
        self.env = dict(
            os.environ,
            DATA_DIR=str(self.data),
            STATE_DIR=str(self.state_dir),
            MOCK_STATE=str(self.statefile),
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
        )
        for key in (
            "AGENT_EXPORT_DIR",
            "CODEX_EXPORT_DIR",
            "AGENT_SHARE_TAG",
            "CODEX_SHARE_TAG",
            "AGENT_USER",
            "CODEX_USER",
        ):
            self.env.pop(key, None)
        self.first = self.directory / "projects/first project"
        self.second = self.directory / "projects/second"
        self.first.mkdir(parents=True)
        self.second.mkdir()

    def run_cli(self, *args: str | Path, success: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, "-m", "agent_worker", "project", *map(str, args)],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=10,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def state(self) -> dict[str, dict[str, str]]:
        return json.loads(self.statefile.read_text())

    def records(self) -> list[Path]:
        return list((self.state_dir / "shares").glob("*/project.path"))

    def test_multiple_projects_and_independent_detach(self) -> None:
        self.run_cli("attach", self.first)
        self.run_cli("attach", self.second)
        state = self.state()
        self.assertEqual(len(state["devices"]), 2)
        self.assertEqual(state["exports"], {})
        self.assertEqual(set(state["devices"].values()), {str(self.first), str(self.second)})
        self.assertEqual(len(state["guest"]), 2)
        for tag in state["devices"]:
            self.assertLessEqual(len(tag), 36)
        report = self.run_cli("status").stdout
        self.assertIn(str(self.first), report)
        self.assertIn(str(self.second), report)
        ambiguous = self.run_cli("detach", success=False)
        self.assertIn("Multiple projects", ambiguous.stderr)
        self.assertEqual(self.state(), state)
        self.run_cli("detach", "first project")
        self.assertEqual(list(self.state()["devices"].values()), [str(self.second)])
        self.assertEqual(len(self.state()["guest"]), 1)
        self.run_cli("detach")
        self.assertEqual(self.state(), {"exports": {}, "devices": {}, "guest": {}})
        self.assertEqual(self.records(), [])

    def test_duplicate_name_alias_and_repeated_attach(self) -> None:
        other = self.directory / "elsewhere/first project"
        other.mkdir(parents=True)
        self.run_cli("attach", self.first)
        self.run_cli("attach", self.first)
        self.assertEqual(len(self.state()["devices"]), 1)
        result = self.run_cli("attach", other, success=False)
        self.assertIn("already used", result.stderr)
        self.assertEqual(len(self.state()["devices"]), 1)
        self.run_cli("attach", other, "other-project")
        self.assertIn("/home/agent/workspace/other-project", self.state()["guest"])
        self.run_cli("detach", other)
        self.assertEqual(list(self.state()["devices"].values()), [str(self.first)])
        self.run_cli("detach", "--all")
        self.assertEqual(self.records(), [])

    def test_guest_mount_failure_can_be_retried(self) -> None:
        self.env["GUEST_MOUNT_FAIL"] = "1"
        self.run_cli("attach", self.first, success=False)
        self.assertEqual(len(self.state()["devices"]), 1)
        self.env.pop("GUEST_MOUNT_FAIL")
        self.run_cli("attach", self.first)
        self.assertEqual(len(self.state()["devices"]), 1)
        self.assertEqual(len(self.state()["guest"]), 1)

    def simulate_restart(self, host: bool = False) -> None:
        state = self.state()
        state["devices"] = {}
        state["guest"] = {}
        if host:
            state["exports"] = {}
        self.statefile.write_text(json.dumps(state))

    def test_attach_restores_device_after_vm_restart(self) -> None:
        self.run_cli("attach", self.first)
        self.run_cli("attach", self.second)
        before = self.state()
        self.simulate_restart()
        self.run_cli("attach", self.first)
        self.run_cli("attach", self.second)
        self.assertEqual(self.state(), before)

    def test_old_share_requires_explicit_detach_and_reattach(self) -> None:
        self.make_legacy_share()
        self.simulate_restart(host=True)
        before = self.state()
        result = self.run_cli("attach", self.first, success=False)
        self.assertIn("old host bind mount", result.stderr)
        self.assertEqual(self.state(), before)
        self.run_cli("detach", self.first)
        self.run_cli("attach", self.first)
        self.assertEqual(list(self.state()["devices"].values()), [str(self.first)])
        self.assertEqual(self.state()["exports"], {})

    def test_detach_cleans_records_after_restart(self) -> None:
        self.make_legacy_share()
        self.run_cli("attach", self.second)
        self.simulate_restart()
        self.run_cli("detach", "--all")
        self.assertEqual(self.state(), {"exports": {}, "devices": {}, "guest": {}})
        self.assertEqual(self.records(), [])
        self.assertFalse((self.state_dir / "current-share.path").exists())

    def test_live_inspection_failure_preserves_share(self) -> None:
        self.run_cli("attach", self.first)
        before = self.state()
        self.env["INSPECT_FAIL"] = "1"
        self.run_cli("attach", self.first, success=False)
        self.run_cli("detach", self.first, success=False)
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.records()), 1)

    def test_conflicting_live_tag_is_not_reused_or_detached(self) -> None:
        self.run_cli("attach", self.first)
        state = self.state()
        tag = next(iter(state["devices"]))
        state["devices"][tag] = str(self.second)
        self.statefile.write_text(json.dumps(state))
        self.run_cli("attach", self.first, success=False)
        self.run_cli("detach", self.first, success=False)
        self.assertEqual(self.state(), state)

    def test_attach_failure_rolls_back_only_new_project(self) -> None:
        self.run_cli("attach", self.first)
        before = self.state()
        self.env["ATTACH_FAIL"] = "1"
        self.run_cli("attach", self.second, success=False)
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.records()), 1)

    def test_busy_guest_does_not_detach_devices(self) -> None:
        self.run_cli("attach", self.first)
        before = self.state()
        self.env["GUEST_BUSY"] = "1"
        self.run_cli("detach", self.first, success=False)
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.records()), 1)

    def test_host_unmount_failure_can_be_retried(self) -> None:
        self.make_staged_share()
        self.env["HOST_BUSY"] = "1"
        self.run_cli("detach", self.first, success=False)
        self.assertEqual(self.state()["devices"], {})
        self.assertEqual(len(self.records()), 1)
        self.env.pop("HOST_BUSY")
        self.run_cli("detach", self.first)
        self.assertEqual(self.state()["exports"], {})
        self.assertEqual(self.records(), [])

    def make_staged_share(self) -> None:
        self.run_cli("attach", self.first)
        record = self.records()[0].parent
        export = self.data / "share/current.d" / record.name
        export.mkdir(parents=True)
        (record / "export.path").write_text(str(export) + "\n")
        xml_path = record / "device.xml"
        root = ET.parse(xml_path).getroot()
        source = root.find("source")
        assert source is not None
        source.set("dir", str(export))
        ET.ElementTree(root).write(xml_path)
        state = self.state()
        tag = next(iter(state["devices"]))
        state["devices"][tag] = str(export)
        state["exports"][str(export)] = str(self.first)
        self.statefile.write_text(json.dumps(state))

    def test_staged_share_is_not_reused(self) -> None:
        self.make_staged_share()
        before = self.state()
        self.run_cli("attach", self.first, success=False)
        self.assertEqual(self.state(), before)
        self.run_cli("detach", self.first)
        self.run_cli("attach", self.first)
        self.assertEqual(self.state()["exports"], {})

    def test_attach_refuses_orphan_staging_mount(self) -> None:
        self.make_staged_share()
        shutil.rmtree(self.state_dir / "shares")
        before = self.state()
        result = self.run_cli("attach", self.first, success=False)
        self.assertIn("Old bind mount remains", result.stderr)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.records(), [])

    def test_detach_stopped_vm_needs_no_guest_access(self) -> None:
        self.make_staged_share()
        self.simulate_restart()
        self.env["VM_STATE"] = "shut off"
        self.env["GUEST_BUSY"] = "1"
        self.env["INSPECT_FAIL"] = "1"
        self.run_cli("detach", "--all")
        self.assertEqual(self.records(), [])
        self.assertEqual(self.state()["exports"], {})

    def test_detach_refuses_vm_transition(self) -> None:
        self.make_staged_share()
        before = self.state()
        self.env["VM_STATE"] = "in shutdown"
        self.run_cli("detach", "--all", success=False)
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.records()), 1)

    def test_runtime_data_removal_preserves_project_files(self) -> None:
        sentinel = self.first / "uncommitted.txt"
        sentinel.write_text("irreplaceable work")
        self.run_cli("attach", self.first)
        record = self.records()[0].parent
        self.assertEqual((record / "export.path").read_text().strip(), str(self.first))
        source = ET.parse(record / "device.xml").find("source")
        assert source is not None
        self.assertEqual(source.get("dir"), str(self.first))
        self.assertFalse((self.data / "share").exists())
        shutil.rmtree(self.data)
        self.assertEqual(sentinel.read_text(), "irreplaceable work")

    def test_project_mountpoint_is_never_unmounted(self) -> None:
        self.run_cli("attach", self.first)
        state = self.state()
        state["exports"][str(self.first)] = "/some/dedicated/project/filesystem"
        self.statefile.write_text(json.dumps(state))
        self.run_cli("detach", self.first)
        self.assertEqual(self.state()["exports"], state["exports"])

    def make_legacy_share(self) -> None:
        export = str(self.data / "share/current")
        tag = "agent-project"
        (self.state_dir / "current-share.path").write_text(str(self.first) + "\n")
        root = ET.Element("filesystem")
        ET.SubElement(root, "source", dir=export)
        ET.SubElement(root, "target", dir=tag)
        ET.ElementTree(root).write(self.state_dir / "current-share.xml")
        self.statefile.write_text(
            json.dumps(
                {
                    "exports": {export: str(self.first)},
                    "devices": {tag: export},
                    "guest": {"/home/agent/workspace/first project": tag},
                }
            )
        )

    def test_legacy_share_can_coexist_and_detach_independently(self) -> None:
        self.make_legacy_share()
        self.run_cli("attach", self.second)
        self.assertEqual(len(self.state()["devices"]), 2)
        self.run_cli("detach", self.first)
        self.assertFalse((self.state_dir / "current-share.path").exists())
        self.assertEqual(self.state()["exports"], {})
        self.run_cli("detach", "--all")

    def test_destroy_cleans_all_exports_and_tracking(self) -> None:
        self.make_legacy_share()
        self.run_cli("attach", self.second)
        # Destroy's undefined-VM path cleans leftovers without prompting.
        self.env["VM_UNDEFINED"] = "1"
        result = subprocess.run(
            [sys.executable, "-m", "agent_worker", "vm", "destroy"],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state()["exports"], {})
        self.assertEqual(self.records(), [])
        self.assertFalse((self.state_dir / "current-share.path").exists())

    def test_vm_status_lists_all_projects_without_sudo(self) -> None:
        self.run_cli("attach", self.first)
        self.run_cli("attach", self.second)
        # Status can use only the read-only virsh mock and local metadata.
        (self.bin / "sudo").write_text("#!/bin/sh\nexit 99\n")
        result = subprocess.run(
            [sys.executable, "-m", "agent_worker", "vm", "status"],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.first), result.stdout)
        self.assertIn(str(self.second), result.stdout)


if __name__ == "__main__":
    unittest.main()

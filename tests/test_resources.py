from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

import yaml

from agent_worker.provision import autoinstall, load_packages
from agent_worker.settings import RESOURCE_DIR, Settings


class ResourceTests(unittest.TestCase):
    def test_generated_cloud_init_contains_resources_and_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.load(overrides={"data_dir": directory, "agent_user": "test-agent"}, environ={})
            settings.state_dir.mkdir(parents=True)
            settings.ssh_key.with_suffix(".pub").write_text("ssh-ed25519 test-key")
            packages = load_packages(RESOURCE_DIR / "packages_list")
            autoinstall(settings, packages, "test-password-hash")
            data = yaml.safe_load((settings.seed_dir / "user-data").read_text())["autoinstall"]
            self.assertEqual(data["packages"], packages)
            self.assertEqual(data["identity"]["password"], "test-password-hash")
            self.assertFalse(data["ssh"]["allow-pw"])
            self.assertEqual(data["ssh"]["authorized-keys"], ["ssh-ed25519 test-key"])
            files = {item["path"]: item for item in data["user-data"]["write_files"]}
            for name, target in (
                ("tmux.conf", ".tmux.conf"),
                ("tmux.conf.local", ".tmux.conf.local"),
                ("codex.config.toml", ".codex/config.toml"),
            ):
                item = files[f"/home/test-agent/{target}"]
                self.assertEqual(base64.b64decode(item["content"]), (RESOURCE_DIR / name).read_bytes())
            for action in ("mount", "unmount"):
                helper = files[f"/usr/local/bin/agent-workspace-{action}"]["content"]
                compile(helper, "<guest helper>", "exec")
            self.assertIn("/usr/local/lib/agent-worker/project.py", files)
            self.assertEqual((settings.seed_dir / "user-data").stat().st_mode & 0o777, 0o600)
            self.assertEqual(settings.seed_dir.stat().st_mode & 0o777, 0o700)

    def test_package_comments_whitespace_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resource_dir = Path(directory)
            path = resource_dir / "packages_list"
            path.write_bytes(b"# guest packages\r\n\r\n  tmux  # terminal\r\npython3\r\ncustom-tool")
            self.assertEqual(load_packages(path), ["tmux", "python3", "custom-tool"])
            for content in ("tmux\n- invalid-yaml\n", "# empty"):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    load_packages(path)

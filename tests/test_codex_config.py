from __future__ import annotations

import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_worker.guest import configure_codex as helper

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = tomllib.loads((ROOT / "agent_worker/resources/codex.config.toml").read_text())


class CodexConfigTests(unittest.TestCase):
    def test_preserves_model_comments_project_settings_and_multiline_values(self) -> None:
        original = '''# existing preferences
model = "custom-model"
sandbox_mode = "workspace-write"
approval_policy = { granular = { sandbox_approval = true } }
notes = """
[not_a_table]
sandbox_mode = 'preserve this text'
"""
[projects."/home/agent/workspace/app"]
trust_level = "trusted"
'''
        merged = helper.merge_defaults(original, DEFAULTS)
        expected = dict(tomllib.loads(original), **DEFAULTS)
        self.assertEqual(tomllib.loads(merged), expected)
        self.assertIn("# existing preferences", merged)
        self.assertEqual(helper.merge_defaults(merged, DEFAULTS), merged)
        tables = '[approval_policy.granular]\nsandbox_approval = true\n[profiles.example]\nsandbox_mode = "read-only"\n'
        self.assertEqual(tomllib.loads(helper.merge_defaults(tables, DEFAULTS)), dict(tomllib.loads(tables), **DEFAULTS))

    def test_invalid_config_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".codex/config.toml"
            config.parent.mkdir()
            config.write_text('model = "unterminated')
            with patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=home):
                with self.assertRaises(tomllib.TOMLDecodeError):
                    helper.configure(ROOT / "agent_worker/resources/codex.config.toml")
            self.assertEqual(config.read_text(), 'model = "unterminated')

    def test_creates_private_guest_config_and_keeps_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            codex = home / ".codex"
            codex.mkdir()
            auth = codex / "auth.json"
            auth.write_text("existing-auth")
            with patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=home):
                helper.configure(ROOT / "agent_worker/resources/codex.config.toml")
            config = codex / "config.toml"
            self.assertEqual(tomllib.loads(config.read_text()), DEFAULTS)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(auth.read_text(), "existing-auth")


if __name__ == "__main__":
    unittest.main()

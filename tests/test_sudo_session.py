import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/lib/agent_vm_sudo_session"


class SudoSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        mock = self.directory / "sudo"
        mock.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$SUDO_LOG"
if [[ "$*" == -v ]]; then exit "${INITIAL_STATUS:-0}"; fi
if [[ "$*" == '-n -v' ]]; then exit "${REFRESH_STATUS:-0}"; fi
[[ "${1:-}" == -n ]] || exit 99
shift
"$@"
''')
        mock.chmod(0o755)
        self.env = dict(
            os.environ,
            PATH=str(self.directory) + os.pathsep + os.environ["PATH"],
            SUDO_LOG=str(self.directory / "sudo.log"),
            SUDO_HELPER=str(HELPER),
            PID_FILE=str(self.directory / "refresh.pid"),
        )

    def run_session(self, body):
        return subprocess.run(
            ["bash", "-c", '''set -euo pipefail
source "$SUDO_HELPER"
trap stop_sudo_session EXIT
''' + body], env=self.env, text=True, capture_output=True, timeout=5,
        )

    def assert_refresh_stopped(self):
        pid = int((self.directory / "refresh.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_authenticate_once_refresh_and_noninteractive_commands(self):
        response = self.run_session('''
sleep() { command sleep 0.02; }
start_sudo_session
echo "$SUDO_REFRESH_PID" > "$PID_FILE"
command sleep 0.15
sudo printf 'privileged command succeeded\\n'
''')
        self.assertEqual(response.returncode, 0, response.stderr)
        calls = (self.directory / "sudo.log").read_text().splitlines()
        self.assertEqual(calls.count("-v"), 1)
        self.assertIn("-n -v", calls)
        self.assertTrue(any(call.startswith("-n printf") for call in calls))
        self.assertIn("privileged command succeeded", response.stdout)
        self.assert_refresh_stopped()

    def test_cleanup_on_failure_preserves_exit_status(self):
        response = self.run_session('''
start_sudo_session
echo "$SUDO_REFRESH_PID" > "$PID_FILE"
command sleep 0.05
exit 7
''')
        self.assertEqual(response.returncode, 7)
        self.assert_refresh_stopped()

    def test_initial_authentication_failure_stops_before_work(self):
        self.env["INITIAL_STATUS"] = "17"
        response = self.run_session("start_sudo_session\necho UNEXPECTED\n")
        self.assertEqual(response.returncode, 17)
        self.assertNotIn("UNEXPECTED", response.stdout)
        self.assertEqual((self.directory / "sudo.log").read_text(), "-v\n")

    def test_refresh_failure_is_noninteractive_and_reported(self):
        self.env["REFRESH_STATUS"] = "1"
        response = self.run_session('''
sleep() { command sleep 0.02; }
start_sudo_session
echo "$SUDO_REFRESH_PID" > "$PID_FILE"
command sleep 0.15
''')
        self.assertIn("authorization could not be refreshed", response.stderr)
        self.assertEqual((self.directory / "sudo.log").read_text().splitlines(), [
            "-v", "-n -v",
        ])
        self.assert_refresh_stopped()


if __name__ == "__main__":
    unittest.main()

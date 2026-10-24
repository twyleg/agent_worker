from __future__ import annotations

import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any

import pytest

from agent_worker.guest import dashboard

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "agent_worker/guest"


class DashboardTests(unittest.TestCase):
    def test_quota_windows_and_missing_usage(self) -> None:
        meters = dashboard.quota_meters(
            {
                "rateLimits": {
                    "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1800000000},
                    "secondary": None,
                }
            }
        )
        self.assertEqual(meters[0].label, "5h")
        self.assertEqual(meters[0].percent, 25)
        self.assertTrue(meters[0].reset)
        self.assertEqual(meters[1].label, "Week")
        self.assertIsNone(meters[1].percent)
        self.assertEqual(dashboard.quota_meters({}), [])

    def test_failed_usage_query_keeps_dashboard_alive(self) -> None:
        from unittest.mock import patch

        poller = dashboard.UsagePoller()

        def failed_query(**kwargs: Any) -> None:
            poller.stop.set()
            raise RuntimeError("Not signed in")

        with patch.object(dashboard, "read_usage", side_effect=failed_query):
            poller.run()
        meters, error, updated = poller.snapshot()
        self.assertEqual(meters, [])
        self.assertIn("Not signed in", error)
        self.assertIsNotNone(updated)

    def test_clock_and_bars_fit_narrow_and_wide_panes(self) -> None:
        now = datetime.datetime(2026, 10, 11, 12, 34, 56)
        for width in (5, 17, 22, 27, 48):
            for line in dashboard.clock_lines(now, width):
                self.assertLessEqual(len(line), width)
            for percent in (None, 0, 25, 100, 125):
                spans = dashboard.meter_line(dashboard.Meter("CPU", percent), width)
                self.assertLessEqual(sum(len(text) for text, _ in spans), width)
        empty = dashboard.meter_line(dashboard.Meter("Week", None), 22)
        self.assertIn("N/A", "".join(text for text, _ in empty))
        self.assertNotIn("|", "".join(text for text, _ in empty))
        meters = [
            dashboard.Meter("CPU", 5),
            dashboard.Meter("RAM", 50, "1.0/2.0G"),
            dashboard.Meter("Swap", 0, "0.0/2.0G"),
            dashboard.Meter("Week", 100),
        ]
        for width in (22, 48):
            lines = ["".join(text for text, _ in dashboard.meter_line(meter, width)) for meter in meters]
            self.assertEqual(len({(line.index("["), line.index("]")) for line in lines}), 1)

    def test_cpu_and_memory_percentages_from_counter_deltas(self) -> None:
        from unittest.mock import patch

        previous = dashboard.CpuSample(1000, 600, 300, 100, 0)
        current = dashboard.CpuSample(1100, 650, 330, 115, 5)
        memory = "MemTotal: 1048576 kB\nMemAvailable: 262144 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
        with (
            patch.object(Path, "read_text", return_value=memory),
            patch.object(dashboard.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 75, 25)),
        ):
            meters = dashboard.system_meters(previous, current)
        self.assertEqual([meter.percent for meter in meters], [50, 75, 0, 75])
        self.assertEqual(meters[0].segments, ((30, dashboard.GREEN), (15, dashboard.RED), (5, dashboard.YELLOW)))

    def test_rendering_scrolls_without_overflow_on_resize(self) -> None:
        class Screen:
            def __init__(self, height: int, width: int) -> None:
                self.height = height
                self.width = width

            def getmaxyx(self) -> tuple[int, int]:
                return self.height, self.width

            def erase(self) -> None:
                pass

            def refresh(self) -> None:
                pass

            def addstr(self, row: int, column: int, text: str, attr: int) -> None:
                self_check.assertTrue(0 <= row < self.height)
                self_check.assertTrue(0 <= column < self.width)
                self_check.assertLess(column + len(text), self.width)

        self_check = self
        quotas = dashboard.quota_meters({"rateLimits": {"primary": {"usedPercent": 20, "resetsAt": 1800000000}}})
        for height, width in ((1, 1), (5, 10), (12, 23), (25, 60)):
            screen = Screen(height, width)
            for scroll in (0, 100):
                actual = dashboard.draw(screen, [dashboard.Meter("RAM", 50, "1.0/2.0G")], "", quotas, "", "12:00:00", {}, scroll)
                self.assertGreaterEqual(actual, 0)

    def test_compact_dashboard_shows_queue_without_scrolling(self) -> None:
        from unittest.mock import patch

        class Screen:
            def __init__(self) -> None:
                self.text: list[str] = []

            def erase(self) -> None:
                pass

            def getmaxyx(self) -> tuple[int, int]:
                return 12, 23

            def addstr(self, row: int, column: int, text: str, attr: int) -> None:
                self.text.append(text)

            def refresh(self) -> None:
                pass

        screen = Screen()
        with patch.object(dashboard, "github_lines", return_value=[("GITHUB | idle", dashboard.CYAN), ("Queued 2 | Wait 1", dashboard.MUTED)]):
            dashboard.draw(screen, [dashboard.Meter("CPU", 50)], "", [], "", None, {})
        self.assertIn("GITHUB | idle", screen.text)
        self.assertIn("Queued 2 | Wait 1", screen.text)


class ConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        (self.directory / "id_ed25519").touch()
        self.env = dict(
            os.environ,
            STATE_DIR=str(self.directory),
            PATH=str(self.directory) + os.pathsep + os.environ["PATH"],
            SSH_LOG=str(self.directory / "ssh.log"),
            SSH_PAYLOAD=str(self.directory / "payload"),
        )
        for name, script in {
            "virsh": """#!/usr/bin/env python3
import sys
if 'domifaddr' in sys.argv:
    print('vnet0 aa:bb:cc:dd:ee:ff ipv4 192.168.122.10/24')
""",
            "ssh": """#!/usr/bin/env python3
import json, os, sys
with open(os.environ['SSH_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
if '-t' not in sys.argv:
    with open(os.environ['SSH_PAYLOAD'], 'wb') as output:
        output.write(sys.stdin.buffer.read())
""",
        }.items():
            path = self.directory / name
            path.write_text(script)
            path.chmod(0o755)

    def run_cli(self, *args: str, input: bytes = b"") -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-m", "agent_worker", *args],
            env=self.env,
            input=input,
            capture_output=True,
            check=True,
            timeout=10,
        )

    def ssh_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in (self.directory / "ssh.log").read_text().splitlines()]

    def test_ssh_preserves_remote_command_and_stdin(self) -> None:
        self.run_cli("vm", "ssh", "python3", "-", "--json", input=b"example")
        (call,) = self.ssh_calls()
        self.assertEqual(call[-4:], ["agent@192.168.122.10", "python3", "-", "--json"])
        self.assertEqual((self.directory / "payload").read_bytes(), b"example")

    def test_ssh_preserves_arbitrary_ssh_options(self) -> None:
        self.run_cli("vm", "ssh", "--", "-t", "-o", "ServerAliveInterval=15", "bash")
        (call,) = self.ssh_calls()
        self.assertEqual(call[-5:], ["agent@192.168.122.10", "-t", "-o", "ServerAliveInterval=15", "bash"])

    def test_workspace_uploads_helpers_then_requests_tty(self) -> None:
        self.run_cli("vm", "connect")
        upload, attach = self.ssh_calls()
        self.assertIn("tar -xf", upload[-1])
        self.assertIn("-t", attach)
        self.assertIn("workspace.py", attach[-1])
        with tarfile.open(fileobj=io.BytesIO((self.directory / "payload").read_bytes())) as archive:
            self.assertEqual(
                set(archive.getnames()),
                {
                    "workspace.py",
                    "dashboard.py",
                    "codex_usage.py",
                    "tmux.conf",
                    "tmux.conf.local",
                    "configure_codex.py",
                    "github_workflow.py",
                    "github_client.py",
                    "git_credential.py",
                    "github_review.py",
                    "codex.config.toml",
                },
            )
            for name in ("tmux.conf", "tmux.conf.local"):
                member = archive.extractfile(name)
                assert member is not None
                self.assertEqual(member.read(), (ROOT / "agent_worker/resources" / name).read_bytes())


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for layout verification")
class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.helpers = self.directory / "helpers with spaces"
        shutil.copytree(LIB, self.helpers)
        for name in ("tmux.conf", "tmux.conf.local", "codex.config.toml"):
            shutil.copyfile(ROOT / "agent_worker/resources" / name, self.helpers / name)
        self.project = self.directory / "workspace/project with spaces"
        self.project.mkdir(parents=True)
        tmux = shutil.which("tmux")
        assert tmux is not None
        self.tmux = tmux
        self.socket = str(self.directory / "tmux.sock")
        self.env = dict(
            os.environ,
            HOME=str(self.directory),
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            TERM="xterm-256color",
            SHELL="/bin/bash",
        )
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.env.pop("CODEX_HOME", None)
        # Exercise real tmux creation, but bypass the interactive attach.
        self.write_command(
            "tmux",
            f"""#!/usr/bin/env python3
import os, sys
if sys.argv[1] == 'attach-session':
    print(sys.argv[-1])
else:
    os.execv({self.tmux!r}, [{self.tmux!r}, '-S', {self.socket!r}, '-f', '/dev/null', *sys.argv[1:]])
""",
        )
        self.write_command("stty", '#!/bin/sh\necho "40 160"\n')
        self.write_command("codex", "#!/bin/sh\nsleep 60\n")
        self.addCleanup(lambda: self.tmux_run("kill-server", check=False))

    def write_command(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def tmux_run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.tmux, "-S", self.socket, "-f", "/dev/null", *args],
            env=self.env,
            text=True,
            capture_output=True,
            check=check,
            timeout=10,
        )

    def connect(self) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [sys.executable, str(self.helpers / "workspace.py")],
                env=self.env,
                text=True,
                capture_output=True,
                check=True,
                timeout=10,
            )
        except subprocess.CalledProcessError as exc:
            print("STDOUT:", exc.stdout, flush=True)
            print("STDERR:", exc.stderr, file=sys.stderr, flush=True)
            raise

    def panes(self) -> list[str]:
        return self.tmux_run(
            "list-panes",
            "-t",
            "agent-workspace",
            "-F",
            "#{pane_id}|#{pane_left}|#{pane_top}|#{pane_width}|#{pane_current_path}",
        ).stdout.splitlines()

    def test_three_pane_layout_and_reconnect_preserves_panes(self) -> None:
        self.assertEqual(self.connect().stdout.strip(), "agent-workspace")
        panes = self.panes()
        self.assertEqual(len(panes), 3)
        left, upper, lower = [line.split("|") for line in panes]
        self.assertEqual(left[1:3], ["0", "0"])
        self.assertTrue(108 <= int(left[3]) <= 114)
        self.assertEqual(upper[1], lower[1])
        self.assertEqual(upper[2], "0")
        self.assertGreater(int(lower[2]), 0)
        for pane in (left, upper, lower):
            self.assertEqual(pane[4], str(self.project))
        self.connect()
        self.assertEqual(self.panes(), panes)
        self.assertEqual((self.directory / ".tmux.conf").read_bytes(), (ROOT / "agent_worker/resources/tmux.conf").read_bytes())
        self.assertEqual(self.tmux_run("show-options", "-gv", "prefix2").stdout.strip(), "C-a")
        self.assertEqual(self.tmux_run("show-options", "-gv", "mouse").stdout.strip(), "on")
        self.assertEqual(self.tmux_run("show-options", "-gv", "status-bg").stdout.strip(), "colour66")
        with (self.helpers / "tmux.conf.local").open("a") as config:
            config.write("\nset -g status-bg colour123\n")
        self.connect()
        self.assertEqual(self.tmux_run("show-options", "-gv", "status-bg").stdout.strip(), "colour123")
        self.assertEqual(self.panes(), panes)

    def test_reconnect_refreshes_only_outdated_dashboard(self) -> None:
        self.connect()
        layout = self.panes()
        ids = [line.split("|")[0] for line in layout]

        def pids() -> list[str]:
            return self.tmux_run("list-panes", "-t", "agent-workspace", "-F", "#{pane_pid}").stdout.splitlines()

        before = pids()
        # Simulate a dashboard started before version tracking existed.
        self.tmux_run("set-option", "-p", "-u", "-t", ids[1], "@agent-worker-dashboard-version")
        self.connect()
        after = pids()
        self.assertEqual(after[0], before[0])
        self.assertNotEqual(after[1], before[1])
        self.assertEqual(after[2], before[2])
        self.assertEqual(self.panes(), layout)
        self.connect()
        self.assertEqual(pids(), after)
        with (self.helpers / "dashboard.py").open("a") as stream:
            stream.write("\n# Installed dashboard update\n")
        self.connect()
        updated = pids()
        self.assertNotEqual(updated[1], after[1])
        self.assertEqual(updated[0], after[0])
        self.assertEqual(updated[2], after[2])
        self.assertEqual(self.panes(), layout)

    def test_attaches_existing_user_session(self) -> None:
        self.tmux_run("new-session", "-d", "-s", "existing", "-x", "160", "-y", "40")
        target = self.connect().stdout.strip()
        self.assertTrue(target.startswith("$"))
        sessions = self.tmux_run("list-sessions", "-F", "#{session_name}").stdout.splitlines()
        self.assertEqual(sessions, ["existing"])


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("problem", [False, True])
def test_dashboard_poll_key_shows_confirmation_or_error(monkeypatch: pytest.MonkeyPatch, problem: bool) -> None:
    from unittest.mock import Mock

    screen = Mock()
    screen.getch.side_effect = [ord("p"), ord("q")]
    poller = Mock()
    poller.snapshot.return_value = ([], "", None)
    monkeypatch.setattr(dashboard, "UsagePoller", lambda: poller)
    monkeypatch.setattr(dashboard, "palette", lambda: {})
    monkeypatch.setattr(dashboard.curses, "curs_set", lambda value: None)
    render = Mock(return_value=0)
    monkeypatch.setattr(dashboard, "draw", render)
    trigger = Mock(side_effect=RuntimeError("Start the GitHub worker first")) if problem else Mock(return_value="Poll requested")
    monkeypatch.setattr(dashboard, "request_poll", trigger)
    dashboard.dashboard(screen)
    trigger.assert_called_once_with()
    assert render.call_args.args[-1] == ("Start the GitHub worker first" if problem else "Poll requested")

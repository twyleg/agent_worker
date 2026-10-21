from __future__ import annotations

import subprocess
import time
import unittest
from unittest.mock import Mock

from agent_worker.system import CommandError, Runner, SudoSession


class SudoSessionTests(unittest.TestCase):
    def runner(self, initial: int = 0, refresh: int = 0) -> Mock:
        runner = Mock(noninteractive_sudo=False)
        runner.run.side_effect = lambda *args, **kwargs: subprocess.CompletedProcess(args, initial if not runner.noninteractive_sudo else refresh, "", "")
        return runner

    def test_authorize_refresh_and_stop_on_error(self) -> None:
        runner = self.runner()
        with self.assertRaisesRegex(ValueError, "work failed"):
            with SudoSession(runner, interval=0.01) as session:
                self.assertTrue(runner.noninteractive_sudo)
                time.sleep(0.05)
                raise ValueError("work failed")
        assert session.thread is not None
        self.assertFalse(session.thread.is_alive())
        self.assertFalse(runner.noninteractive_sudo)
        runner.run.assert_any_call("sudo", "-v")
        runner.run.assert_any_call("sudo", "-v", check=False, capture=True)

    def test_initial_failure_does_not_start_refresh(self) -> None:
        runner = self.runner()
        runner.run.side_effect = CommandError(["sudo"], subprocess.CompletedProcess(["sudo"], 17, "", ""))
        session = SudoSession(runner)
        with self.assertRaises(CommandError):
            session.__enter__()
        self.assertIsNone(session.thread)
        self.assertFalse(runner.noninteractive_sudo)

    def test_refresh_failure_reports_and_stops(self) -> None:
        runner = self.runner(refresh=1)
        from unittest.mock import patch

        with patch("sys.stderr") as stderr:
            with SudoSession(runner, interval=0.01) as session:
                assert session.thread is not None
                session.thread.join(timeout=1)
                self.assertFalse(session.thread.is_alive())
            self.assertIn("could not be refreshed", "".join(call.args[0] for call in stderr.write.call_args_list))

    def test_runner_makes_privileged_operations_noninteractive(self) -> None:
        from unittest.mock import patch

        runner = Runner()
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            runner.noninteractive_sudo = True
            runner.run("sudo", "pacman", "-S", "libvirt")
            self.assertEqual(run.call_args.args[0], ["sudo", "-n", "pacman", "-S", "libvirt"])

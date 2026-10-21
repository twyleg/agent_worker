from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "agent_worker/guest/codex_usage.py"
MOCK_SERVER = """#!/usr/bin/env python3
import json, os, sys
initialized = False
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    with open(os.environ["RPC_LOG"], "a") as log:
        log.write(method + "\\n")
    if method == "initialize":
        print(json.dumps({"method": "notification", "params": {}}), flush=True)
        print(json.dumps({"id": request["id"], "result": {}}), flush=True)
    elif method == "initialized":
        initialized = True
    elif method == "account/rateLimits/read" and initialized:
        if os.environ.get("MOCK_EXIT"):
            sys.exit(1)
        reply = {"id": request["id"]}
        if os.environ.get("MOCK_ERROR"):
            reply["error"] = {"code": -1, "message": "Not signed in"}
        else:
            reply["result"] = json.loads(os.environ["MOCK_RESULT"])
        print(json.dumps(reply), flush=True)
    else:
        sys.exit(99)
"""


class UsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        server = self.directory / "codex"
        server.write_text(MOCK_SERVER)
        server.chmod(0o755)
        self.env = dict(
            os.environ,
            PATH=str(self.directory) + os.pathsep + os.environ["PATH"],
            RPC_LOG=str(self.directory / "rpc.log"),
            MOCK_RESULT="{}",
        )

    def run_helper(self, result: dict[str, Any], *args: str) -> subprocess.CompletedProcess[str]:
        self.env["MOCK_RESULT"] = json.dumps(result)
        return subprocess.run(
            [sys.executable, str(HELPER), *args],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=10,
        )

    def test_usage_and_handshake_without_inference(self) -> None:
        result = {
            "rateLimits": {
                "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1800000000},
                "secondary": {"usedPercent": 61.5, "windowDurationMins": 10080, "resetsAt": 1800600000},
            }
        }
        response = self.run_helper(result)
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertIn("5-hour: 25% used, 75% remaining", response.stdout)
        self.assertIn("Weekly: 61.5% used, 38.5% remaining", response.stdout)
        self.assertIn("resets ", response.stdout)
        self.assertEqual(
            (self.directory / "rpc.log").read_text().splitlines(),
            [
                "initialize",
                "initialized",
                "account/rateLimits/read",
            ],
        )

    def test_json_preserves_multiple_buckets(self) -> None:
        result = {
            "rateLimitsByLimitId": {
                "codex": {"primary": {"usedPercent": 10, "windowDurationMins": 300}},
                "other": {"secondary": None},
            }
        }
        response = self.run_helper(result, "--json")
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(json.loads(response.stdout), result)

    def test_missing_window_is_not_reported_as_zero(self) -> None:
        response = self.run_helper(
            {
                "rateLimits": {
                    "primary": {"usedPercent": 12, "windowDurationMins": 60},
                    "secondary": None,
                }
            }
        )
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertIn("60-minute: 12% used", response.stdout)
        self.assertIn("Weekly: not reported", response.stdout)
        self.assertIn("5-hour: not reported", response.stdout)

    def test_authentication_error(self) -> None:
        self.env["MOCK_ERROR"] = "1"
        response = self.run_helper({})
        self.assertEqual(response.returncode, 1)
        self.assertIn("Not signed in", response.stderr)
        self.assertIn("codex login", response.stderr)

    def test_empty_result(self) -> None:
        response = self.run_helper({})
        self.assertEqual(response.returncode, 1)
        self.assertIn("no usage percentages", response.stderr)

    def test_server_exits(self) -> None:
        self.env["MOCK_EXIT"] = "1"
        response = self.run_helper({})
        self.assertEqual(response.returncode, 1)
        self.assertIn("exited before returning usage", response.stderr)


if __name__ == "__main__":
    unittest.main()

"""Run inside the guest via SSH; only query the Codex app-server rate limits."""

from __future__ import annotations

import datetime
import json
import os
import selectors
import shutil
import subprocess
import sys
import time
from typing import Any


def read_usage(timeout: float = 30) -> dict[str, Any]:
    if not shutil.which("codex"):
        raise RuntimeError("Codex is not installed in the VM.")
    process = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdin is not None and process.stdout is not None
    stdin = process.stdin
    stdout = process.stdout
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    pending = b""

    def send(message: dict[str, Any]) -> None:
        stdin.write((json.dumps(message) + "\n").encode())
        stdin.flush()

    def response(request_id: int) -> dict[str, Any]:
        nonlocal pending
        while True:
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if not line.strip():
                    continue
                message = json.loads(line)
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    error = message["error"]
                    detail = error.get("message", "request failed") if isinstance(error, dict) else str(error)
                    raise RuntimeError(f"Codex rejected the usage query: {detail}. " "This requires a ChatGPT login inside the VM (codex login).")
                return message["result"]
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise RuntimeError(f"Timed out after {timeout:g} seconds querying Codex usage.")
            chunk = os.read(stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("Codex app-server exited before returning usage.")
            pending += chunk

    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "agent_worker",
                        "title": "Agent Worker Codex Usage",
                        "version": "1.0.0",
                    }
                },
            }
        )
        response(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read"})
        return response(2)
    finally:
        selector.close()
        process.stdin.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


def print_usage(result: dict[str, Any]) -> None:
    buckets = result.get("rateLimitsByLimitId") or {}
    if not buckets and result.get("rateLimits"):
        snapshot = result["rateLimits"]
        buckets = {snapshot.get("limitId") or "codex": snapshot}
    if not buckets:
        raise RuntimeError("The account did not report rate limits; no usage percentages are available.")
    for bucket_id, snapshot in buckets.items():
        if not snapshot:
            continue
        print(f"{snapshot.get('limitName') or bucket_id} usage (VM account)")
        if snapshot.get("planType"):
            print(f"Plan: {snapshot['planType']}")
        reported = set()
        for field in ("primary", "secondary"):
            window = snapshot.get(field)
            if not window:
                continue
            duration = window.get("windowDurationMins")
            label = {300: "5-hour", 10080: "Weekly"}.get(duration)
            if label is None:
                label = f"{duration}-minute" if duration is not None else field.capitalize()
            reported.add(label)
            percent = window.get("usedPercent")
            if percent is None:
                usage = "usage not reported"
            else:
                usage = f"{percent:g}% used, {max(0, 100 - percent):g}% remaining"
            reset = window.get("resetsAt")
            reset_text = "reset time not reported"
            if reset is not None:
                reset_text = "resets " + datetime.datetime.fromtimestamp(reset, datetime.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            print(f"  {label}: {usage}; {reset_text}")
        for label in ("5-hour", "Weekly"):
            if label not in reported:
                print(f"  {label}: not reported for this account")


def main() -> int:
    try:
        result = read_usage()
        if "--json" in sys.argv[1:]:
            print(json.dumps(result, indent=2))
        else:
            print_usage(result)
        return 0
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

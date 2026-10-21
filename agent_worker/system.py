"""External commands and scoped sudo authorization."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Self, TypedDict, overload

CommandArg = str | Path | int


class TextRunOptions(TypedDict, total=False):
    capture: bool
    check: bool
    input: str | None
    timeout: float | None


class RunOptions(TypedDict, total=False):
    capture: bool
    check: bool
    input: str | bytes | None
    timeout: float | None


def host_environment() -> dict[str, str]:
    """Keep bundled shared libraries out of external host commands."""
    env = dict(os.environ, LC_ALL="C")
    if getattr(sys, "frozen", False):
        original = env.get("LD_LIBRARY_PATH_ORIG")
        if original is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original
    return env


class CommandError(RuntimeError):
    def __init__(self, command: Sequence[CommandArg], result: subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]) -> None:
        self.returncode = result.returncode
        # Never include command arguments: they can contain passwords or guest data.
        detail = (result.stderr or "").strip() if isinstance(result.stderr, str) else ""
        super().__init__(f"{Path(str(command[0])).name} failed (exit {result.returncode})" + (f": {detail}" if detail else ""))


class Runner:
    def __init__(self) -> None:
        self.noninteractive_sudo = False

    @overload
    def run(
        self, *command: CommandArg, capture: bool = False, check: bool = True, input: bytes, timeout: float | None = None
    ) -> subprocess.CompletedProcess[bytes]: ...

    @overload
    def run(
        self, *command: CommandArg, capture: bool = False, check: bool = True, input: str | None = None, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]: ...

    @overload
    def run(
        self, *command: CommandArg, capture: bool = False, check: bool = True, input: str | bytes | None, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]: ...

    def run(
        self, *command: CommandArg, capture: bool = False, check: bool = True, input: str | bytes | None = None, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        argv = [str(arg) for arg in command]
        if argv[0] == "sudo" and self.noninteractive_sudo:
            argv.insert(1, "-n")
        result = subprocess.run(
            argv,
            capture_output=capture,
            input=input,
            text=not isinstance(input, bytes),
            timeout=timeout,
            env=host_environment(),
        )
        if check and result.returncode:
            raise CommandError(argv, result)
        return result


class SudoSession:
    def __init__(self, runner: Runner, interval: float = 30) -> None:
        self.runner = runner
        self.interval = interval
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        self.runner.run("sudo", "-v")
        self.runner.noninteractive_sudo = True
        self.thread = threading.Thread(target=self._refresh, daemon=True)
        self.thread.start()
        return self

    def _refresh(self) -> None:
        while not self.stopped.wait(self.interval):
            try:
                result = self.runner.run("sudo", "-v", check=False, capture=True)
                if result.returncode == 0:
                    continue
            except OSError:
                pass
            print(
                "ERROR: Sudo authorization could not be refreshed; privileged operations will fail without prompting.",
                file=sys.stderr,
            )
            break

    def __exit__(self, *exc: object) -> None:
        self.stopped.set()
        assert self.thread is not None
        self.thread.join()
        self.runner.noninteractive_sudo = False


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

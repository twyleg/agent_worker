"""Create or reconnect to the guest's persistent Codex/tmux workspace."""

from __future__ import annotations

import fcntl
import os
import shlex
import shutil
import subprocess
from pathlib import Path

if __package__:
    from .configure_codex import configure
else:
    from configure_codex import configure as standalone_configure

    configure = standalone_configure


def run(*args: str, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=capture, text=True, check=check)


def main() -> None:
    if not shutil.which("tmux"):
        print("Installing tmux in the guest...")
        run("sudo", "-n", "apt-get", "update")
        run("sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "tmux")
    helpers = Path(__file__).resolve().parent
    session = "agent-workspace"
    with (helpers / "workspace.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        configure(helpers / "codex.config.toml")
        for name in ("tmux.conf", "tmux.conf.local"):
            destination = Path.home() / f".{name}"
            shutil.copyfile(helpers / name, destination)
            destination.chmod(0o644)
        if run("tmux", "has-session", "-t", f"={session}", capture=True, check=False).returncode == 0:
            target = session
        else:
            sessions = run("tmux", "list-sessions", "-F", "#{session_id}", capture=True, check=False)
            if sessions.returncode == 0 and sessions.stdout.strip():
                target = sessions.stdout.splitlines()[0]
            else:
                workdir = Path.home() / "workspace"
                workdir.mkdir(exist_ok=True)
                projects = [path for path in workdir.iterdir() if path.is_dir()]
                if len(projects) == 1:
                    workdir = projects[0]
                rows, columns = run("stty", "size", capture=True).stdout.split()
                left = run(
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    session,
                    "-n",
                    "work",
                    "-c",
                    str(workdir),
                    "-x",
                    columns,
                    "-y",
                    rows,
                    "-P",
                    "-F",
                    "#{pane_id}",
                    capture=True,
                ).stdout.strip()
                try:
                    right = run(
                        "tmux",
                        "split-window",
                        "-h",
                        "-p",
                        "30",
                        "-t",
                        left,
                        "-c",
                        str(workdir),
                        "-P",
                        "-F",
                        "#{pane_id}",
                        shlex.join(["python3", str(helpers / "dashboard.py")]),
                        capture=True,
                    ).stdout.strip()
                    run("tmux", "split-window", "-v", "-p", "40", "-t", right, "-c", str(workdir))
                    run("tmux", "select-pane", "-t", left)
                    run(
                        "tmux",
                        "respawn-pane",
                        "-k",
                        "-t",
                        left,
                        "-c",
                        str(workdir),
                        'codex --sandbox danger-full-access --ask-for-approval never; exec "${SHELL:-/bin/bash}" -l',
                    )
                except subprocess.CalledProcessError:
                    run("tmux", "kill-session", "-t", f"={session}", check=False)
                    raise RuntimeError("Could not create the workspace; enlarge the terminal and reconnect.")
                target = session
        run("tmux", "source-file", str(Path.home() / ".tmux.conf"))
        fcntl.flock(lock, fcntl.LOCK_UN)
    os.execlp("tmux", "tmux", "attach-session", "-t", target)


if __name__ == "__main__":
    main()

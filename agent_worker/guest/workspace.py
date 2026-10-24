"""Create or reconnect to the guest's persistent Codex/tmux workspace."""

from __future__ import annotations

import fcntl
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

if __package__:
    from .configure_codex import configure
else:
    from configure_codex import configure as standalone_configure

    configure = standalone_configure


def run(*args: str, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            capture_output=capture,
            text=True,
            check=check,
        )
    except subprocess.CalledProcessError as exc:
        print(f"Failed command: {exc.cmd}", file=sys.stderr)
        print(f"STDERR: {exc.stderr}", file=sys.stderr, flush=True)
        raise


def dashboard_version(helpers: Path) -> str:
    return hashlib.sha256(
        b"".join((helpers / name).read_bytes() for name in ("dashboard.py", "codex_usage.py", "github_workflow.py", "github_client.py", "github_review.py"))
    ).hexdigest()


def refresh_dashboards(target: str, helpers: Path) -> None:
    """Reload only recognized dashboard panes when their installed code changes."""
    panes = run(
        "tmux",
        "list-panes",
        "-s",
        "-t",
        target,
        "-F",
        "#{pane_id}\t#{pane_start_command}\t#{pane_current_command}\t#{pane_current_path}\t#{@agent-worker-dashboard-version}",
        capture=True,
    )
    version = dashboard_version(helpers)
    for line in panes.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 5:
            continue
        pane, start, current, directory, installed = fields
        try:
            args = shlex.split(start)
            # tmux can render a single shell command as one quoted argument.
            if len(args) == 1:
                args = shlex.split(args[0])
        except ValueError:
            continue
        # Recognize old, untagged dashboards by the exact script path. Never
        # respawn a shell or Codex pane that happens to occupy the same position.
        if len(args) != 2 or Path(args[1]) != helpers / "dashboard.py" or not Path(args[0]).name.startswith("python"):
            continue
        if installed == version or not current.startswith("python"):
            continue
        run("tmux", "respawn-pane", "-k", "-t", pane, "-c", directory, shlex.join(["python3", str(helpers / "dashboard.py")]))
        run("tmux", "set-option", "-p", "-t", pane, "@agent-worker-dashboard-version", version)


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
                        "-l",
                        "30%",
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
                    run("tmux", "set-option", "-p", "-t", right, "@agent-worker-dashboard-version", dashboard_version(helpers))
                    run("tmux", "split-window", "-v", "-l", "40%", "-t", right, "-c", str(workdir))
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
        refresh_dashboards(target, helpers)
        run("tmux", "source-file", str(Path.home() / ".tmux.conf"))
        fcntl.flock(lock, fcntl.LOCK_UN)
    os.execlp("tmux", "tmux", "attach-session", "-t", target)


if __name__ == "__main__":
    main()

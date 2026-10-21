"""Guest provisioning helpers, runnable without the host package."""

from __future__ import annotations

import os
import pwd
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path


def legacy_helper(action: str, user: str, tag: str) -> str:
    return f"""#!/usr/bin/env python3
import runpy, sys
from pathlib import Path
name = sys.argv[1] if len(sys.argv) == 2 else ''
if not name or name in ('.', '..') or '/' in name:
    raise SystemExit('Pass a valid project directory name')
helper = runpy.run_path('/usr/local/lib/agent-worker/project.py')
workspace = '/home/{user}/workspace'
path = str(Path(workspace) / name)
""" + (
        f"helper['mount'](path, {tag!r}, workspace)\n" if action == "mount" else f"helper['unmount'](path, {tag!r}, '')\n"
    )


def setup(packages: Sequence[str], user: str, prefix: str, tag: str, source: str, environment: Mapping[str, str] | None = None) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Run guest setup inside the VM with sudo/root.")
    account = pwd.getpwnam(user)
    env = dict(os.environ if environment is None else environment, DEBIAN_FRONTEND="noninteractive")

    def run(*args: str) -> None:
        subprocess.run(args, env=env, check=True)

    run("apt-get", "update")
    run("apt-get", "install", "-y", *packages)
    sudoers = Path(f"/etc/sudoers.d/90-{user}")
    sudoers.write_text(f"{user} ALL=(ALL:ALL) NOPASSWD: ALL\n")
    sudoers.chmod(0o440)
    run("visudo", "-cf", str(sudoers))
    workspace = Path(f"/home/{user}/workspace")
    workspace.mkdir(parents=True, exist_ok=True)
    os.chown(workspace, account.pw_uid, account.pw_gid)
    if Path("/usr/bin/fdfind").exists():
        Path("/usr/local/bin/fd").unlink(missing_ok=True)
        Path("/usr/local/bin/fd").symlink_to("/usr/bin/fdfind")
    helper = Path("/usr/local/lib/agent-worker/project.py")
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text(source)
    for action in ("mount", "unmount"):
        path = Path(f"/usr/local/bin/{prefix}-{action}")
        path.write_text(legacy_helper(action, user, tag))
        path.chmod(0o755)
    run("npm", "install", "-g", "@openai/codex@latest")
    run("systemctl", "enable", "--now", "qemu-guest-agent.service")
    run("codex", "--version")

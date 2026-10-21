"""Mount a selected virtiofs device inside the guest (standalone helper)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def mounted(path: str | Path, kind: str | None = None) -> str | None:
    command = ["findmnt", "-rn", "-M", str(path), "-o", "SOURCE"]
    if kind:
        command += ["-t", kind]
    result = subprocess.run(command, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def mount(path: str, tag: str, workspace: str) -> None:
    if mounted(workspace, "virtiofs"):
        raise RuntimeError(f"A legacy share is mounted at {workspace}; detach it first.")
    Path(path).mkdir(parents=True, exist_ok=True)
    current = mounted(path)
    if current is not None:
        if current != tag:
            raise RuntimeError(f"Another filesystem is mounted at {path}")
        print(f"Already mounted: {path}")
        return
    subprocess.run(["mount", "-t", "virtiofs", tag, path], check=True)
    print(f"Mounted {tag} at {path}")


def unmount(path: str, tag: str, legacy: str) -> None:
    for candidate in (path, legacy):
        if not candidate:
            continue
        current = mounted(candidate, "virtiofs")
        if current is not None:
            if current != tag:
                raise RuntimeError(f"Another share is mounted at {candidate}")
            subprocess.run(["umount", candidate], check=True)
            print(f"Unmounted {candidate}")


if __name__ == "__main__":
    action, path, tag, extra = sys.argv[1:]
    if action == "mount":
        mount(path, tag, extra)
    elif action == "unmount":
        unmount(path, tag, extra)
    else:
        raise ValueError(f"Unknown action: {action}")

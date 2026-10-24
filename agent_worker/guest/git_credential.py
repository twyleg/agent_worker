"""Supply only the guest token to Git's HTTPS credential protocol."""

from __future__ import annotations

import sys

if __package__:
    from .github_client import read_token
else:
    from github_client import read_token as standalone_read_token

    read_token = standalone_read_token


def main() -> None:
    if sys.argv[1:] != ["get"]:
        return  # Never persist credentials supplied by Git (store/erase).
    fields = dict(line.rstrip("\n").split("=", 1) for line in sys.stdin if "=" in line)
    if fields.get("protocol") != "https" or fields.get("host") != "github.com":
        return
    try:
        token = read_token()
    except (RuntimeError, OSError):
        return
    sys.stdout.write(f"username=x-access-token\npassword={token}\n\n")


if __name__ == "__main__":
    main()

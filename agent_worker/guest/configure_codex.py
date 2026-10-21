"""Apply guest Codex defaults while preserving unrelated settings and comments."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def merge_defaults(text: str, defaults: Mapping[str, Any]) -> str:
    before = tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    kept = []
    index = 0
    in_table = False
    drop_table = False
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("["):
            in_table = True
            drop_table = bool(set(tomllib.loads(line)).intersection(defaults))
            if not drop_table:
                kept.append(line)
            index += 1
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            if not drop_table:
                kept.append(line)
            index += 1
            continue
        # Parse complete assignments, including multiline strings, inline tables
        # and dotted keys. Never mistake text inside a string for a setting.
        start = index
        while True:
            index += 1
            block = "".join(lines[start:index])
            try:
                statement = tomllib.loads(block)
                break
            except tomllib.TOMLDecodeError:
                if index == len(lines):
                    raise ValueError("Could not parse the existing Codex settings")
        if not drop_table and (in_table or not set(statement).intersection(defaults)):
            kept.append(block)
    prefix = "".join(f"{key} = {json.dumps(value)}\n" for key, value in defaults.items())
    merged = prefix + "".join(kept)
    expected = dict(before, **defaults)
    if tomllib.loads(merged) != expected:
        raise ValueError("Could not preserve the existing Codex settings")
    return merged


def configure(defaults_path: str | Path) -> None:
    defaults = tomllib.loads(Path(defaults_path).read_text())
    codex_dir = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    config = codex_dir / "config.toml"
    original = config.read_text() if config.exists() else ""
    merged = merge_defaults(original, defaults)
    if merged == original:
        return
    codex_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=codex_dir, delete=False) as output:
        temporary = Path(output.name)
        output.write(merged)
    try:
        temporary.replace(config)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    configure(sys.argv[1])

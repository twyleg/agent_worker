"""Read local project records for shell completion without contacting the VM."""

import argparse
import json
from pathlib import Path

from agent_worker.settings import Settings
from agent_worker.shares import Shares
from agent_worker.vm import VM


def project_selectors(prefix: str, parsed_args: argparse.Namespace, config_filepath: str | Path | None) -> list[str]:
    try:
        config = json.loads(Path(config_filepath).read_text()) if config_filepath else {}
        settings = Settings.load(config, {"data_dir": getattr(parsed_args, "data_dir", None)})
        candidates = {value for share in Shares(VM(settings)).records() for value in (share.name, str(share.project))}
    except (OSError, ValueError, TypeError):
        # Missing or incomplete records/configuration must not interrupt shell completion.
        return []
    return sorted(value for value in candidates if value.startswith(prefix))

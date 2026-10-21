from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agent_worker.main import Application
from agent_worker.settings import RESOURCE_DIR


def test_FrameworkInitialization_NoUserConfig_LoadPackagedDefaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    with patch("agent_worker.main.VM") as vm:
        application = Application()
        assert application.start(["vm", "status"]) == 0
        assert application.config.application_config_init_enabled
        assert application.application_config == {}
        assert application.application_config_filepath is not None
        assert Path(application.application_config_filepath) == RESOURCE_DIR / "agent_worker_config.json"
        assert application.application_config_filepath_source is application.ConfigFilepathSource.SEARCH
        assert application.logging_config_type is application.LoggingConfigType.DEFAULT
        vm.assert_called_once()


def test_FrameworkInitialization_LocalConfigAndLogging_LoadThroughFramework(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "agent_worker_config.json").write_text('{"vm_name": "configured-agent"}')
    (tmp_path / "logging.yaml").write_text((RESOURCE_DIR / "logging.yaml").read_text())
    with patch("agent_worker.main.VM") as vm:
        application = Application()
        assert application.start(["vm", "status"]) == 0
        assert application.application_config_filepath_source is application.ConfigFilepathSource.SEARCH
        assert application.logging_config_type is application.LoggingConfigType.CUSTOM
        assert vm.call_args.args[0].vm_name == "configured-agent"
        assert application.logging_logfile_filepath is not None
        assert application.logging_logfile_filepath.is_file()

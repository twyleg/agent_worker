from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from agent_worker.system import Runner


@pytest.mark.parametrize("frozen,original,expected", [(True, "/host/lib", "/host/lib"), (True, None, None), (False, None, "/bundled/lib")])
def test_RunExternalCommand_FrozenEnvironment_RestoreHostLibraries(
    monkeypatch: pytest.MonkeyPatch, frozen: bool, original: str | None, expected: str | None
) -> None:
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/bundled/lib")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    if original is not None:
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", original)
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
        Runner().run("virsh", "--version")
    env = run.call_args.kwargs["env"]
    assert env.get("LD_LIBRARY_PATH") == expected
    assert env["LC_ALL"] == "C"
    assert os.environ["LD_LIBRARY_PATH"] == "/bundled/lib"

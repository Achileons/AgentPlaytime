"""Keep every test away from the user's service and data directories."""

from pathlib import Path

import pytest

from agentplaytime.service import manager


@pytest.fixture(autouse=True)
def isolated_service_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "test home"))

    def absent_service(arguments):
        assert arguments[:2] == [manager.LAUNCHCTL, "print"], (
            "A mutating service operation must inject a fake command runner"
        )
        return manager.CommandResult(113, stderr="Could not find service")

    monkeypatch.setattr(manager, "run_command", absent_service)

"""Shared runtime dependencies for uploaded agents."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ctb.agents.dirge import Dirge


@pytest.mark.parametrize("manager", ["apt-get", "dnf", "yum", "apk"])
def test_install_ensures_ca_certificates(tmp_path, monkeypatch, manager):
    archive = tmp_path / "agent.tar.gz"
    archive.write_bytes(b"test archive")
    agent = Dirge(logs_dir=tmp_path / "logs", model_name="openai/gpt-6.1-sol")
    monkeypatch.setattr(agent, "ARCHIVE", archive)
    monkeypatch.setattr(agent, "_get_system_package_manager", AsyncMock(return_value=manager))
    execute = AsyncMock()
    monkeypatch.setattr(agent, "exec_as_root", execute)
    environment = SimpleNamespace(
        exec=AsyncMock(return_value=SimpleNamespace(return_code=0)),
        upload_file=AsyncMock(),
    )
    asyncio.run(agent.install(environment))
    commands = [call.kwargs["command"] for call in execute.call_args_list]
    assert "ca-certificates" in commands[0]
    assert "tar -xzf" in commands[1]

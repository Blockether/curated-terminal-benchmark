import asyncio
import importlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harbor.agents.installed.base import BaseInstalledAgent

from ctb import toolkit
from ctb.agents.dirge import Dirge
from ctb.matrix import AGENTS


def harbor_class(path):
    module, _, name = path.partition(":")
    return getattr(importlib.import_module(module), name)


@pytest.mark.parametrize("name", list(AGENTS))
def test_every_agent_wraps_its_harbor_class_with_the_toolkit(name):
    mro = harbor_class(AGENTS[name].harbor).__mro__
    assert mro.index(toolkit.Toolkit) < mro.index(BaseInstalledAgent)


def test_every_agent_command_gets_the_toolkit_path():
    environment = AsyncMock()
    environment.exec.return_value = SimpleNamespace(return_code=0, stdout="", stderr="")
    dirge = Dirge(logs_dir=Path("/tmp/ctb-test-logs"), model_name="openai/gpt-6.1-sol")
    asyncio.run(dirge.exec_as_agent(environment, command="python3 -V"))
    command = environment.exec.await_args.kwargs["command"]
    assert toolkit.PATH_SETUP + "python3 -V" in command


class Recorder:
    def __init__(self):
        self.calls = []

    async def setup(self, environment):
        self.calls.append("agent setup")

    async def exec_as_root(self, environment, command):
        self.calls.append(command)

    async def exec_as_agent(self, environment, command, *args, **kwargs):
        self.calls.append(command)


class Agent(toolkit.Toolkit, Recorder):
    pass


def test_setup_needs_the_archive_and_installs_it_after_the_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(toolkit, "ARTIFACTS", tmp_path)
    environment, agent = AsyncMock(), Agent()
    with pytest.raises(FileNotFoundError, match="uv run ctb fetch"):
        asyncio.run(agent.setup(environment))
    assert agent.calls == []
    toolkit.archive().write_bytes(b"toolkit")
    asyncio.run(agent.setup(environment))
    environment.upload_file.assert_awaited_once_with(toolkit.archive(), toolkit.UPLOAD)
    assert agent.calls[0] == "agent setup"
    # A repeated setup replaces the old toolkit before it unpacks the new one.
    install = agent.calls[1]
    assert install.index(f"rm -rf {toolkit.TOOLKIT}") < install.index("tar -xzf")


def test_a_task_tool_comes_before_the_toolkit(tmp_path):
    for name, tools in {"task": ["python3"], "toolkit": ["python3", "pytest"]}.items():
        (tmp_path / name / "bin").mkdir(parents=True)
        for tool in tools:
            path = tmp_path / name / "bin" / tool
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)
    setup = toolkit.PATH_SETUP.replace(toolkit.TOOLKIT, str(tmp_path / "toolkit"))
    found = subprocess.run(
        ["/bin/bash", "-c", setup + "command -v python3 pytest"],
        env={"PATH": f"{tmp_path / 'task' / 'bin'}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert found == [str(tmp_path / "task/bin/python3"), str(tmp_path / "toolkit/bin/pytest")]


def test_build_replaces_the_archive_only_after_the_container_succeeds(tmp_path, monkeypatch):
    cpython = tmp_path / toolkit.CPYTHON_ASSET
    cpython.write_bytes(b"cpython")
    target = tmp_path / "toolkit.tar.gz"
    calls = []

    def podman(argv, check):
        calls.append(argv)
        out = next(a for a in argv if a.startswith("OUT=")).removeprefix("OUT=")
        (tmp_path / out).write_bytes(b"built")

    monkeypatch.setattr(toolkit.subprocess, "run", podman)
    assert toolkit.build(cpython, target) == target
    assert target.read_bytes() == b"built"
    assert not target.with_name(target.name + ".partial").exists()
    argv = calls[0]
    assert argv[:5] == ["podman", "run", "--rm", "--platform", "linux/amd64"]
    assert f"CPYTHON={toolkit.CPYTHON_ASSET}" in argv
    assert f"{toolkit.REQUIREMENTS}:/s/requirements.txt:ro" in argv


def test_the_archive_name_follows_the_requirements(tmp_path, monkeypatch):
    monkeypatch.setattr(toolkit, "REQUIREMENTS", tmp_path / "r.txt")
    toolkit.REQUIREMENTS.write_text("numpy==1\n")
    first = toolkit.archive()
    toolkit.REQUIREMENTS.write_text("numpy==2\n")
    assert toolkit.archive() != first
    assert first.name.startswith(f"ctb-python-{toolkit.CPYTHON_VERSION}-")


def test_shared_tools_are_explicit_and_inventory_is_checked():
    environment, agent = AsyncMock(), Agent()
    asyncio.run(toolkit.install(agent, environment))
    command = agent.calls[-1]
    assert "ctb-python" in command and "ctb-pytest" in command
    assert "toolkit-inventory.json" in command
    assert "importlib.metadata" in command
    assert "numpy" in command


def test_shared_instruction_is_the_same_for_all_agents():
    class RunRecorder:
        async def run(self, instruction, environment, context):
            return instruction

    class Shared(toolkit.Toolkit, RunRecorder):
        pass

    text = asyncio.run(Shared().run("Solve this task.", None, None))
    assert text.startswith("Solve this task.")
    assert "ctb-python" in text and "ctb-pytest" in text
    assert text.count(toolkit.SETUP_NOTE) == 1
    assert asyncio.run(Shared().run(text, None, None)) == text


@pytest.mark.parametrize("name", ["dirge", "kmet", "samizdat", "vis-solo", "vis-team", "opencode"])
def test_real_adapter_run_receives_shared_instruction(name, tmp_path, monkeypatch):
    from harbor.models.agent.context import AgentContext

    from ctb import credentials
    from ctb.agents import opencode

    instance = harbor_class(AGENTS[name].harbor)(logs_dir=tmp_path, model_name="openai/gpt-6.1-sol")
    captured = []

    def command(instruction):
        captured.append(instruction)
        return "true"

    monkeypatch.setattr(instance, "exec_as_agent", AsyncMock())
    monkeypatch.setattr(instance, "exec_as_root", AsyncMock())
    if name == "opencode":
        monkeypatch.setattr(
            credentials,
            "codex_vis",
            lambda _: credentials.Access("synthetic", 123, account_id="test"),
        )
        monkeypatch.setattr(opencode, "stage_files", AsyncMock())
        monkeypatch.setattr(instance, "run_command", command)
        monkeypatch.setattr(instance, "_watch", AsyncMock())
        monkeypatch.setattr(instance, "_collect", AsyncMock())
        monkeypatch.setattr(instance, "_error_messages", list)
    else:
        monkeypatch.setattr(instance, "stage", AsyncMock())
        monkeypatch.setattr(instance, "secret_files", dict)
        monkeypatch.setattr(instance, "secret_env", dict)
        monkeypatch.setattr(instance, "command", command)
    asyncio.run(instance.run("Solve this task.", AsyncMock(), AgentContext()))
    assert captured == [toolkit.shared_instruction("Solve this task.")]

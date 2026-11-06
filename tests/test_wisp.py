"""wisp over ctb.proxy: the pinned script, one non-stream turn per round."""

import asyncio
import json
import shlex
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harbor.models.agent.context import AgentContext

from ctb import cli
from ctb.agents import wisp
from ctb.agents.wisp import Wisp, usage_totals
from ctb.matrix import AGENTS, DEFAULT_AGENTS


def agent(logs=Path("/tmp/ctb-test-logs")):
    return Wisp(logs_dir=logs, model_name="openai/gpt-6.1-sol")


def tokens(context):
    return context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens


def test_wisp_is_a_proxy_arm_of_the_default_matrix():
    spec = AGENTS["wisp"]
    assert (spec.harbor, spec.families, spec.proxy) == ("ctb.agents.wisp:Wisp", ("openai",), True)
    assert "wisp" in DEFAULT_AGENTS.split(",")


def test_wisp_starts_a_pinned_session_and_runs_the_prompt(monkeypatch):
    monkeypatch.setenv("CTB_PROXY_URL", "http://host.containers.internal:9")
    monkeypatch.setenv("CTB_PROXY_KEY", "key-123")
    instruction = "do 'it'\n"
    command = agent().command(instruction)
    assert 'export PATH=/installed-agent/wisp-bin:"$PATH"' in command
    assert "WISP_SESSIONS_ROOT=/logs/agent/wisp-sessions WISP_LLM_STREAM=false;" in command
    # One turn is one chat body, so the proxy answers it; a percent guard needs /v1/model/info.
    assert "--context-guard" not in command
    assert (
        "wisp new --name wisp -m gpt-6.1-sol -t medium --env-in-system-prompt"
        " -s <(wisp build-system-prompt --wisp)" in command
    )
    assert 'wisp run -d "$session" < $HOME/problem.txt > /logs/agent/wisp.stream' in command
    assert shlex.quote(instruction) in command
    assert "key-123" not in command


def test_wisp_takes_the_proxy_address_and_key_from_the_environment(monkeypatch):
    monkeypatch.setenv("CTB_PROXY_URL", "http://host.containers.internal:9")
    monkeypatch.setenv("CTB_PROXY_KEY", "private-test-key")
    assert agent().secret_env() == {
        "WISP_LLM_URL": "http://host.containers.internal:9",
        "WISP_LLM_API_KEY": "private-test-key",
    }
    monkeypatch.delenv("CTB_PROXY_KEY")
    with pytest.raises(RuntimeError, match="CTB_PROXY_KEY"):
        agent().secret_env()


def test_wisp_sums_the_usage_of_every_turn(tmp_path):
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 7,
        "total_tokens": 107,
        "prompt_tokens_details": {"cached_tokens": 80},
    }
    second = {
        "prompt_tokens": 120,
        "completion_tokens": 3,
        "total_tokens": 123,
        "prompt_tokens_details": {"cached_tokens": 100},
    }
    directory = tmp_path / "wisp-sessions" / "wisp"
    directory.mkdir(parents=True)
    (directory / "wisp.usage").write_text(
        json.dumps(usage) + "\n" + "data: " + json.dumps(second) + "\nnot json\n"
    )
    context = AgentContext()
    agent(tmp_path).populate_context_post_run(context)
    # OpenAI counts cached tokens inside prompt tokens; the list price subtracts them.
    assert tokens(context) == (220, 180, 10)
    assert context.metadata == {"wisp": {"turns": 2}}


def test_wisp_without_a_usage_file_leaves_tokens_unknown(tmp_path):
    context = AgentContext()
    agent(tmp_path).populate_context_post_run(context)
    assert tokens(context) == (None, None, None)
    assert usage_totals("") == {"input": 0, "cached": 0, "output": 0, "turns": 0}


def test_wisp_unpacks_the_script_and_writes_the_path_shim(tmp_path, monkeypatch):
    archive = tmp_path / "wisp.tar.gz"
    archive.write_bytes(b"script")
    harness = agent(tmp_path / "logs")
    monkeypatch.setattr(harness, "ARCHIVE", archive)
    monkeypatch.setattr(harness, "_get_system_package_manager", AsyncMock(return_value="apt-get"))
    execute = AsyncMock()
    monkeypatch.setattr(harness, "exec_as_root", execute)
    environment = SimpleNamespace(
        exec=AsyncMock(return_value=SimpleNamespace(return_code=0)), upload_file=AsyncMock()
    )
    asyncio.run(harness.install(environment))
    environment.upload_file.assert_awaited_once_with(archive, "/tmp/ctb-agent.tar.gz")
    shim = execute.call_args_list[-1].kwargs["command"]
    assert "/installed-agent/wisp-bin/wisp" in shim
    assert "exec /installed-agent/ctb-python/bin/python3 /installed-agent/wisp" in shim
    monkeypatch.setattr(harness, "ARCHIVE", tmp_path / "missing.tar.gz")
    with pytest.raises(FileNotFoundError, match="uv run ctb fetch"):
        asyncio.run(harness.install(environment))


def test_fetch_wisp_packs_the_pinned_script(tmp_path, monkeypatch):
    checkout = tmp_path / "src" / "wisp"
    (checkout / "wisp").mkdir(parents=True)
    (checkout / "wisp" / "wisp").write_text("#!/usr/bin/env python3\n")
    seen = {}

    def fake_checkout(repo, commit, name):
        seen["checkout"] = (repo, commit, name)
        return checkout

    def fake_pack(source, archive, name):
        seen["pack"] = (source, archive, name)
        archive.write_bytes(b"packed")

    archive = tmp_path / wisp.ARCHIVE_NAME
    monkeypatch.setattr(cli, "checkout", fake_checkout)
    monkeypatch.setattr(cli, "pack", fake_pack)
    monkeypatch.setattr(Wisp, "ARCHIVE", archive)
    assert cli.fetch_wisp() == archive
    assert seen["checkout"] == (cli.WISP_REPO, wisp.COMMIT, "wisp")
    assert seen["pack"] == (checkout / "wisp" / "wisp", archive, "wisp")
    cli.fetch_wisp()
    assert seen["pack"] == (checkout / "wisp" / "wisp", archive, "wisp")
    assert cli.pins()["wisp"] == wisp.COMMIT

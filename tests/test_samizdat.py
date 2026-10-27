import json
from pathlib import Path

import pytest
from harbor.models.agent.context import AgentContext

from ctb import credentials, matrix
from ctb.agents.samizdat import Samizdat
from ctb.cli import host_env
from ctb.proxy import codex_body


@pytest.fixture
def proxy_vars(monkeypatch):
    monkeypatch.setenv("CTB_PROXY_URL", "http://host.containers.internal:5555")
    monkeypatch.setenv("CTB_PROXY_KEY", "k3y")


def samizdat(model, logs=Path("/tmp/ctb-test-logs")):
    return Samizdat(logs_dir=logs, model_name=model)


def test_samizdat_reaches_each_model_through_the_proxy(proxy_vars):
    opus = samizdat("anthropic/claude-opus-5-5").config()
    gpt = samizdat("openai/gpt-6.1-sol").config()
    assert ':base-url "http://host.containers.internal:5555/v1"' in opus
    assert ':model "claude-opus-5-5"' in opus and ':reasoning-effort "medium"' in opus
    # Opus refuses a forced tool_choice; only GPT gets the native feature.
    assert ":features #{:reasoning-effort}" in opus
    assert ":features #{:native-tool-choice :reasoning-effort}" in gpt
    assert ":approval {:mode :yolo}" in gpt and "k3y" not in gpt
    assert samizdat("openai/gpt-6.1-sol").secret_env() == {"CTB_PROXY_KEY": "k3y"}


def test_samizdat_command_runs_one_run_in_the_task_directory(proxy_vars):
    command = samizdat("openai/gpt-6.1-sol").command("say 'hi'")
    assert "HARNESS_ROOT=$root" in command and "samizdat_drive.py" in command
    assert "--headless" in command
    assert "HARNESS_DB=/logs/agent/samizdat.sqlite3" in command
    assert "printf %s 'say '\"'\"'hi'\"'\"'' > $HOME/problem.txt" in command
    assert command.endswith('rm -rf "$root/.samizdat"; exit $code')


def test_samizdat_needs_the_proxy(monkeypatch):
    monkeypatch.delenv("CTB_PROXY_URL", raising=False)
    monkeypatch.delenv("CTB_PROXY_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CTB_PROXY_URL"):
        samizdat("openai/gpt-6.1-sol").secret_env()
    trial = matrix.Trial(matrix.AGENTS["samizdat"], "openai", "t")
    with pytest.raises(credentials.CredentialError):
        host_env(trial, Path("/tmp"))


def test_host_env_gives_samizdat_the_proxy_url_and_key(tmp_path):
    class FakeProxy:
        port, key = 4242, "secret"

    env = host_env(matrix.Trial(matrix.AGENTS["samizdat"], "anthropic", "t"), tmp_path, FakeProxy())
    assert env["CTB_PROXY_URL"] == "http://host.containers.internal:4242"
    assert env["CTB_PROXY_KEY"] == "secret"


def test_samizdat_runs_on_both_models():
    jobs = [t.job for t in matrix.trials(["samizdat"], ["anthropic", "openai"], ["x"])]
    assert jobs == ["samizdat__anthropic__x", "samizdat__openai__x"]


def test_samizdat_tokens_come_from_the_saved_run(tmp_path, proxy_vars):
    usage = {"prompt-tokens": 100, "cache-hit-tokens": 80, "completion-tokens": 7, "turns": 3}
    (tmp_path / "samizdat-run.json").write_text(
        json.dumps({"run": {"status": "completed", "usage": usage}})
    )
    context = AgentContext()
    samizdat("anthropic/claude-opus-5-5", tmp_path).populate_context_post_run(context)
    assert (context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens) == (100, 80, 7)
    assert context.metadata == {"samizdat": {"status": "completed", "turns": 3}}


def test_codex_requests_of_one_conversation_share_a_prompt_cache_key():
    def body(*turns):
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
        messages += [{"role": "user", "content": t} for t in turns]
        return codex_body({"model": "gpt-6.1-sol", "messages": messages})

    other = codex_body({"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "b"}]})
    assert body()["prompt_cache_key"] == body("more")["prompt_cache_key"]
    assert body()["prompt_cache_key"] != other["prompt_cache_key"]


def test_codex_requests_get_the_gpt_settings_of_every_agent():
    body = codex_body(
        {
            "model": "gpt-6.1-sol",
            "messages": [{"role": "user", "content": "a"}],
            "reasoning_effort": "medium",
        }
    )
    assert body["text"] == {"verbosity": "low"}
    assert body["reasoning"] == {"effort": "medium", "summary": "auto"}


def test_samizdat_exports_full_turn_text(tmp_path):
    from ctb.agents.samizdat_drive import export_transcript

    calls = []

    def get(path):
        calls.append(path)
        if path.endswith("/branches/b"):
            return {"turns": [{"turn": 1}]}
        return {"turn": 1, "assistant_text": "Complete reply", "reasoning_text": "Reasoning"}

    output = tmp_path / "transcript.json"
    export_transcript("r", {"branches": [{"id": "b"}], "run": {}}, output, get=get)
    saved = json.loads(output.read_text())
    assert saved["branches"][0]["turns"][0]["assistant_text"] == "Complete reply"
    assert calls[-1] == "/v1/runs/r/branches/b/turns/1"


def test_samizdat_uses_released_binary_and_model_limits(proxy_vars):
    from ctb.agents.samizdat import ARCHIVE_NAME, VERSION

    instance = samizdat("anthropic/claude-opus-5-5")
    assert VERSION == "v0.1.0"
    assert instance.ARCHIVE.name == ARCHIVE_NAME
    assert ":context-window 1000000" in instance.config()
    assert ":max-tokens 128000" in instance.config()


def test_samizdat_installs_runtime_libraries(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ctb.agents.base import CuratedAgent

    monkeypatch.setattr(CuratedAgent, "install", AsyncMock())
    agent = samizdat("openai/gpt-6.1-sol", tmp_path)
    monkeypatch.setattr(agent, "_get_system_package_manager", AsyncMock(return_value="apt-get"))
    execute = AsyncMock()
    monkeypatch.setattr(agent, "exec_as_root", execute)
    environment = SimpleNamespace(
        exec=AsyncMock(return_value=SimpleNamespace(return_code=0)),
        upload_file=AsyncMock(),
    )
    asyncio.run(agent.install(environment))
    command = execute.call_args.kwargs["command"]
    assert "libsqlite3-0" in command
    assert "libssl3" in command
    environment.upload_file.assert_awaited_once()

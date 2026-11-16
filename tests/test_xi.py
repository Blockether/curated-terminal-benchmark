import asyncio
import json
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ctb import credentials
from ctb.agents import xi
from ctb.agents.xi import Xi


def agent(model="openai/gpt-6.1-sol") -> Xi:
    return Xi(logs_dir="/tmp/ctb-test-logs", model_name=model)


def test_xi_uses_only_the_codex_provider_and_allows_tool_calls():
    run = agent()
    assert ":providers [:openai]" in run.config()
    assert "{:match {} :action {:type :allow}}" in run.rules()


def test_xi_stages_the_subscription_login_and_xi_config(monkeypatch):
    access = credentials.Access("token", 4_000_000_000_000, "account", "id-token")
    monkeypatch.setattr(xi.credentials, "codex_vis", lambda _seconds: access)
    files = agent().secret_files()
    assert set(files) == {".codex/auth.json", ".config/xi/config.edn", ".config/xi/rules.edn"}
    auth = json.loads(files[".codex/auth.json"])
    assert auth["tokens"]["access_token"] == "token"
    assert auth["tokens"]["refresh_token"] == ""


def test_xi_runs_one_prompt_with_the_benchmark_model():
    instruction = "fix 'this'"
    command = agent().command(instruction)
    assert f"printf %s {shlex.quote(instruction)} | xi prompt" in command
    assert "--model openai/gpt-6.1-sol --json" in command
    assert "CODEX_HOME=$HOME/.codex" in command
    assert "> /logs/agent/xi.json 2> /logs/agent/xi-stderr.log" in command
    assert 'cp -a "$HOME/.config/xi/sessions/." /logs/agent/xi-sessions/' in command
    assert 'cp -a "$HOME/.claude/projects/." /logs/agent/xi-transcripts/' in command


def test_xi_rejects_another_model():
    with pytest.raises(ValueError, match="got 'anthropic/claude'"):
        agent("anthropic/claude")


def test_xi_installs_the_pinned_runtime_and_package(tmp_path):
    run = agent()
    run.exec_as_root = AsyncMock()
    run._get_system_package_manager = AsyncMock(return_value="apt-get")
    environment = SimpleNamespace(
        exec=AsyncMock(return_value=SimpleNamespace(return_code=0)),
        upload_file=AsyncMock(),
    )
    asyncio.run(run.install(environment))
    install = run.exec_as_root.await_args.kwargs["command"]
    assert xi.PACKAGE_URL.endswith("xi-agent-0.6.0.tgz")
    assert xi.PACKAGE_SHA256 in install
    assert "bun-v1.4.4/bun-linux-aarch64.zip" in install
    assert "bun-v1.4.4/bun-linux-x64-musl.zip" in install
    assert f'exec {xi.BUN} {xi.MAIN} "$@"' in install


def test_xi_reports_its_pinned_version():
    assert agent().get_version_command() == "printf %s 0.6.0"

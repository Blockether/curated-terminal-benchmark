import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harbor.models.agent.context import AgentContext

from ctb import arch, matrix
from ctb.agents import nodecode
from ctb.agents.base import CuratedAgent
from ctb.agents.nodecode import Nodecode
from ctb.agents.nodecode_drive import frame_event, turn_id_of
from ctb.cli import check_target_arch, host_env
from ctb.report.transcripts import read_nodecode


@pytest.fixture
def proxy_vars(monkeypatch):
    monkeypatch.setenv("CTB_PROXY_URL", "http://host.containers.internal:5555")
    monkeypatch.setenv("CTB_PROXY_KEY", "k3y")


def agent(model="openai/gpt-6.1-sol", logs=Path("/tmp/ctb-nodecode-logs")):
    return Nodecode(logs_dir=logs, model_name=model)


def test_nodecode_uses_the_proxy_and_medium_effort(proxy_vars):
    instance = agent()
    config = json.loads(instance.config())
    provider = config["providers"]["ctb"]
    assert provider["sdk"] == "openai-responses"
    assert provider["base_url"] == "http://host.containers.internal:5555/v1"
    assert provider["models"][0]["id"] == "gpt-6.1-sol"
    assert config["general"]["default_model"] == {
        "provider": "ctb",
        "model": "gpt-6.1-sol",
        "effort": "medium",
    }
    assert config["update"]["mode"] == "off"
    assert config["workspace"]["captures"] is False
    assert "k3y" not in instance.config()
    assert instance.secret_files()[nodecode.CATALOG] == "{}\n"
    assert instance.secret_env() == {"NODECODE_API_KEY": "k3y"}


def test_nodecode_command_runs_one_rpc_turn(proxy_vars):
    command = agent().command("say hi")
    assert "nodecode_drive.py" in command and nodecode.NODECODE in command
    assert "NODECODE_MODELS_CATALOG=$HOME/.nodecode/models.json" in command
    assert "NODECODE_MODEL=gpt-6.1-sol" in command
    assert "NODECODE_REASONING_EFFORT=medium" in command
    assert "CTB_PROBLEM_FILE=$HOME/problem.txt" in command
    assert "printf %s " in command and "say hi" in command


def test_nodecode_needs_the_proxy(monkeypatch):
    monkeypatch.delenv("CTB_PROXY_URL", raising=False)
    monkeypatch.delenv("CTB_PROXY_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CTB_PROXY_URL"):
        agent().secret_env()


def test_nodecode_reports_input_including_the_cache(tmp_path, proxy_vars):
    def line(input_tokens, cached, output_tokens):
        event = {
            "type": "turn.usage",
            "payload": {
                "input-tokens": input_tokens,
                "cached-input-tokens": cached,
                "output-tokens": output_tokens,
            },
        }
        return json.dumps({"payload": {"frame": {"event": event}}})

    (tmp_path / nodecode.FRAMES).write_text("\n".join([line(10, 2, 5), line(4, 1, 3)]) + "\n")
    context = AgentContext()
    agent(logs=tmp_path).populate_context_post_run(context)
    assert (context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens) == (17, 3, 8)
    assert context.metadata == {"nodecode": {"turns": 2}}


def test_driver_reads_the_turn_and_its_terminal_frame():
    line = json.dumps(
        {
            "payload": {
                "frame": {
                    "turn_id": "turn-1",
                    "event": {
                        "type": "turn.completed",
                        "metadata": {"turn_id": "turn-1"},
                    },
                }
            }
        }
    )
    payload, event = frame_event(line)
    assert event["type"] == "turn.completed"
    assert turn_id_of(payload, event) == "turn-1"
    result = json.dumps(
        {
            "payload": {
                "type": "gateway_turn_command_result",
                "result": {"response": {"turn_id": "turn-2"}},
            }
        }
    )
    payload, event = frame_event(result)
    assert turn_id_of(payload, event) == "turn-2"


def test_nodecode_is_an_amd64_experiment_arm():
    spec = matrix.AGENTS["nodecode"]
    assert spec.experiment and spec.proxy and spec.arch == nodecode.ARCH == "amd64"
    assert "nodecode" not in matrix.DEFAULT_AGENTS.split(",")
    assert [t.job for t in matrix.trials(["nodecode"], ["openai"], ["x"])] == [
        "nodecode__openai__x"
    ]


def test_nodecode_requires_an_amd64_target(monkeypatch):
    monkeypatch.setattr(arch, "ARCH", "arm64")
    with pytest.raises(SystemExit, match="nodecode needs amd64"):
        check_target_arch(["nodecode"])
    monkeypatch.setattr(arch, "ARCH", "amd64")
    check_target_arch(["nodecode"])


def test_nodecode_installs_the_driver_and_runtime_libraries(tmp_path, monkeypatch):
    monkeypatch.setattr(CuratedAgent, "install", AsyncMock())
    instance = agent(logs=tmp_path)
    monkeypatch.setattr(
        instance,
        "exec_as_root",
        AsyncMock(return_value=SimpleNamespace(stdout="ldd (Ubuntu GLIBC 2.35)")),
    )
    dependencies = AsyncMock()
    monkeypatch.setattr(instance, "ensure_system_dependencies", dependencies)
    environment = SimpleNamespace(upload_file=AsyncMock())
    asyncio.run(instance.install(environment))
    dependencies.assert_awaited_once_with(environment, ("nodecode_libraries",))
    commands = [call.kwargs["command"] for call in instance.exec_as_root.await_args_list]
    assert any("libssl3t64" in command for command in commands)
    environment.upload_file.assert_awaited_once()


def test_nodecode_refuses_a_musl_image(tmp_path, monkeypatch):
    monkeypatch.setattr(CuratedAgent, "install", AsyncMock())
    instance = agent(logs=tmp_path)
    monkeypatch.setattr(
        instance,
        "exec_as_root",
        AsyncMock(return_value=SimpleNamespace(stdout="musl libc (x86_64)")),
    )
    with pytest.raises(RuntimeError, match="musl"):
        asyncio.run(instance.install(SimpleNamespace(upload_file=AsyncMock())))


def test_nodecode_pins_the_release_and_its_checksum():
    assert nodecode.LABEL == nodecode.COMMIT[:9] == "cef1be5a3"
    assert nodecode.VERSION == "0.1.14"
    assert Nodecode.ARCHIVE.name == f"nodecode-{nodecode.LABEL}-x86_64-linux.tar.gz"
    assert nodecode.URL.endswith(Nodecode.ARCHIVE.name)
    assert len(nodecode.SHA256) == 64
    from ctb.cli import pins

    assert pins()["nodecode"] == "0.1.14+cef1be5a3"


def test_host_env_gives_nodecode_the_proxy_url_and_key(tmp_path, monkeypatch):
    monkeypatch.setattr("ctb.credentials.fresh_codex_vis", lambda *_a: None)

    class FakeProxy:
        port, key = 4242, "secret"

    trial = matrix.Trial(matrix.AGENTS["nodecode"], "openai", "t")
    env = host_env(trial, tmp_path, FakeProxy())
    assert env["CTB_PROXY_URL"] == "http://host.containers.internal:4242"
    assert env["CTB_PROXY_KEY"] == "secret"


def test_transcript_reader_keeps_calls_and_tools(tmp_path):
    def frame(kind, body):
        event = {
            "type": kind,
            "payload": body,
            "metadata": {"occurred_at": "2026-11-11T13:00:00.000Z"},
        }
        return json.dumps({"payload": {"frame": {"event": event}}})

    frames = [
        frame("turn.input_committed", {"role": "user", "message": "task"}),
        frame(
            "turn.tool_call_started",
            {
                "call-id": "call-1",
                "tool-name": "eval",
                "arguments": json.dumps({"form": "echo hi"}),
            },
        ),
        frame(
            "turn.tool_result",
            {
                "call-id": "call-1",
                "tool-name": "eval",
                "result": "hi",
                "failed": False,
                "duration-ms": 12,
            },
        ),
        frame(
            "turn.usage",
            {
                "input-tokens": 10,
                "cached-input-tokens": 2,
                "output-tokens": 5,
                "model": "gpt-6.1-sol",
                "cost_known": False,
            },
        ),
        frame(
            "turn.assistant_message_completed",
            {"role": "assistant", "message": {"role": "assistant", "content": "done"}},
        ),
    ]
    (tmp_path / "nodecode-rpc.jsonl").write_text(chr(10).join(frames) + chr(10))
    events = read_nodecode(tmp_path, "gpt-6.1-sol")
    assert [e["kind"] for e in events] == ["user", "tool", "call", "assistant"]
    assert events[0]["text"] == "task"
    assert events[1]["name"] == "eval"
    assert events[1]["output"] == "hi" and events[1]["error"] is False
    assert events[1]["ms"] == 12 and events[1]["ms_source"] == "recorded"
    assert events[2]["tokens"] == {"input": 12, "cache": 2, "output": 5}
    assert events[3]["text"] == "done"
    assert read_nodecode(tmp_path / "missing", "gpt-6.1-sol") == []


def test_fetch_nodecode_uses_the_pinned_url_and_checksum(monkeypatch):
    from ctb import cli

    seen = {}

    def download(url, path, sha256):
        seen.update(url=url, path=path, sha256=sha256)
        return path

    monkeypatch.setattr(cli, "download", download)
    assert cli.fetch_nodecode() == Nodecode.ARCHIVE
    assert seen == {
        "url": nodecode.URL,
        "path": Nodecode.ARCHIVE,
        "sha256": nodecode.SHA256,
    }


def test_fetch_can_select_the_nodecode_arm(tmp_path, monkeypatch):
    from ctb import cli

    monkeypatch.setattr(arch, "ARCH", "amd64")
    toolkit = tmp_path / "toolkit.tar.gz"
    archive = tmp_path / "nodecode.tar.gz"
    for path in (toolkit, archive):
        path.write_bytes(b"x")
    calls = []
    monkeypatch.setattr(cli, "fetch_toolkit", lambda: calls.append("toolkit") or toolkit)
    monkeypatch.setattr(cli, "fetch_nodecode", lambda: calls.append("nodecode") or archive)
    monkeypatch.setattr(cli, "fetch_kmet", lambda: calls.append("kmet") or toolkit)
    cli.fetch("nodecode")
    assert calls == ["toolkit", "nodecode"]

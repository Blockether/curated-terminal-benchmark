import argparse
import asyncio
import hashlib
import io
import json
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from harbor.models.agent.context import AgentContext

from ctb import cli
from ctb.agents import vis
from ctb.agents.dirge import Dirge
from ctb.agents.kmet import Kmet
from ctb.agents.vis import VisSolo, VisTeam
from ctb.cli import harbor_command
from ctb.matrix import AGENTS, Trial


def agent(cls, model, logs=Path("/tmp/ctb-test-logs")):
    return cls(logs_dir=logs, model_name=model)


def test_vis_team_uses_the_gateway_path_with_council_on():
    team = agent(VisTeam, "anthropic/claude-opus-5-5").command("do 'it'")
    solo = agent(VisSolo, "openai/gpt-6.1-sol").command("x")
    assert "--name team --persist --toggles council=true,draft_backend=off" in team
    assert "--db :memory" not in team
    assert "anthropic-coding-plan/claude-opus-5-5 --reasoning-effort medium" in team
    assert "--name solo --db :memory --toggles council=false" in solo
    assert "openai-codex/gpt-6.1-sol" in solo
    assert "--trajectory /logs/agent/trajectory.json --full-trace-json-stream" in solo
    assert "'do '\"'\"'it'\"'\"''" in team


BUILD = {
    "repo": "Blockether/vis",
    "run_id": 7,
    "commit": "a" * 40,
    "asset": "vis-agent-linux-x64.tar.gz",
}


def bundle_bytes(commit):
    """A gzip tar with only the build stamp of a Vis bundle."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        data = f"0.0.0 {commit} main 2026-01-01T00:00:00Z\n".encode()
        info = tarfile.TarInfo("./vis-agent-native.build")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def write_settings(path, build, profiles):
    lines = ["[build]"] + [f"{key} = {json.dumps(value)}" for key, value in build.items()]
    for name, profile in profiles.items():
        lines.append(f"[profiles.{name}]")
        lines += [f"{key} = {value}" for key, value in profile.items()]
    path.write_text("\n".join(lines) + "\n")


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    """A profiles.toml with a pinned fixture bundle under a temporary artifacts/."""
    data = bundle_bytes(BUILD["commit"])
    build = {**BUILD, "sha256": hashlib.sha256(data).hexdigest()}
    profiles = {
        "solo": {"persist": "false"},
        "lean": {
            "persist": "false",
            "effort": '"high"',
            "extensions": "[]",
            "toggles": '{ council = false, fold_hints = "off" }',
            "config": '{ providers = [{ id = "openai-codex", timeout_s = 9 }] }',
        },
    }
    write_settings(tmp_path / "profiles.toml", build, profiles)
    monkeypatch.setattr(vis, "SETTINGS", tmp_path / "profiles.toml")
    monkeypatch.setattr(vis, "ARTIFACTS", tmp_path / "artifacts")
    return build, data


def test_vis_profile_kwarg_sets_every_switch(pinned):
    lean = VisSolo(logs_dir=Path("/tmp/l"), model_name="openai/gpt-6.1-sol", profile="lean")
    command = lean.command("x")
    assert "--name lean --db :memory --toggles council=false,fold_hints=off" in command
    assert "--extensions none --model openai-codex/gpt-6.1-sol --reasoning-effort high" in command
    config = json.loads(command.split("printf %s ", 1)[1].split(" > ", 1)[0].strip("'"))
    # The overlay replaces a list; it merges only tables.
    assert config["providers"] == [{"id": "openai-codex", "timeout_s": 9}]
    assert config["default_model"] == "gpt-6.1-sol"
    with pytest.raises(ValueError, match="Unknown Vis profile 'nope'; use one of lean, solo"):
        VisSolo(logs_dir=Path("/tmp/l"), model_name="openai/gpt-6.1-sol", profile="nope")


def test_vis_api_key_provider_gets_the_key_only_in_the_environment(pinned, monkeypatch):
    monkeypatch.setenv("ZAI_CODING_API_KEY", "private-test-key")
    glm = VisSolo(logs_dir=Path("/tmp/l"), model_name="zai-coding-plan/glm-5.3-flash")
    assert glm.secret_files() == {}
    assert glm.secret_env() == {"ZAI_CODING_API_KEY": "private-test-key"}
    command = glm.command("x")
    assert "--model zai-coding-plan/glm-5.3-flash" in command
    assert "private-test-key" not in command
    monkeypatch.delenv("ZAI_CODING_API_KEY")
    with pytest.raises(RuntimeError, match="ZAI_CODING_API_KEY is required"):
        glm.secret_env()
    with pytest.raises(ValueError, match="got 'another/model'"):
        VisSolo(logs_dir=Path("/tmp/l"), model_name="another/model")


def test_vis_installs_only_the_pinned_build(pinned):
    _, data = pinned
    solo = VisSolo(logs_dir=Path("/tmp/l"), model_name="openai/gpt-6.1-sol")
    solo.exec_as_root = AsyncMock()
    environment = AsyncMock()
    with pytest.raises(FileNotFoundError, match="ctb.agents.vis fetch"):
        asyncio.run(solo.install(environment))
    solo.ARCHIVE.parent.mkdir(parents=True)
    solo.ARCHIVE.write_bytes(b"another build")
    with pytest.raises(ValueError, match="not the pinned"):
        asyncio.run(solo.install(environment))
    environment.upload_file.assert_not_awaited()
    solo.ARCHIVE.write_bytes(data)
    asyncio.run(solo.install(environment))
    environment.upload_file.assert_awaited_once_with(solo.ARCHIVE, "/tmp/ctb-agent.tar.gz")
    assert solo.ARCHIVE.parts[-3:] == ("vis-builds", "7", "vis-agent-linux-x64.tar.gz")


def test_vis_fetch_checks_the_commit_of_the_build(pinned, monkeypatch):
    build, data = pinned
    calls = []

    def download(argv, check):
        calls.append(argv)
        directory = Path(argv[argv.index("-D") + 1])
        (directory / build["asset"]).write_bytes(payload)

    monkeypatch.setattr(vis.subprocess, "run", download)
    payload = bundle_bytes("b" * 40)
    with pytest.raises(ValueError, match="built b+, not the pinned a+"):
        vis.fetch()
    payload = data
    path = vis.fetch()
    assert path.read_bytes() == data
    assert calls[-1][:6] == ["gh", "run", "download", "7", "-R", "Blockether/vis"]
    vis.fetch()
    assert len(calls) == 2


def test_vis_metrics_come_from_the_result_frame(pinned, tmp_path):
    frames = [
        {"event": "trace-chunk", "payload": {}},
        {
            "event": "result",
            "payload": {
                "status": "success",
                "tokens": {"input": 100, "cached": 80, "output": 7},
                "cost": {"total_cost": 0.5},
                "duration-ms": 900,
                "iteration-count": 3,
            },
        },
    ]
    (tmp_path / "vis-trace.jsonl").write_text("\n".join(map(json.dumps, frames)) + "\n")
    context = AgentContext()
    agent(VisSolo, "openai/gpt-6.1-sol", tmp_path).populate_context_post_run(context)
    assert tokens(context) == (100, 80, 7)
    assert context.cost_usd is None
    metadata = context.metadata["vis"]
    assert metadata["model"] == "openai/gpt-6.1-sol"
    assert metadata["profile"] == "solo"
    assert metadata["build"] == {"run_id": 7, "commit": "a" * 40, "sha256": pinned[0]["sha256"]}
    assert (metadata["duration_ms"], metadata["iteration_count"]) == (900, 3)
    assert metadata["estimated_metered_api_cost_usd"] == 0.5


def test_run_record_holds_the_vis_build_and_profiles_only_for_vis_runs(
    pinned, tmp_path, monkeypatch
):
    monkeypatch.setattr(cli, "git_state", lambda: {"commit": "c", "dirty": False})
    monkeypatch.setattr(cli, "pins", dict)
    monkeypatch.setattr(cli, "ARTIFACTS", tmp_path / "none")
    args = argparse.Namespace(
        agents="vis-solo,codex", models="m", tasks="all", parallel=1, timeout_multiplier=1.0
    )
    cli.record_run(tmp_path, args)
    cli.record_run(tmp_path, argparse.Namespace(**{**vars(args), "agents": "codex"}))
    first, second = json.loads((tmp_path / "ctb-runs.json").read_text())
    assert first["vis"]["build"]["run_id"] == 7
    assert first["vis"]["profiles"] == {"vis-solo": {"name": "solo", "persist": False}}
    assert "vis" not in second


def test_dirge_and_kmet_select_subscription_auth_and_medium_effort():
    dirge = agent(Dirge, "openai/gpt-6.1-sol").config()
    assert dirge["providers"]["openai"] == {
        "auth": "chatgpt",
        "model": "gpt-6.1-sol",
        "effort": "medium",
    }
    kmet = agent(Kmet, "openai/gpt-6.1-sol").command("x")
    assert "--model openai-codex/gpt-6.1-sol --thinking medium" in kmet
    assert (
        "KMET_CODING_AGENT_DIR=$HOME/.kmet/agent KMET_USAGE_FILE=/logs/agent/kmet-usage.json"
        in kmet
    )


def test_builtin_agents_get_effort_as_a_harbor_kwarg():
    command = harbor_command(Trial(AGENTS["codex"], "openai", "t"), Path("/j"), 0.25)
    assert command[command.index("-a") + 1] == "codex"
    assert command[-2:] == ["--ak", "reasoning_effort=medium"]


def tokens(context):
    return context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens


def test_dirge_tokens_come_from_the_result_event(tmp_path):
    usage = {
        "input_tokens": 100,
        "cached_input_tokens": 80,
        "cache_creation_input_tokens": 5,
        "output_tokens": 7,
    }
    lines = [{"type": "assistant"}, {"type": "result", "usage": usage}]
    (tmp_path / "dirge.jsonl").write_text("\n".join(map(json.dumps, lines)) + "\n")
    opus, gpt = AgentContext(), AgentContext()
    agent(Dirge, "anthropic/claude-opus-5-5", tmp_path).populate_context_post_run(opus)
    agent(Dirge, "openai/gpt-6.1-sol", tmp_path).populate_context_post_run(gpt)
    # Anthropic counts cached tokens apart from input_tokens; OpenAI counts them inside.
    assert tokens(opus) == (185, 80, 7)
    assert tokens(gpt) == (100, 80, 7)


def test_dirge_without_usage_leaves_tokens_unknown(tmp_path):
    (tmp_path / "dirge.jsonl").write_text(json.dumps({"type": "result"}) + "\n")
    context = AgentContext()
    agent(Dirge, "openai/gpt-6.1-sol", tmp_path).populate_context_post_run(context)
    assert tokens(context) == (None, None, None)


def test_kmet_tokens_come_from_the_usage_file(tmp_path):
    usage = {"input": 20, "output": 7, "cache_read": 80, "cache_write": 5}
    (tmp_path / "kmet-usage.json").write_text(json.dumps(usage))
    context = AgentContext()
    agent(Kmet, "openai/gpt-6.1-sol", tmp_path).populate_context_post_run(context)
    assert tokens(context) == (105, 80, 7)

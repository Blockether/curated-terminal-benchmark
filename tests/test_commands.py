import argparse
import asyncio
import hashlib
import io
import json
import shlex
import subprocess
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from harbor.models.agent.context import AgentContext

from ctb import cli, credentials
from ctb.agents import vis
from ctb.agents.dirge import Dirge
from ctb.agents.kmet import Kmet
from ctb.agents.opencode import (
    LOCAL_DATA,
    OpenCode,
    OpenCodeHangError,
    has_final_answer,
    opencode_auth_json,
    processes_command,
    stop_command,
    stopped_error,
    watchdog_verdict,
)
from ctb.agents.pi import Pi, pi_auth_json
from ctb.agents.vis import VisSolo, VisTeam, VisTeamActive, VisTeamNote, VisTeamWait
from ctb.cli import harbor_command
from ctb.matrix import AGENTS, Trial


def agent(cls, model, logs=Path("/tmp/ctb-test-logs")):
    return cls(logs_dir=logs, model_name=model)


def test_vis_team_uses_the_gateway_path_with_council_on():
    team = agent(VisTeam, "anthropic/claude-opus-5-5").command("do 'it'")
    solo = agent(VisSolo, "openai/gpt-6.1-sol").command("x")
    assert "--name team --persist --toggles verbosity=low,council=true,draft_backend=off" in team
    assert "--db :memory" not in team
    assert "anthropic-coding-plan/claude-opus-5-5 --reasoning-effort medium" in team
    assert "--name solo --db :memory --toggles verbosity=low,council=false" in solo
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
    assert (
        "--name lean --db :memory --toggles verbosity=low,council=false,fold_hints=off" in command
    )
    assert "--extensions none --model openai-codex/gpt-6.1-sol --reasoning-effort high" in command
    config = json.loads(command.split("printf %s ", 1)[1].split(" > ", 1)[0].strip("'"))
    # The overlay replaces a list; it merges only tables.
    assert config["providers"] == [{"id": "openai-codex", "timeout_s": 9}]
    assert config["default_model"] == "gpt-6.1-sol"
    with pytest.raises(ValueError, match="Unknown Vis profile 'nope'; use one of lean, solo"):
        VisSolo(logs_dir=Path("/tmp/l"), model_name="openai/gpt-6.1-sol", profile="nope")


def test_vis_council_arms_add_the_note_and_their_orchestration_toggle():
    note = "Use Council subagents for this task: delegate independent parts with council.run."
    expected = shlex.quote(f"do 'it'\n\n{note}")
    for cls, mode in ((VisTeamWait, "wait"), (VisTeamActive, "active")):
        command = agent(cls, "openai/gpt-6.1-sol").command("do 'it'")
        toggles = f"council=true,draft_backend=off,council_orchestration={mode}"
        assert f"--name team-{mode} --persist --toggles verbosity=low,{toggles}" in command
        assert f"-- {expected} >" in command
    command = agent(VisTeamNote, "openai/gpt-6.1-sol").command("do 'it'")
    assert "council=true,draft_backend=off --model" in command
    assert f"-- {expected} >" in command
    # Profiles without a note keep the task text as it is.
    assert "-- x >" in agent(VisTeam, "openai/gpt-6.1-sol").command("x")
    names = [AGENTS[name].harbor.rpartition(":")[2] for name in vis.AGENT_PROFILES]
    assert names == ["VisSolo", "VisTeam", "VisTeamNote", "VisTeamWait", "VisTeamActive"]
    assert {cls.name() for cls in (VisTeamWait, VisTeamActive)} == {
        "vis-team-wait",
        "vis-team-active",
    }


def test_vis_pin_takes_a_run_of_any_branch(pinned, monkeypatch, tmp_path):
    build, data = pinned
    calls = []

    def gh(argv, check, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gh", "run", "view"]:
            head = json.dumps({"headSha": "a" * 40, "conclusion": "success"})
            return subprocess.CompletedProcess(argv, 0, stdout=head)
        (Path(argv[argv.index("-D") + 1]) / build["asset"]).write_bytes(data)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(vis.subprocess, "run", gh)
    found = vis.run_build(42)
    assert (found["run_id"], found["commit"], found["sha256"]) == (42, "a" * 40, build["sha256"])
    assert vis.archive(found).parts[-2] == "42"
    vis.write_pin(found)
    assert vis.settings()["build"]["run_id"] == 42
    assert vis.settings()["profiles"]["solo"] == {"persist": False}
    assert vis.fetch() == vis.archive(found)
    assert len(calls) == 2


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
    solo._get_system_package_manager = AsyncMock(return_value="apt-get")
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
                "content": [
                    {"type": "prose", "markdown": "Done."},
                    {"type": "error", "message": "Provider quota exhausted"},
                ],
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
    assert metadata["instruction_note"] is None
    assert metadata["build"] == {"run_id": 7, "commit": "a" * 40, "sha256": pinned[0]["sha256"]}
    assert (metadata["duration_ms"], metadata["iteration_count"]) == (900, 3)
    assert metadata["estimated_metered_api_cost_usd"] == 0.5
    assert metadata["error_count"] == 1


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


def test_run_pins_hold_the_version_of_each_harness():
    assert cli.pins()["harnesses"] == {
        "claude-code": "2.1.302",
        "codex": "0.163.0",
        "opencode": "1.19.3",
        "pi": "1.3.0",
    }


def test_parallel_run_records_keep_every_record(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "git_state", lambda: {"commit": "c", "dirty": False})
    monkeypatch.setattr(cli, "pins", dict)
    monkeypatch.setattr(cli, "ARTIFACTS", tmp_path / "none")
    args = argparse.Namespace(
        agents="codex", models="m", tasks="all", parallel=1, timeout_multiplier=1.0
    )
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda _: cli.record_run(tmp_path, args), range(16)))
    assert len(json.loads((tmp_path / "ctb-runs.json").read_text())) == 16
    assert not (tmp_path / "ctb-runs.json.tmp").exists()


def test_dirge_and_kmet_select_subscription_auth_and_medium_effort():
    dirge = agent(Dirge, "openai/gpt-6.1-sol").config()
    assert dirge["providers"]["openai"] == {
        "auth": "chatgpt",
        "model": "gpt-6.1-sol",
        "effort": "medium",
    }
    command = agent(Dirge, "openai/gpt-6.1-sol").command("x")
    assert "DIRGE_CODEX_TEXT_VERBOSITY=low DIRGE_CODEX_REASONING_SUMMARY=auto;" in command
    kmet = agent(Kmet, "openai/gpt-6.1-sol").command("x")
    assert "--model openai-codex/gpt-6.1-sol --thinking medium" in kmet
    assert "KMET_CODING_AGENT_DIR=$HOME/.kmet/agent" in kmet
    assert "--mode edn --session-dir /logs/agent/kmet-sessions" in kmet


def test_builtin_agents_get_effort_and_a_pinned_version_as_harbor_kwargs():
    command = harbor_command(Trial(AGENTS["codex"], "openai", "t"), Path("/j"), 0.25)
    assert command[command.index("-a") + 1] == "codex"
    assert command[-6:] == [
        "--ak",
        "reasoning_effort=medium",
        "--ak",
        "reasoning_summary=auto",
        "--ak",
        "version=0.163.0",
    ]
    claude = harbor_command(Trial(AGENTS["claude-code"], "anthropic", "t"), Path("/j"), 0.25)
    assert claude[-2:] == ["--ak", "version=2.1.302"]


def test_agent_setup_gets_more_time_than_the_harbor_default():
    command = harbor_command(Trial(AGENTS["codex"], "openai", "t"), Path("/j"), 0.25)
    assert command[command.index("--agent-setup-timeout-multiplier") + 1] == "5"


def test_opencode_and_pi_use_the_chatgpt_subscription_with_medium_effort():
    opencode = agent(OpenCode, "openai/gpt-6.1-sol")
    assert opencode.options.variant == "medium"
    assert opencode._version == "1.19.3"
    pi = Pi(
        logs_dir=Path("/tmp/ctb-test-logs"),
        model_name="openai/gpt-6.1-sol",
        reasoning_effort="medium",
    )
    assert pi.options.thinking == "medium"
    assert pi._version == "1.3.0"
    assert pi.model_connection.provider == "openai-codex"
    command = harbor_command(Trial(AGENTS["pi"], "openai", "t"), Path("/j"), 0.25)
    assert command[-2:] == ["--ak", "reasoning_effort=medium"]


def test_opencode_and_pi_use_pinned_anthropic_plugins_with_medium_effort():
    opencode = agent(OpenCode, "anthropic/claude-opus-5-5")
    pi = agent(Pi, "anthropic/claude-opus-5-5")
    assert opencode.options.variant == "medium"
    assert "@ex-machina/opencode-anthropic-auth@1.8.6" in opencode._build_register_config_command()
    assert pi.options.thinking == "medium"
    assert pi.model_connection.provider == "anthropic"
    assert "--extension npm:@gotgenes/pi-anthropic-auth@3.4.3" in pi.build_cli_flags()
    assert "--extension" not in agent(Pi, "openai/gpt-6.1-sol").build_cli_flags()
    access = credentials.Access("A", 123)
    for auth in (pi_auth_json, opencode_auth_json):
        assert json.loads(auth(access, "anthropic")) == {
            "anthropic": {"type": "oauth", "access": "A", "refresh": "", "expires": 123}
        }
    for name in ("pi", "opencode"):
        assert AGENTS[name].families == ("anthropic", "openai")


def test_opencode_opus_output_limit_is_128k_without_changing_gpt(monkeypatch):
    calls = []

    async def fake_exec(self, environment, command, env=None, **kwargs):
        calls.append(env)

    monkeypatch.setattr(OpenCode.__mro__[1], "exec_as_agent", fake_exec)
    opus = agent(OpenCode, "anthropic/claude-opus-5-5")
    gpt = agent(OpenCode, "openai/gpt-6.1-sol")
    assert opus._opencode_config["provider"]["anthropic"]["models"]["claude-opus-5-5"]["limit"] == {
        "context": 1000000,
        "output": 128000,
    }
    # OpenCode sets GPT verbosity only for gpt-5.x ids, so ctb sends pi's `low` itself.
    assert gpt._opencode_config["provider"] == {
        "openai": {"models": {"gpt-6.1-sol": {"options": {"textVerbosity": "low"}}}}
    }
    asyncio.run(opus.exec_as_agent(None, command="opencode", env={"A": "1"}))
    asyncio.run(gpt.exec_as_agent(None, command="opencode", env={"A": "1"}))
    assert calls == [
        {"A": "1", "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX": "128000"},
        {"A": "1"},
    ]


def test_opencode_and_pi_logins_cannot_rotate_the_host_login():
    access = credentials.Access("A", 123, account_id="acc")
    assert json.loads(opencode_auth_json(access)) == {
        "openai": {
            "type": "oauth",
            "access": "A",
            "refresh": "",
            "expires": 123,
            "accountId": "acc",
        }
    }
    assert json.loads(pi_auth_json(access))["openai-codex"]["refresh"] == ""


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


def test_kmet_tokens_come_from_completed_native_messages(tmp_path):
    (tmp_path / "kmet.edn").write_text(
        "{:type :message-update :message {:usage {:input 999}}}\n"
        "{:type :message-end :message {:role :assistant :usage "
        "{:input_tokens 105 :output_tokens 7 :input_tokens_details {:cached_tokens 80 :cache_write_tokens 5}}}}\n"
    )
    context = AgentContext()
    agent(Kmet, "openai/gpt-6.1-sol", tmp_path).populate_context_post_run(context)
    assert tokens(context) == (105, 80, 7)


def opencode_text(phase):
    part = {"type": "text", "text": "done", "metadata": {"openai": {"phase": phase}}}
    return json.dumps({"type": "text", "part": part})


def test_opencode_watchdog_stops_a_finished_run_that_does_not_exit():
    # Baseline payments-pipeline-fix: a final answer after 11 minutes, then 109 idle minutes.
    output = "\n".join(["not json", opencode_text("commentary"), opencode_text("final_answer")])
    assert has_final_answer(output)
    assert watchdog_verdict(output, idle_s=119, idle_limit_s=1800) is None
    assert watchdog_verdict(output, idle_s=120, idle_limit_s=1800) == "final"


def test_opencode_watchdog_reports_a_silent_run_without_a_final_answer_as_a_hang():
    # Baseline interleaved-vigenere: OpenCode wrote nothing for 112 minutes after a tool call.
    output = opencode_text("commentary")
    assert not has_final_answer(output)
    assert watchdog_verdict(output, idle_s=1799, idle_limit_s=1800) is None
    assert watchdog_verdict(output, idle_s=1800, idle_limit_s=1800) == "hang"


def test_opencode_data_stays_off_the_host_mounted_logs_directory():
    opencode = agent(OpenCode, "openai/gpt-6.1-sol")
    calls = []

    async def fake_exec(self, environment, command, env=None, **kwargs):
        calls.append(env)

    original = OpenCode.__mro__[1].exec_as_agent
    OpenCode.__mro__[1].exec_as_agent = fake_exec
    try:
        env = {
            "XDG_DATA_HOME": "/logs/agent/opencode/xdg-data",
            "XDG_STATE_HOME": "/logs/x",
            "A": "1",
        }
        asyncio.run(opencode.exec_as_agent(None, command="opencode", env=env))
        asyncio.run(opencode.exec_as_agent(None, command="true"))
    finally:
        OpenCode.__mro__[1].exec_as_agent = original
    assert calls[0] == {
        "XDG_DATA_HOME": f"{LOCAL_DATA}/xdg-data",
        "XDG_STATE_HOME": f"{LOCAL_DATA}/xdg-state",
        "A": "1",
    }
    assert calls[1] is None


def test_opencode_stop_command_does_not_match_its_own_shell():
    command = stop_command("TERM")
    assert "opencode --model=" not in command
    assert "kill -TERM" in command and "pkill" not in command


def test_opencode_stopped_after_a_final_answer_is_not_a_failure():
    # Harbor runs OpenCode with pipefail, so the stopped run fails with exit 143.
    exit_143 = RuntimeError("Command failed (exit 143)")
    assert stopped_error(["final"], exit_143) is None
    assert isinstance(stopped_error(["hang"], exit_143), OpenCodeHangError)
    assert isinstance(stopped_error(["hang"], None), OpenCodeHangError)
    assert stopped_error([], exit_143) is exit_143
    assert stopped_error([], None) is None


def test_opencode_hang_still_runs_the_verifier():
    # Harbor verifies the task after NonZeroAgentExitCodeError, not after other errors.
    from harbor.agents.installed.base import NonZeroAgentExitCodeError

    assert issubclass(OpenCodeHangError, NonZeroAgentExitCodeError)


def test_opencode_hang_keeps_a_process_list_in_the_trial_logs():
    command = processes_command("/logs/agent/opencode/processes.txt")
    assert "/proc/[0-9]*" in command and "wchan" in command
    assert command.endswith("> /logs/agent/opencode/processes.txt 2>/dev/null; true")


def test_dirge_uses_the_same_chatgpt_account_as_other_harnesses(monkeypatch):
    access = credentials.Access("vis-access", 9999999999999, "vis-account")
    monkeypatch.setattr(credentials, "codex_vis", lambda minimum: access)
    monkeypatch.setattr(credentials, "codex_cli", lambda minimum: credentials.Access("other", 1))
    auth = json.loads(agent(Dirge, "openai/gpt-6.1-sol").secret_files()[".codex/auth.json"])
    assert auth["tokens"]["access_token"] == "vis-access"
    assert auth["tokens"]["account_id"] == "vis-account"


def test_kmet_ignores_interleaved_tool_events_for_usage(tmp_path):
    (tmp_path / "kmet.edn").write_text(
        '{:type :tool-execution-end :content "partial{:type :tool-execution-end :id "a|b"}\n'
        "{:type :message-end :message {:role :assistant :usage "
        "{:input_tokens 105 :output_tokens 7 :input_tokens_details {:cached_tokens 80}}}}\n"
    )
    context = AgentContext()
    agent(Kmet, "openai/gpt-6.1-sol", tmp_path).populate_context_post_run(context)
    assert tokens(context) == (105, 80, 7)

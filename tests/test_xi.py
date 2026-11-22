import json
import shlex
from types import SimpleNamespace

import pytest

from ctb import credentials
from ctb.agents import xi
from ctb.agents.xi import Xi
from ctb.report import transcripts
from ctb.report.trials import summarize


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


def test_xi_installs_the_pinned_runtime_next_to_the_patched_package():
    install = xi.install_command()
    assert xi.Xi.ARCHIVE.name == f"xi-0.6.0-{xi.PATCH_SHA.hexdigest()[:8]}.tar.gz"
    assert "bun-v1.4.4/bun-linux-aarch64.zip" in install
    assert "bun-v1.4.4/bun-linux-x64-musl.zip" in install
    assert f'exec {xi.BUN} {xi.MAIN} "$@"' in install


def test_xi_gives_gpt_the_settings_of_every_agent():
    command = agent().command("x")
    assert "XI_CODEX_REASONING_EFFORT=medium " in command
    assert "XI_CODEX_TEXT_VERBOSITY=low " in command
    assert "XI_CODEX_REASONING_SUMMARY=auto " in command
    assert "XI_TRACE_LOG=/logs/agent/xi-trace.jsonl;" in command


TRACE = (
    '{"type": "call", "at": 1000, "ms": 50, "usage": {"input_tokens": 100, "output_tokens": 7,'
    ' "cache_read_input_tokens": 900}, "calls": ["c1"]}\n'
    '{"type": "tool", "id": "c1", "name": "ls", "at": 1060, "ms": 12, "error": true}\n'
    '{"type": "call", "at": 1080, "ms": 30, "usage": {"input_tokens": 20, "output_tokens": 3}, "calls": []}\n'
)


def test_xi_counts_the_usage_of_every_request(tmp_path):
    run = Xi(logs_dir=tmp_path, model_name="openai/gpt-6.1-sol")
    (tmp_path / xi.TRACE).write_text(TRACE)
    context = SimpleNamespace(n_input_tokens=None, n_cache_tokens=None, n_output_tokens=None)
    run.populate_context_post_run(context)
    assert (context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens) == (
        1020,
        900,
        10,
    )


def test_xi_transcript_has_the_calls_and_tool_times_that_the_report_needs(tmp_path):
    (tmp_path / xi.TRACE).write_text(TRACE)
    session = tmp_path / "xi-transcripts" / "-app"
    session.mkdir(parents=True)
    records = [
        {"type": "user", "message": {"role": "user", "content": "task"}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "c1", "name": "ls", "input": {}}]},
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "c1",
                        "content": [{"type": "text", "text": "no"}],
                        "is_error": True,
                    }
                ]
            },
        },
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
    ]
    (session / "s.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    events = transcripts.read_xi(tmp_path, "openai/gpt-6.1-sol")
    assert [e["kind"] for e in events] == ["user", "call", "tool", "call", "assistant"]
    summary = summarize(events)
    assert (summary["provider_calls"], summary["provider_ms"], summary["tool_calls"]) == (2, 80, 1)
    assert (summary["tool_errors"], summary["tool_ms"], summary["input"], summary["cache"]) == (
        1,
        12,
        1020,
        900,
    )


def test_xi_reports_its_pinned_version():
    assert agent().get_version_command() == "printf %s 0.6.0"

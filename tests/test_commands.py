import json
from pathlib import Path

from harbor.models.agent.context import AgentContext

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
    assert "--persist --toggles council=true" in team and "--db :memory" not in team
    assert "anthropic-coding-plan/claude-opus-5-5 --reasoning-effort medium" in team
    assert "council=false" in solo and "openai-codex/gpt-6.1-sol" in solo
    assert "'do '\"'\"'it'\"'\"''" in team


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

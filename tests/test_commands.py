from pathlib import Path

from ctb.agents.dirge import Dirge
from ctb.agents.kmet import Kmet
from ctb.agents.vis import VisSolo, VisTeam
from ctb.cli import harbor_command
from ctb.matrix import AGENTS, Trial


def agent(cls, model):
    return cls(logs_dir=Path("/tmp/ctb-test-logs"), model_name=model)


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
    assert "KMET_CODING_AGENT_DIR=$HOME/.kmet/agent" in kmet


def test_builtin_agents_get_effort_as_a_harbor_kwarg():
    command = harbor_command(Trial(AGENTS["codex"], "openai", "t"), Path("/j"), 0.25)
    assert command[command.index("-a") + 1] == "codex"
    assert command[-2:] == ["--ak", "reasoning_effort=medium"]

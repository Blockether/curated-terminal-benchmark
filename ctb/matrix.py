"""Agents, models and tasks of the curated comparison."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = {"anthropic": "anthropic/claude-opus-5-5", "openai": "openai/gpt-6.1-sol"}


@dataclass(frozen=True)
class AgentSpec:
    name: str
    harbor: str
    families: tuple[str, ...]
    effort_kwarg: bool = False


AGENTS = {
    spec.name: spec
    for spec in (
        AgentSpec("vis-solo", "ctb.agents.vis:VisSolo", ("anthropic", "openai")),
        AgentSpec("vis-team", "ctb.agents.vis:VisTeam", ("anthropic", "openai")),
        AgentSpec("claude-code", "claude-code", ("anthropic",), effort_kwarg=True),
        AgentSpec("codex", "codex", ("openai",), effort_kwarg=True),
        AgentSpec("dirge", "ctb.agents.dirge:Dirge", ("anthropic", "openai")),
        # kmet has no Anthropic OAuth transport, so it runs only on the GPT model.
        AgentSpec("kmet", "ctb.agents.kmet:Kmet", ("openai",)),
    )
}


@dataclass(frozen=True)
class Trial:
    agent: AgentSpec
    family: str
    task: str

    @property
    def model(self) -> str:
        return MODELS[self.family]

    @property
    def job(self) -> str:
        return f"{self.agent.name}__{self.family}__{self.task}"


def load_tasks(path: Path = ROOT / "tasks.toml") -> list[dict]:
    return tomllib.loads(path.read_text())["task"]


def select_tasks(selector: str, tasks: list[dict]) -> list[str]:
    """`all`, `easy`, `hard`, or a comma-separated list of task names."""
    if selector == "all":
        return [t["name"] for t in tasks]
    if selector in {"easy", "hard"}:
        return [t["name"] for t in tasks if t["level"] == selector]
    names = [n.strip() for n in selector.split(",") if n.strip()]
    known = {t["name"] for t in tasks}
    unknown = sorted(set(names) - known)
    if unknown:
        raise ValueError(f"Unknown tasks: {', '.join(unknown)}")
    return names


def trials(agents: list[str], families: list[str], tasks: list[str]) -> list[Trial]:
    """Every supported agent and model pair for each task, task-major."""
    out = []
    for task in tasks:
        for name in agents:
            spec = AGENTS[name]
            out.extend(Trial(spec, family, task) for family in families if family in spec.families)
    return out

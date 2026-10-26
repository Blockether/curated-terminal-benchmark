"""Agents, models and tasks of the curated comparison."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from ctb.agents.base import REASONING_SUMMARY

ROOT = Path(__file__).resolve().parents[1]
MODELS = {"anthropic": "anthropic/claude-opus-5-5", "openai": "openai/gpt-6.1-sol"}


@dataclass(frozen=True)
class AgentSpec:
    name: str
    harbor: str
    families: tuple[str, ...]
    effort_kwarg: bool = False
    #: Further Harbor `--ak` kwargs, for example the reasoning summary of Codex.
    agent_kwargs: tuple[str, ...] = ()
    proxy: bool = False
    #: The pinned version of a Harbor built-in agent; Harbor otherwise installs the latest one.
    version: str | None = None
    #: An experiment arm runs only when --agents names it, never in the default matrix.
    experiment: bool = False


AGENTS = {
    spec.name: spec
    for spec in (
        AgentSpec("vis-solo", "ctb.agents.vis:VisSolo", ("anthropic", "openai")),
        AgentSpec("vis-team", "ctb.agents.vis:VisTeam", ("anthropic", "openai")),
        # Council arms of exp/council-orchestration: each task text asks for subagents.
        *(
            AgentSpec(name, f"ctb.agents.vis:{cls}", ("anthropic", "openai"), experiment=True)
            for name, cls in (
                ("vis-team-note", "VisTeamNote"),
                ("vis-team-wait", "VisTeamWait"),
                ("vis-team-active", "VisTeamActive"),
                ("vis-team-lead", "VisTeamLead"),
                ("vis-team-lead3", "VisTeamLead3"),
                ("vis-solo-x", "VisSoloX"),
                ("vis-solo-main", "VisSoloMain"),
                ("vis-lead", "VisLead"),
                ("vis-solo-next", "VisSoloNext"),
                ("vis-lead-auto", "VisLeadAuto"),
                ("vis-solo-v4", "VisSoloV4"),
                ("vis-lead-v4", "VisLeadV4"),
                ("vis-solo-v4n", "VisSoloV4Note"),
                ("vis-solo-v5", "VisSoloV5"),
                ("vis-lead-v5", "VisLeadV5"),
                ("vis-solo-v6", "VisSoloV6"),
                ("vis-lead-v6", "VisLeadV6"),
                ("vis-solo-v7", "VisSoloV7"),
                ("vis-lead-v7", "VisLeadV7"),
                ("vis-solo-v8", "VisSoloV8"),
                ("vis-lead-v8", "VisLeadV8"),
                ("vis-solo-v9", "VisSoloV9"),
                ("vis-lead-v9", "VisLeadV9"),
            )
        ),
        AgentSpec(
            "claude-code", "claude-code", ("anthropic",), effort_kwarg=True, version="2.1.302"
        ),
        AgentSpec(
            "codex",
            "codex",
            ("openai",),
            effort_kwarg=True,
            agent_kwargs=(f"reasoning_summary={REASONING_SUMMARY}",),
            version="0.163.0",
        ),
        AgentSpec("dirge", "ctb.agents.dirge:Dirge", ("anthropic", "openai")),
        # kmet has no Anthropic OAuth transport, so it runs only on the GPT model.
        AgentSpec("kmet", "ctb.agents.kmet:Kmet", ("openai",)),
        # samizdat reaches both models through the host proxy (ctb.proxy).
        AgentSpec("samizdat", "ctb.agents.samizdat:Samizdat", ("anthropic", "openai"), proxy=True),
        # Pinned OAuth plugins enable Claude; GPT uses each harness's native subscription login.
        AgentSpec("opencode", "ctb.agents.opencode:OpenCode", ("anthropic", "openai")),
        AgentSpec("pi", "ctb.agents.pi:Pi", ("anthropic", "openai"), effort_kwarg=True),
    )
}


#: The agents of a run or seal without --agents.
DEFAULT_AGENTS = ",".join(name for name, spec in AGENTS.items() if not spec.experiment)


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

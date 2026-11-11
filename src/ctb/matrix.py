"""Agents, models and tasks of the curated comparison."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from ctb.agents.base import REASONING_SUMMARY
from ctb.paths import ROOT

MODELS = {"openai": "openai/gpt-6.1-sol"}


@dataclass(frozen=True)
class AgentSpec:
    name: str
    harbor: str
    families: tuple[str, ...]
    effort_kwarg: bool = False
    #: Further Harbor `--ak` kwargs, for example the reasoning summary of Codex.
    agent_kwargs: tuple[str, ...] = ()
    proxy: bool = False
    #: The only target CPU this arm runs on; None for every CPU.
    arch: str | None = None
    #: The pinned version of a Harbor built-in agent; Harbor otherwise installs the latest one.
    version: str | None = None
    #: An experiment arm runs only when --agents names it, never in the default matrix.
    experiment: bool = False
    #: A paused arm also runs only when --agents names it. Dirge and Samizdat are paused:
    #: they used the most time and cost and passed 2 and 3 of 12 tasks.
    paused: bool = False


AGENTS = {
    spec.name: spec
    for spec in (
        AgentSpec("vis-solo", "ctb.agents.vis:VisSolo", ("openai",)),
        AgentSpec("vis-team", "ctb.agents.vis:VisTeam", ("openai",)),
        AgentSpec(
            "codex",
            "ctb.agents.codex:Codex",
            ("openai",),
            effort_kwarg=True,
            agent_kwargs=(f"reasoning_summary={REASONING_SUMMARY}",),
            version="0.166.0",
        ),
        AgentSpec("dirge", "ctb.agents.dirge:Dirge", ("openai",), paused=True),
        AgentSpec("kmet", "ctb.agents.kmet:Kmet", ("openai",)),
        # The same kmet arm with only the eval tool selected; run it by name.
        AgentSpec(
            "kmet-eval-only",
            "ctb.agents.kmet:KmetEvalOnly",
            ("openai",),
            experiment=True,
        ),
        # Nodecode publishes no Linux arm64 build, so it runs only on an amd64 target.
        AgentSpec(
            "nodecode",
            "ctb.agents.nodecode:Nodecode",
            ("openai",),
            proxy=True,
            arch="amd64",
            experiment=True,
        ),
        # Samizdat reaches GPT through the host proxy (ctb.proxy).
        AgentSpec("samizdat", "ctb.agents.samizdat:Samizdat", ("openai",), proxy=True, paused=True),
        # Wisp cannot refresh a ChatGPT token, so it also uses the host proxy.
        AgentSpec("wisp", "ctb.agents.wisp:Wisp", ("openai",), proxy=True),
        # GPT uses each harness's native subscription login.
        AgentSpec("opencode", "ctb.agents.opencode:OpenCode", ("openai",)),
        AgentSpec("pi", "ctb.agents.pi:Pi", ("openai",), effort_kwarg=True),
    )
}


#: The agents of a run or seal without --agents.
DEFAULT_AGENTS = ",".join(
    name for name, spec in AGENTS.items() if not (spec.experiment or spec.paused)
)


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

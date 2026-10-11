"""Vis with and without Council subagents."""

import json
import shlex
from pathlib import Path

from harbor.models.agent.context import AgentContext

from ctb import credentials
from ctb.agents.base import ARTIFACTS, EFFORT, CuratedAgent, min_token_seconds

PROVIDERS = {"anthropic": "anthropic-coding-plan", "openai": "openai-codex"}
VIS = "/installed-agent/vis-agent"
TRACE = "/logs/agent/vis-trace.jsonl"


def result_frame(path: Path) -> dict | None:
    """Read the last `result` frame of a full-trace JSON stream."""
    if not path.is_file():
        return None
    result = None
    for line in path.read_text(errors="replace").splitlines():
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        if frame.get("event") == "result" and isinstance(frame.get("payload"), dict):
            result = frame["payload"]
    return result


class VisSolo(CuratedAgent):
    """One Vis agent: Council and subagents off."""

    ARCHIVE = ARTIFACTS / "vis-agent-linux-amd64.tar.gz"
    TEAM = False

    @staticmethod
    def name() -> str:
        return "vis-solo"

    def get_version_command(self) -> str:
        return f"{VIS} --version"

    def secret_files(self) -> dict[str, str]:
        if self.family == "anthropic":
            access = credentials.anthropic(min_token_seconds())
            return {".vis/anthropic-auth.json": credentials.vis_anthropic_file(access)}
        access = credentials.codex_vis(min_token_seconds())
        return {".vis/openai-codex-auth.json": credentials.vis_codex_file(access)}

    def command(self, instruction: str) -> str:
        provider = PROVIDERS[self.family]
        config = "\n".join(
            [
                "providers:",
                f"  - id: {provider}",
                f"default_provider: {provider}",
                f"default_model: {self.model}",
                "",
            ]
        )
        mode = (
            "--persist --toggles council=true,draft_backend=off"
            if self.TEAM
            else "--db :memory --toggles council=false,draft_backend=off"
        )
        return (
            f"mkdir -p $HOME/.vis && printf %s {shlex.quote(config)} > $HOME/.vis/config.yml && "
            f"export VIS_HOME=$HOME/.vis && "
            f"{VIS} {mode} --model {provider}/{self.model} --reasoning-effort {EFFORT} "
            f"--full-trace-json-stream -- {shlex.quote(instruction)} "
            f"> {TRACE} 2> /logs/agent/vis-stderr.log"
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        result = result_frame(self.logs_dir / "vis-trace.jsonl")
        if result is None:
            return
        tokens = result.get("tokens") or {}
        context.n_input_tokens = tokens.get("input")
        context.n_cache_tokens = tokens.get("cached")
        context.n_output_tokens = tokens.get("output")
        context.metadata = {"vis": {"status": result.get("status"), "team": self.TEAM}}


class VisTeam(VisSolo):
    """Vis with Council on: the leader can run subagents with council.run."""

    TEAM = True

    @staticmethod
    def name() -> str:
        return "vis-team"

"""Dirge (github.com/yogthos/dirge) in one-shot print mode."""

import json
import shlex

from harbor.models.agent.context import AgentContext

from ctb import credentials
from ctb.agents.base import (
    EFFORT,
    REASONING_SUMMARY,
    VERBOSITY,
    CuratedAgent,
    min_token_seconds,
)
from ctb.paths import ARTIFACTS

DIRGE = "/installed-agent/dirge"
STREAM = "dirge.jsonl"
ARCHIVE_NAME = "dirge-45377633-usage-v3-x86_64-unknown-linux-musl.tar.gz"


class Dirge(CuratedAgent):
    ARCHIVE = ARTIFACTS / ARCHIVE_NAME

    @staticmethod
    def name() -> str:
        return "dirge"

    def get_version_command(self) -> str:
        return f"{DIRGE} --version"

    def config(self) -> dict:
        return {
            "provider": self.family,
            "yolo": True,
            "providers": {self.family: {"auth": "chatgpt", "model": self.model, "effort": EFFORT}},
        }

    def secret_files(self) -> dict[str, str]:
        access = credentials.codex_vis(min_token_seconds())
        return {
            ".config/dirge/config.json": json.dumps(self.config()),
            ".codex/auth.json": credentials.codex_auth_json(access),
        }

    def command(self, instruction: str) -> str:
        return (
            "export DIRGE_DATA_DIR=$HOME/.local/share/dirge DIRGE_CONFIG_DIR=$HOME/.config/dirge "
            # patches/dirge-codex-settings.patch sends these GPT settings to Codex.
            f"DIRGE_CODEX_TEXT_VERBOSITY={VERBOSITY} DIRGE_CODEX_REASONING_SUMMARY={REASONING_SUMMARY}; "
            f"{DIRGE} --yolo --no-session --verbose --output-format stream-json "
            f"-p {shlex.quote(instruction)} > /logs/agent/{STREAM} 2> /logs/agent/dirge-stderr.log"
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        # patches/dirge-usage.patch adds the raw usage to the final result event.
        usage = None
        path = self.logs_dir / STREAM
        # JSON Lines end only at \n; splitlines() also splits at \x85 or \u2028 inside a string.
        for line in path.read_text().split("\n") if path.is_file() else []:
            if line.startswith("{"):
                event = json.loads(line)
                if event.get("type") == "result":
                    usage = event.get("usage")
        if not usage:
            return
        context.n_input_tokens = usage.get("input_tokens", 0)
        context.n_cache_tokens = usage.get("cached_input_tokens", 0)
        context.n_output_tokens = usage.get("output_tokens", 0)

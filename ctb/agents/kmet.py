"""kmet (github.com/kmetia/kmet-agent) in one-shot print mode."""

import shlex
from collections.abc import Mapping

from edn_format import Keyword, loads
from harbor.models.agent.context import AgentContext

from ctb import credentials
from ctb.agents.base import ARTIFACTS, EFFORT, CuratedAgent, min_token_seconds

KMET = "/installed-agent/kmet"
STREAM = "kmet.edn"
ARCHIVE_NAME = "kmet-4ab740ff-linux-amd64.tar.gz"


class Kmet(CuratedAgent):
    ARCHIVE = ARTIFACTS / ARCHIVE_NAME

    @staticmethod
    def name() -> str:
        return "kmet"

    def get_version_command(self) -> str:
        return f"{KMET} --version"

    def secret_files(self) -> dict[str, str]:
        if self.family != "openai":
            raise ValueError(
                "kmet has no Anthropic OAuth transport; run it with openai/gpt-6.1-sol"
            )
        access = credentials.codex_vis(min_token_seconds())
        return {".kmet/agent/auth.edn": credentials.kmet_auth_edn("openai-codex", access)}

    def command(self, instruction: str) -> str:
        model = f"openai-codex/{self.model}"
        # A native Babashka binary takes user.home from passwd, not from $HOME.
        return (
            "export KMET_CODING_AGENT_DIR=$HOME/.kmet/agent; "
            f"{KMET} --mode edn --session-dir /logs/agent/kmet-sessions "
            f"--model {model} --thinking {EFFORT} {shlex.quote(instruction)} "
            f"> /logs/agent/{STREAM} 2> /logs/agent/kmet-stderr.log"
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        path = self.logs_dir / STREAM
        if not path.is_file():
            return
        totals = {key: 0 for key in ("input", "output", "cache-read", "cache-write")}
        found = False
        for line in path.read_text().splitlines():
            event = loads(line)
            if not isinstance(event, Mapping) or event.get(Keyword("type")) != Keyword(
                "message-end"
            ):
                continue
            message = event.get(Keyword("message")) or {}
            if message.get(Keyword("role")) != Keyword("assistant"):
                continue
            usage = message.get(Keyword("usage"))
            if usage:
                found = True
                for key in totals:
                    totals[key] += usage.get(Keyword(key), 0) or 0
        if found:
            context.n_input_tokens = totals["input"] + totals["cache-read"] + totals["cache-write"]
            context.n_cache_tokens = totals["cache-read"]
            context.n_output_tokens = totals["output"]

"""kmet (github.com/kmetia/kmet-agent) in one-shot print mode."""

import shlex

from ctb import credentials
from ctb.agents.base import ARTIFACTS, EFFORT, CuratedAgent, min_token_seconds

KMET = "/installed-agent/kmet"


class Kmet(CuratedAgent):
    ARCHIVE = ARTIFACTS / "kmet-linux-amd64.tar.gz"

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
            f"{KMET} --print --model {model} --thinking {EFFORT} {shlex.quote(instruction)} "
            "> /logs/agent/kmet.log 2> /logs/agent/kmet-stderr.log"
        )

"""kmet (github.com/kmetia/kmet-agent) in one-shot print mode."""

import json
import shlex

from harbor.models.agent.context import AgentContext

from ctb import credentials
from ctb.agents.base import ARTIFACTS, EFFORT, CuratedAgent, min_token_seconds

KMET = "/installed-agent/kmet"
# patches/kmet-usage.patch writes the session token totals here.
USAGE = "kmet-usage.json"


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
            f"export KMET_CODING_AGENT_DIR=$HOME/.kmet/agent KMET_USAGE_FILE=/logs/agent/{USAGE}; "
            f"{KMET} --print --model {model} --thinking {EFFORT} {shlex.quote(instruction)} "
            "> /logs/agent/kmet.log 2> /logs/agent/kmet-stderr.log"
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        path = self.logs_dir / USAGE
        if not path.is_file():
            return
        usage = json.loads(path.read_text())
        # kmet's input excludes cache reads and writes; Harbor counts the whole prompt.
        context.n_input_tokens = usage["input"] + usage["cache_read"] + usage["cache_write"]
        context.n_cache_tokens = usage["cache_read"]
        context.n_output_tokens = usage["output"]

"""Wisp (github.com/FolderFile/wisp-cli) in one-shot JSON mode, through the host proxy."""

import json
import shlex

from harbor.models.agent.context import AgentContext

from ctb.agents.base import EFFORT, CuratedAgent, proxy_env
from ctb.paths import ARTIFACTS

REPO = "https://github.com/FolderFile/wisp-cli.git"
# The newest main commit (package version 0.3.0); the repository has no release tags.
COMMIT = "01b850ef05fd40038d6b27a15908b92566ed91c5"
VERSION = COMMIT[:8]
# The current Node LTS; Wisp needs Node 22 or later.
NODE_IMAGE = "docker.io/library/node:26.13.0-bookworm-slim"
ARCHIVE_NAME = f"wisp-{VERSION}-linux-amd64.tar.gz"
NODE = "LD_LIBRARY_PATH=/installed-agent/wisp/lib /installed-agent/wisp/node"
WISP = "/installed-agent/wisp/dist/index.js"
STREAM = "wisp.jsonl"


class Wisp(CuratedAgent):
    ARCHIVE = ARTIFACTS / ARCHIVE_NAME

    @staticmethod
    def name() -> str:
        return "wisp"

    def get_version_command(self) -> str:
        return f"printf %s {VERSION}"

    def settings(self) -> str:
        # Wisp knows only gpt-5 models; an unknown model gets no thinking levels.
        # The window and output limit are the ones that Samizdat uses for this model.
        model = {
            "provider": "openai",
            "id": self.model,
            "name": self.model,
            "contextWindow": 1_000_000,
            "maxOutput": 128_000,
            "thinking": ["off", "low", "medium", "high"],
            "images": True,
        }
        return json.dumps({"models": [model]})

    def secret_files(self) -> dict[str, str]:
        return {".wisp/settings.json": self.settings()}

    def secret_env(self) -> dict[str, str]:
        return {"OPENAI_API_KEY": proxy_env()[1]}

    def command(self, instruction: str) -> str:
        url, _ = proxy_env()
        return (
            f"export WISP_HOME=$HOME/.wisp WISP_SESSION_DIR=/logs/agent/wisp-sessions "
            f"OPENAI_BASE_URL={shlex.quote(url + '/v1')}; "
            f"{NODE} {WISP} --json --yes --model openai/{self.model} --thinking {EFFORT} "
            f"-- {shlex.quote(instruction)} < /dev/null "
            f"> /logs/agent/{STREAM} 2> /logs/agent/wisp-stderr.log"
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        path = self.logs_dir / STREAM
        if not path.is_file():
            return
        totals = {"input": 0, "cacheRead": 0, "output": 0}
        turns, error = 0, None
        for line in path.read_text().split("\n"):
            if not line.startswith("{"):
                continue
            event = json.loads(line)
            if event.get("type") == "assistant_message":
                turns += 1
                for key in totals:
                    totals[key] += event.get("usage", {}).get(key) or 0
            elif event.get("type") == "turn_end" and event.get("error"):
                error = str(event["error"])
        if turns:
            # Wisp counts uncached input apart from cache reads.
            context.n_input_tokens = totals["input"] + totals["cacheRead"]
            context.n_cache_tokens = totals["cacheRead"]
            context.n_output_tokens = totals["output"]
        context.metadata = {"wisp": {"requests": turns, "error": error}}

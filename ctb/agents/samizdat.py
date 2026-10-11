"""samizdat (github.com/yogthos/samizdat) through its run API, with one beam branch.

samizdat speaks only the OpenAI Chat Completions wire, so `ctb run` starts the model
proxy (ctb.proxy) on the host and gives the agent its URL and a random proxy key.
"""

import json
import os
import shlex

from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ctb.agents.base import ARTIFACTS, EFFORT, CuratedAgent

SAMIZDAT = "/installed-agent/samizdat"
RUN_LOG = "samizdat-run.json"
# Opus 5.5 refuses a forced tool_choice, so the proxy asks for the tool in the prompt.
FEATURES = {"anthropic": ":reasoning-effort", "openai": ":native-tool-choice :reasoning-effort"}


def proxy_env() -> tuple[str, str]:
    """The proxy URL as a container sees it, and its key; `ctb run` sets both."""
    url, key = os.environ.get("CTB_PROXY_URL"), os.environ.get("CTB_PROXY_KEY")
    if not url or not key:
        raise RuntimeError(
            "samizdat needs CTB_PROXY_URL and CTB_PROXY_KEY; start it with `ctb run`"
        )
    return url, key


class Samizdat(CuratedAgent):
    ARCHIVE = ARTIFACTS / "samizdat-linux-amd64.tar.gz"

    @staticmethod
    def name() -> str:
        return "samizdat"

    def get_version_command(self) -> str:
        return f"{SAMIZDAT}/jolt/jolt --version"

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        # jolt writes its caches under the home that the archive brings.
        await self.exec_as_root(environment, command=f"chmod -R a+rwX {SAMIZDAT}/home")

    def config(self) -> str:
        url, _ = proxy_env()
        return (
            "{:providers {:ctb {:type :openai\n"
            f'                    :base-url "{url}/v1"\n'
            '                    :api-key-env "CTB_PROXY_KEY"\n'
            f'                    :model "{self.model}"\n'
            f"                    :features #{{{FEATURES[self.family]}}}\n"
            f'                    :reasoning-effort "{EFFORT}"\n'
            "                    :context-window 200000\n"
            "                    :max-tokens 32000}}\n"
            " :roles {:default :ctb}\n"
            " :approval {:mode :yolo}}\n"
        )

    def secret_files(self) -> dict[str, str]:
        return {"samizdat.edn": self.config()}

    def secret_env(self) -> dict[str, str]:
        return {"CTB_PROXY_KEY": proxy_env()[1]}

    def command(self, instruction: str) -> str:
        # The task directory is the project root; samizdat keeps its database in $HOME.
        return (
            f"root=$PWD; S={SAMIZDAT}; "
            "for d in .jolt .m2 .cache; do ln -sfn $S/home/$d $HOME/$d; done; "
            f"printf %s {shlex.quote(instruction)} > $HOME/problem.txt; "
            "export SAMIZDAT_CONFIG_FILE=$HOME/samizdat.edn HARNESS_ROOT=$root "
            "HARNESS_DB=$HOME/samizdat.sqlite3 CTB_PROBLEM_FILE=$HOME/problem.txt PATH=$S/jolt:$PATH; "
            "cd $S/src && { jolt serve > /logs/agent/samizdat-server.log 2>&1 & server=$!; }; "
            "jolt -m ctb.drive > /logs/agent/samizdat-drive.log 2>&1; code=$?; "
            # samizdat seeds its own userspace into the project; the verifier must not see it.
            'kill $server 2>/dev/null; rm -rf "$root/.samizdat"; exit $code'
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        path = self.logs_dir / RUN_LOG
        if not path.is_file():
            return
        run = json.loads(path.read_text()).get("run") or {}
        usage = run.get("usage") or {}
        context.n_input_tokens = usage.get("prompt-tokens")
        context.n_cache_tokens = usage.get("cache-hit-tokens")
        context.n_output_tokens = usage.get("completion-tokens")
        context.metadata = {"samizdat": {"status": run.get("status"), "turns": usage.get("turns")}}

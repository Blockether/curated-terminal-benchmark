"""Samizdat built from a pinned commit, through its HTTP API, with full transcript export."""

import json
import os
import shlex
from pathlib import Path
from typing import ClassVar

from harbor.agents.installed.base import PackageSpec
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ctb.agents.base import EFFORT, CuratedAgent
from ctb.paths import ARTIFACTS

# The newest main commit; v0.1.0 is the only release and is 100 commits older.
COMMIT = "97727796fc245f918f10b0db52537562aacb80dc"
VERSION = COMMIT[:8]
# The jolt release that builds it; samizdat CI also builds with the latest jolt release.
JOLT_VERSION = "v0.8.23"
ARCHIVE_NAME = f"samizdat-{VERSION}-x86_64-linux.tar.gz"
SAMIZDAT = f"/installed-agent/samizdat-{VERSION}-x86_64-linux/samizdat"
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
    ARCHIVE = ARTIFACTS / ARCHIVE_NAME
    SYSTEM_PACKAGES: ClassVar[dict[str, PackageSpec]] = {
        **CuratedAgent.SYSTEM_PACKAGES,
        "samizdat_libraries": PackageSpec(
            commands=(),
            packages={
                "apt-get": ("libsqlite3-0", "libssl3"),
                "dnf": ("sqlite-libs", "openssl-libs"),
                "yum": ("sqlite-libs", "openssl-libs"),
                "apk": ("sqlite-libs", "libssl3"),
            },
            always_install=True,
        ),
    }

    @staticmethod
    def name() -> str:
        return "samizdat"

    def get_version_command(self) -> str:
        return f"printf %s {VERSION}"

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        await self.ensure_system_dependencies(environment, ("python3", "samizdat_libraries"))
        await environment.upload_file(
            Path(__file__).with_name("samizdat_drive.py"), "/installed-agent/samizdat_drive.py"
        )

    def config(self) -> str:
        url, _ = proxy_env()
        return (
            "{:providers {:ctb {:type :openai\n"
            f'                    :base-url "{url}/v1"\n'
            '                    :api-key-env "CTB_PROXY_KEY"\n'
            f'                    :model "{self.model}"\n'
            f"                    :features #{{{FEATURES[self.family]}}}\n"
            f'                    :reasoning-effort "{EFFORT}"\n'
            "                    :context-window 1000000\n"
            "                    :max-tokens 128000}}\n"
            " :roles {:default :ctb}\n"
            " :approval {:mode :yolo}}\n"
        )

    def secret_files(self) -> dict[str, str]:
        return {"samizdat.edn": self.config()}

    def secret_env(self) -> dict[str, str]:
        return {"CTB_PROXY_KEY": proxy_env()[1]}

    def command(self, instruction: str) -> str:
        return (
            "root=$PWD; "
            f"printf %s {shlex.quote(instruction)} > $HOME/problem.txt; "
            "export SAMIZDAT_CONFIG_FILE=$HOME/samizdat.edn HARNESS_ROOT=$root "
            "HARNESS_DB=/logs/agent/samizdat.sqlite3 CTB_PROBLEM_FILE=$HOME/problem.txt; "
            f"{SAMIZDAT} --headless > /logs/agent/samizdat-server.log 2>&1 & server=$!; "
            "python3 /installed-agent/samizdat_drive.py > /logs/agent/samizdat-drive.log 2>&1; code=$?; "
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

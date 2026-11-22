"""Samizdat built from a pinned commit, through its HTTP API, with full transcript export."""

import json
import shlex
from pathlib import Path
from typing import ClassVar

from harbor.agents.installed.base import PackageSpec
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ctb import arch
from ctb.agents.base import EFFORT, VERBOSITY, CuratedAgent, proxy_env
from ctb.paths import ARTIFACTS

# The newest main commit on 21 November 2026; release v0.3.3 is older.
COMMIT = "daf7a373e1f0164ea3c688c832a849de68620696"
VERSION = COMMIT[:8]
# The latest jolt release, which samizdat CI also builds with. Jolt has no arm64 Linux release,
# so `ctb fetch` builds this tag from source on arm64.
JOLT_VERSION = "v0.8.28"
ARCHIVE_NAME = f"samizdat-{VERSION}-{arch.GNU}-linux.tar.gz"
SAMIZDAT = f"/installed-agent/samizdat-{VERSION}-{arch.GNU}-linux/samizdat"
RUN_LOG = "samizdat-run.json"


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
        # The Responses preset of samizdat for GPT-5 and later sets the features and windows; it
        # asks for the reasoning summary itself. The proxy relays /v1/responses with the login.
        return (
            "{:providers {:ctb {:type :openai-responses\n"
            f'                    :base-url "{url}/v1"\n'
            '                    :api-key-env "CTB_PROXY_KEY"\n'
            f'                    :model "{self.model}"\n'
            f'                    :reasoning-effort "{EFFORT}"\n'
            f'                    :text-verbosity "{VERBOSITY}"}}}}\n'
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

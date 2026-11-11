"""Nodecode (github.com/nodecode-ai/nodecode) through one RPC turn, on amd64 Linux."""

import json
import shlex
from pathlib import Path
from typing import ClassVar

from harbor.agents.installed.base import PackageSpec
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ctb.agents.base import EFFORT, CuratedAgent, proxy_env
from ctb.paths import ARTIFACTS
from ctb.resources import files_footprint
from ctb.toolkit import TOOLKIT

#: v0.1.14, the release that nodecode.ai serves for Linux x86_64 on 11 November 2026.
COMMIT = "cef1be5a34836b4b1d735efb8d346a461d8c9fe3"
VERSION = "0.1.14"
LABEL = COMMIT[:9]
#: Nodecode publishes no Linux arm64 build, so this arm runs only on an amd64 target.
ARCH = "amd64"
URL = f"https://nodecode.ai/dl/nodecode-{LABEL}-x86_64-linux.tar.gz"
SHA256 = "2c49979cc9b70ac49258477f8c0f7b6188df046e3c273883e687d19d7890762b"
ARCHIVE_NAME = f"nodecode-{LABEL}-x86_64-linux.tar.gz"
DIR = f"/installed-agent/nodecode-{LABEL}-x86_64-linux"
NODECODE = f"{DIR}/nodecode"
DRIVER = "/installed-agent/nodecode_drive.py"
FRAMES = "nodecode-rpc.jsonl"
#: An empty catalog keeps Nodecode from fetching models.dev; the declared model needs no entry.
CATALOG = ".nodecode/models.json"
#: Debian 12 and Ubuntu 22.04 ship libssl3; Ubuntu 24.04 ships libssl3t64.
APT_LIBSSL = (
    "if command -v apt-get >/dev/null 2>&1 && "
    "! ldconfig -p 2>/dev/null | grep -q libssl.so.3; then "
    "apt-get install -y libssl3 || apt-get install -y libssl3t64; fi"
)


class Nodecode(CuratedAgent):
    ARCHIVE = ARTIFACTS / ARCHIVE_NAME
    FOOTPRINT = files_footprint(package=(DIR,))
    SYSTEM_PACKAGES: ClassVar[dict[str, PackageSpec]] = {
        **CuratedAgent.SYSTEM_PACKAGES,
        "nodecode_libraries": PackageSpec(
            commands=(),
            packages={
                "apt-get": ("libsqlite3-0", "git"),
                "dnf": ("sqlite-libs", "openssl-libs", "git"),
                "yum": ("sqlite-libs", "openssl-libs", "git"),
            },
            always_install=True,
        ),
    }

    @staticmethod
    def name() -> str:
        return "nodecode"

    def get_version_command(self) -> str:
        return f"printf %s {VERSION}+{LABEL}"

    async def install(self, environment: BaseEnvironment) -> None:
        libc = await self.exec_as_root(
            environment, command="ldd --version 2>&1 | head -n 1 || true"
        )
        if "musl" in (libc.stdout or "").lower():
            raise RuntimeError("Nodecode ships for glibc x86_64 Linux; this image uses musl")
        await super().install(environment)
        await self.ensure_system_dependencies(environment, ("nodecode_libraries",))
        await self.exec_as_root(environment, command=APT_LIBSSL)
        await environment.upload_file(Path(__file__).with_name("nodecode_drive.py"), DRIVER)

    def config(self) -> str:
        """The RPC turn's provider, model and effort; the key arrives in NODECODE_API_KEY."""
        url, _ = proxy_env()
        return (
            json.dumps(
                {
                    "general": {
                        "default_model": {
                            "provider": "ctb",
                            "model": self.model,
                            "effort": EFFORT,
                        }
                    },
                    "providers": {
                        "ctb": {
                            "sdk": "openai-responses",
                            "base_url": f"{url}/v1",
                            "models": [
                                {
                                    "id": self.model,
                                    "context_window": 1000000,
                                    "reasoning_options": [
                                        {
                                            "type": "effort",
                                            "values": ["low", "medium", "high", "xhigh", "max"],
                                        }
                                    ],
                                }
                            ],
                            "reasoning_summary": "auto",
                        }
                    },
                    "update": {"mode": "off"},
                    "welcome": {"note": False},
                    "workspace": {"captures": False},
                    "request_bodies": {"keep_mb": 0},
                },
                indent=2,
            )
            + "\n"
        )

    def secret_files(self) -> dict[str, str]:
        return {".nodecode/config.jsonc": self.config(), CATALOG: "{}\n"}

    def secret_env(self) -> dict[str, str]:
        return {"NODECODE_API_KEY": proxy_env()[1]}

    def command(self, instruction: str) -> str:
        return (
            "export NODECODE_HOME=$HOME/.nodecode "
            "NODECODE_MODELS_CATALOG=$HOME/.nodecode/models.json NODECODE_PROVIDER=ctb "
            f"NODECODE_MODEL={shlex.quote(self.model)} NODECODE_REASONING_EFFORT={EFFORT} "
            "NODECODE_QA=0; "
            f"printf %s {shlex.quote(instruction)} > $HOME/problem.txt; "
            "export CTB_PROBLEM_FILE=$HOME/problem.txt; "
            f"{TOOLKIT}/bin/python3 {DRIVER} {NODECODE} "
            "> /logs/agent/nodecode-driver.log 2>&1"
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        totals = {"input": 0, "cache": 0, "output": 0}
        turns = 0
        path = self.logs_dir / FRAMES
        # JSON Lines end only at \n; splitlines() also splits at \x85 or \u2028 inside a string.
        for line in path.read_text().split("\n") if path.is_file() else []:
            try:
                event = json.loads(line)["payload"]["frame"]["event"]
            except (ValueError, KeyError, TypeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "turn.usage":
                continue
            usage = event.get("payload")
            if not isinstance(usage, dict):
                continue
            cached = usage.get("cached-input-tokens") or 0
            # Nodecode reports uncached input and cache reads apart; Harbor's input includes both.
            totals["input"] += (usage.get("input-tokens") or 0) + cached
            totals["cache"] += cached
            totals["output"] += usage.get("output-tokens") or 0
            turns += 1
        if turns:
            context.n_input_tokens = totals["input"]
            context.n_cache_tokens = totals["cache"]
            context.n_output_tokens = totals["output"]
            context.metadata = {"nodecode": {"turns": turns}}

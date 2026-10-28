"""Shared install, credential and cleanup steps for the curated agents."""

import os
import shlex
import tempfile
from pathlib import Path
from typing import ClassVar

from harbor.agents.installed.base import BaseInstalledAgent, PackageSpec, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ctb.toolkit import Toolkit

EFFORT = "medium"
# GPT Responses settings of every agent: pi's defaults, and the Codex catalog verbosity.
VERBOSITY = "low"
REASONING_SUMMARY = "auto"
AGENT_HOME = "/tmp/ctb-home"
FAMILIES = {"anthropic": "claude-opus-5-5", "openai": "gpt-6.1-sol"}


def min_token_seconds() -> int:
    """A token must outlive the agent timeout; CTB_MIN_TOKEN_S sets it."""
    return int(os.environ.get("CTB_MIN_TOKEN_S", "7500"))


def split_model(model_name: str | None) -> tuple[str, str]:
    """Accept only the two models of this benchmark: `anthropic/…` or `openai/…`."""
    family, _, model = (model_name or "").partition("/")
    if FAMILIES.get(family) != model:
        allowed = ", ".join(f"{f}/{m}" for f, m in FAMILIES.items())
        raise ValueError(f"Use --model {allowed}; got {model_name!r}")
    return family, model


async def stage_files(
    agent: BaseInstalledAgent, environment: BaseEnvironment, files: dict[str, str], root: str
) -> None:
    """Copy secret files to paths under root, readable only by the agent user."""
    for index, (target, text) in enumerate(files.items()):
        upload = f"/tmp/ctb-upload-{index}"
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write(text)
            local = Path(handle.name)
        try:
            os.chmod(local, 0o600)
            await environment.upload_file(local, upload)
        finally:
            local.unlink(missing_ok=True)
        await agent.exec_as_root(environment, command=f"chmod 644 {upload}")
        path = shlex.quote(f"{root}/{target}")
        await agent.exec_as_agent(
            environment,
            command=f"umask 077 && mkdir -p $(dirname {path}) && cp {upload} {path}",
        )
        await agent.exec_as_root(environment, command=f"rm -f {upload}")


async def agent_home(agent: BaseInstalledAgent, environment: BaseEnvironment) -> str:
    """The home directory of the agent user in the task container."""
    result = await agent.exec_as_agent(environment, command='printf %s "$HOME"')
    home = (result.stdout or "").strip()
    if not home.startswith("/"):
        raise RuntimeError(f"No home directory for the agent user: {home!r}")
    return home


class CuratedAgent(Toolkit, BaseInstalledAgent):
    """An installed agent that gets files and secrets only for one run, and the Python toolkit."""

    SYSTEM_PACKAGES: ClassVar[dict[str, PackageSpec]] = {
        **BaseInstalledAgent.SYSTEM_PACKAGES,
        "ca_certificates": PackageSpec(
            commands=(),
            packages={
                manager: ("ca-certificates",) for manager in ("apt-get", "dnf", "yum", "apk")
            },
            always_install=True,
        ),
    }
    #: Local archive to upload and unpack into /installed-agent.
    ARCHIVE: Path

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.family, self.model = split_model(self.model_name)

    async def install(self, environment: BaseEnvironment) -> None:
        if not self.ARCHIVE.is_file():
            raise FileNotFoundError(
                f"Missing agent archive: {self.ARCHIVE}; run `uv run ctb fetch`."
            )
        await self.ensure_system_dependencies(environment, ("ca_certificates",))
        await environment.upload_file(self.ARCHIVE, "/tmp/ctb-agent.tar.gz")
        await self.exec_as_root(
            environment,
            command="mkdir -p /installed-agent && tar -xzf /tmp/ctb-agent.tar.gz -C /installed-agent"
            " && chmod -R a+rX /installed-agent && rm /tmp/ctb-agent.tar.gz",
        )

    async def stage(self, environment: BaseEnvironment, files: dict[str, str]) -> None:
        """Copy secret files to paths under AGENT_HOME, readable only by the agent user."""
        await stage_files(self, environment, files, AGENT_HOME)

    async def clean(self, environment: BaseEnvironment) -> None:
        """Remove every staged token before the verifier runs."""
        await self.exec_as_root(environment, command=f"rm -rf {AGENT_HOME}")

    def secret_files(self) -> dict[str, str]:
        return {}

    def secret_env(self) -> dict[str, str]:
        return {}

    def command(self, instruction: str) -> str:
        raise NotImplementedError

    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        await self.exec_as_agent(environment, command=f"mkdir -p {AGENT_HOME} /logs/agent")
        try:
            await self.stage(environment, self.secret_files())
            # Tokens stay in the process environment, never in a command argument.
            await self.exec_as_agent(
                environment,
                command=f"export HOME={AGENT_HOME}; " + self.command(instruction),
                env=self.secret_env(),
            )
        finally:
            await self.clean(environment)

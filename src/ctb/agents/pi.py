"""pi through Harbor, with pinned subscription authentication plugins."""

import dataclasses
import json

from harbor.agents.installed.pi import Pi as HarborPi
from harbor.agents.model_connection import ResolvedModelConnection
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ctb import credentials
from ctb.agents.base import EFFORT, agent_home, min_token_seconds, split_model, stage_files
from ctb.toolkit import Toolkit

PI_VERSION = "1.3.0"
ANTHROPIC_PLUGIN = "@gotgenes/pi-anthropic-auth@3.4.3"
AUTH = ".pi/agent/auth.json"


def pi_auth_json(access: credentials.Access, family: str = "openai") -> str:
    """Create a subscription login without permission to rotate the host token."""
    entry = {"type": "oauth", "access": access.token, "refresh": "", "expires": access.expires_ms}
    if access.account_id:
        entry["accountId"] = access.account_id
    return json.dumps({"openai-codex" if family == "openai" else "anthropic": entry})


class Pi(Toolkit, HarborPi):
    """Run both benchmark models with medium effort, pinned OAuth support and the toolkit."""

    def __init__(self, *args, version: str | None = None, **kwargs) -> None:
        kwargs.setdefault("reasoning_effort", EFFORT)
        super().__init__(*args, version=version or PI_VERSION, **kwargs)
        split_model(self.model_name)

    @property
    def model_connection(self) -> ResolvedModelConnection:
        connection = super().model_connection
        family, _ = split_model(self.model_name)
        return dataclasses.replace(
            connection, provider="openai-codex" if family == "openai" else "anthropic"
        )

    def build_cli_flags(self) -> str:
        flags = super().build_cli_flags()
        if split_model(self.model_name)[0] == "anthropic":
            flags += f" --extension npm:{ANTHROPIC_PLUGIN}"
        return flags

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        home = await agent_home(self, environment)
        family, _ = split_model(self.model_name)
        access = (
            credentials.refresh_anthropic(min_token_seconds())
            if family == "anthropic"
            else credentials.codex_vis(min_token_seconds())
        )
        await stage_files(self, environment, {AUTH: pi_auth_json(access, family)}, home)
        try:
            await super().run(instruction, environment, context)
        finally:
            # Remove the token before the verifier runs.
            await self.exec_as_root(environment, command=f"rm -f {home}/{AUTH}")

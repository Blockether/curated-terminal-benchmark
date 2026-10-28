"""pi through Harbor, with the ChatGPT subscription login."""

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
AUTH = ".pi/agent/auth.json"


def pi_auth_json(access: credentials.Access) -> str:
    """Create a subscription login without permission to rotate the host token."""
    entry = {"type": "oauth", "access": access.token, "refresh": "", "expires": access.expires_ms}
    if access.account_id:
        entry["accountId"] = access.account_id
    return json.dumps({"openai-codex": entry})


class Pi(Toolkit, HarborPi):
    """Run the benchmark model with medium effort, the subscription login and the toolkit."""

    def __init__(self, *args, version: str | None = None, **kwargs) -> None:
        kwargs.setdefault("reasoning_effort", EFFORT)
        super().__init__(*args, version=version or PI_VERSION, **kwargs)
        split_model(self.model_name)

    @property
    def model_connection(self) -> ResolvedModelConnection:
        return dataclasses.replace(super().model_connection, provider="openai-codex")

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        home = await agent_home(self, environment)
        access = credentials.codex_vis(min_token_seconds())
        await stage_files(self, environment, {AUTH: pi_auth_json(access)}, home)
        try:
            await super().run(instruction, environment, context)
        finally:
            # Remove the token before the verifier runs.
            await self.exec_as_root(environment, command=f"rm -f {home}/{AUTH}")

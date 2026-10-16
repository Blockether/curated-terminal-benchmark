"""pi (pi.dev) through Harbor's built-in Pi agent, on the ChatGPT subscription."""

import dataclasses
import json

from harbor.agents.installed.pi import Pi as HarborPi
from harbor.agents.model_connection import ResolvedModelConnection
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ctb import credentials
from ctb.agents.base import agent_home, min_token_seconds, split_model, stage_files

PI_VERSION = "1.3.0"
AUTH = ".pi/agent/auth.json"


def pi_auth_json(access: credentials.Access) -> str:
    """A pi `auth.json` with one ChatGPT OAuth entry and an empty refresh token."""
    entry = {"type": "oauth", "access": access.token, "refresh": "", "expires": access.expires_ms}
    if access.account_id:
        entry["accountId"] = access.account_id
    return json.dumps({"openai-codex": entry})


class Pi(HarborPi):
    """Harbor's Pi on the `openai-codex` provider, with a pinned pi version.

    Anthropic bills a subscription token from pi as extra usage, so pi runs only on GPT.
    The effort comes from `--ak reasoning_effort=medium`, which Harbor maps to `--thinking`.
    """

    def __init__(self, *args, version: str | None = None, **kwargs) -> None:
        super().__init__(*args, version=version or PI_VERSION, **kwargs)
        family, _ = split_model(self.model_name)
        if family != "openai":
            raise ValueError(
                "pi runs only on openai/gpt-6.1-sol: Anthropic bills it as extra usage"
            )

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

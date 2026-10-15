"""OpenCode through Harbor's built-in OpenCode agent, on the ChatGPT subscription."""

import json

from harbor.agents.installed.opencode import OpenCode as HarborOpenCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ctb import credentials
from ctb.agents.base import EFFORT, min_token_seconds, split_model, stage_files

OPENCODE_VERSION = "1.19.0"


def opencode_auth_json(access: credentials.Access) -> str:
    """An OpenCode `auth.json` with one ChatGPT OAuth entry and an empty refresh token."""
    entry = {"type": "oauth", "access": access.token, "refresh": "", "expires": access.expires_ms}
    if access.account_id:
        entry["accountId"] = access.account_id
    return json.dumps({"openai": entry})


class OpenCode(HarborOpenCode):
    """Harbor's OpenCode with a pinned version and the `medium` model variant.

    Anthropic bills a subscription token from OpenCode as extra usage, so OpenCode runs only on GPT.
    """

    def __init__(self, *args, version: str | None = None, **kwargs) -> None:
        kwargs.setdefault("variant", EFFORT)
        super().__init__(*args, version=version or OPENCODE_VERSION, **kwargs)
        family, _ = split_model(self.model_name)
        if family != "openai":
            raise ValueError(
                "OpenCode runs only on openai/gpt-6.1-sol: Anthropic bills it as extra usage"
            )

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        # Harbor points XDG_DATA_HOME here, and OpenCode reads its logins from it.
        data = (self.environment_logs_dir / "opencode" / "xdg-data").as_posix()
        access = credentials.codex_vis(min_token_seconds())
        await stage_files(
            self, environment, {"opencode/auth.json": opencode_auth_json(access)}, data
        )
        try:
            await super().run(instruction, environment, context)
        finally:
            # Remove the token before the verifier runs and the logs leave the container.
            await self.exec_as_root(environment, command=f"rm -f {data}/opencode/auth.json")

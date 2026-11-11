"""pi through Harbor, with the ChatGPT subscription login."""

import dataclasses
import inspect
import json
import shlex

from harbor.agents.installed.pi import Pi as HarborPi
from harbor.agents.model_connection import ResolvedModelConnection
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ctb import credentials
from ctb.agents.base import EFFORT, agent_home, min_token_seconds, split_model, stage_files
from ctb.resources import npm_footprint
from ctb.toolkit import TOOLKIT, Toolkit

PI_VERSION = "1.5.2"
AUTH = ".pi/agent/auth.json"


def pi_auth_json(access: credentials.Access) -> str:
    """Create a subscription login without permission to rotate the host token."""
    entry = {"type": "oauth", "access": access.token, "refresh": "", "expires": access.expires_ms}
    if access.account_id:
        entry["accountId"] = access.account_id
    return json.dumps({"openai-codex": entry})


def validate_completion(text: str) -> str:
    """Pi JSON mode can exit zero after a provider error. Check its final assistant event."""
    reason = None
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        if (
            event.get("type") == "message_end"
            and isinstance(message, dict)
            and message.get("role") == "assistant"
        ):
            reason = message.get("stopReason")
    if reason != "stop":
        raise RuntimeError("Pi did not record a successful terminal assistant event")
    return reason


class Pi(Toolkit, HarborPi):
    """Run the benchmark model with medium effort, the subscription login and the toolkit."""

    FOOTPRINT = npm_footprint("@earendil-works/pi-coding-agent")
    #: Tool selection passed as `--tools`; empty keeps pi's defaults.
    TOOLS = ""

    def __init__(self, *args, version: str | None = None, **kwargs) -> None:
        kwargs.setdefault("reasoning_effort", EFFORT)
        super().__init__(*args, version=version or PI_VERSION, **kwargs)
        split_model(self.model_name)

    def build_cli_flags(self) -> str:
        flags = super().build_cli_flags()
        tools = f"--tools {self.TOOLS}" if self.TOOLS else ""
        return " ".join(part for part in (tools, flags) if part)

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
            script = (
                "import json\nfrom pathlib import Path\n"
                + inspect.getsource(validate_completion)
                + "\nvalidate_completion(Path('/logs/agent/pi.txt').read_text())\n"
            )
            await self.exec_as_agent(
                environment, command=f"{TOOLKIT}/bin/python3 -I -c {shlex.quote(script)}"
            )
        finally:
            # Remove the token before the verifier runs.
            await self.exec_as_root(environment, command=f"rm -f {home}/{AUTH}")


class PiBashOnly(Pi):
    """Pi with only the bash tool selected (`--tools bash`), everything else as Pi."""

    TOOLS = "bash"

    @staticmethod
    def name() -> str:
        return "pi-bash-only"

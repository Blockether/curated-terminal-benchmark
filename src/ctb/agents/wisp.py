"""Wisp (github.com/xificurC/wisp) on the toolkit Python, through the host proxy."""

import hashlib
import json
import shlex

from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ctb.agents.base import EFFORT, CuratedAgent, proxy_env
from ctb.paths import ARTIFACTS, ROOT
from ctb.resources import files_footprint
from ctb.toolkit import TOOLKIT

REPO = "https://github.com/xificurC/wisp.git"
# The newest main commit; the repository has no release tags.
COMMIT = "e0d468f792fb12ba2a31ab873c7f54190a92b753"
VERSION = COMMIT[:8]
# Upstream Wisp stops when the task image has no git; the patch treats a missing program as a
# failed command. It changes nothing else.
PATCHES = ("wisp-missing-git.patch",)
PATCH_SHA = hashlib.sha256(b"".join((ROOT / "patches" / p).read_bytes() for p in PATCHES))
ARCHIVE_NAME = f"wisp-{VERSION}-{PATCH_SHA.hexdigest()[:8]}.tar.gz"
HOME = "/installed-agent/wisp"
# Wisp is one Python 3 script without dependencies. Task images can have an old python3 or
# none, so it runs on the toolkit Python. Its bash and python3 tools use the task PATH.
WISP = f"{TOOLKIT}/bin/python3 {HOME}/wisp"
# Wisp starts each subagent with `wisp new` and `wisp run` in its bash tool, so a launcher
# for the same interpreter and script must be on PATH.
BIN = f"{HOME}/bin"
LAUNCHER = f'#!/bin/sh\nexec {WISP} "$@"\n'
SESSIONS = "/logs/agent/wisp-sessions"
STREAM = "wisp.stream"


class Wisp(CuratedAgent):
    ARCHIVE = ARTIFACTS / ARCHIVE_NAME
    # The interpreter is the toolkit Python that every agent gets, so it is not Wisp's runtime.
    FOOTPRINT = files_footprint(package=(HOME,))

    @staticmethod
    def name() -> str:
        return "wisp"

    def get_version_command(self) -> str:
        return f"printf %s {VERSION}"

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        await self.exec_as_root(
            environment,
            command=f"mkdir -p {BIN} && printf %s {shlex.quote(LAUNCHER)} > {BIN}/wisp"
            f" && chmod 755 {BIN}/wisp",
        )

    def secret_env(self) -> dict[str, str]:
        return {"WISP_LLM_API_KEY": proxy_env()[1]}

    def command(self, instruction: str) -> str:
        # The README setup: one session with the wisp block, AGENTS.md files and skills.
        url, _ = proxy_env()
        return (
            # Wisp speaks the Responses wire itself; the proxy adds only the login.
            f"export PATH={BIN}:$PATH WISP_LLM_URL={shlex.quote(url + '/v1/responses')} "
            f"WISP_LLM_API=openai-responses WISP_SESSIONS_ROOT={SESSIONS} && "
            f"{{ WISP_SESSION_DIR=$({WISP} new --name ctb -m {self.model} -t {EFFORT} "
            f"--env-in-system-prompt -s <({WISP} build-system-prompt --wisp --skills --agents-md "
            f"< /dev/null)) && export WISP_SESSION_DIR && "
            f"printf %s {shlex.quote(instruction)} | {WISP} run > /logs/agent/{STREAM}; "
            f"}} 2> /logs/agent/wisp-stderr.log"
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        totals = {"prompt": 0, "cached": 0, "completion": 0}
        turns = 0
        # Subagent sessions are siblings of the main session under WISP_SESSIONS_ROOT, so their
        # usage counts here too. A subagent session names its parent in wisp.run.json.
        sessions = self.logs_dir / "wisp-sessions"
        subagents = sum(
            1
            for path in sessions.glob("*/wisp.run.json")
            if json.loads(path.read_text()).get("parent")
        )
        for path in sorted(sessions.glob("*/wisp.usage")):
            for line in path.read_text().split("\n"):
                line = line.removeprefix("data: ")
                if not line.startswith("{"):
                    continue
                chunk = json.loads(line)
                usage = chunk["usage"] if isinstance(chunk.get("usage"), dict) else chunk
                turns += 1
                totals["prompt"] += usage.get("prompt_tokens") or 0
                totals["cached"] += (usage.get("prompt_tokens_details") or {}).get(
                    "cached_tokens"
                ) or 0
                totals["completion"] += usage.get("completion_tokens") or 0
        if turns:
            # prompt_tokens includes the cached tokens.
            context.n_input_tokens = totals["prompt"]
            context.n_cache_tokens = totals["cached"]
            context.n_output_tokens = totals["completion"]
        context.metadata = {"wisp": {"requests": turns, "subagents": subagents}}

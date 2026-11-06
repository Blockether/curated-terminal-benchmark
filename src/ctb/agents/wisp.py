"""wisp (github.com/xificurC/wisp) over the host proxy, one non-stream chat turn per round."""

import json
import os
import shlex

from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ctb.agents.base import EFFORT, CuratedAgent
from ctb.paths import ARTIFACTS
from ctb.toolkit import TOOLKIT

#: The main commit whose script wisp/wisp runs; a single file needs no build.
COMMIT = "db3dfdf200d5cb837435518ceeb7bbb5289e2106"
VERSION = COMMIT[:8]
ARCHIVE_NAME = f"wisp-{VERSION}.tar.gz"
WISP = "/installed-agent/wisp"
#: The PATH shim directory, so the agent and its persona find `wisp` as documented.
BIN = "/installed-agent/wisp-bin"
#: The pinned CPython of the toolkit runs the script, never the one of the task image.
SHIM = f'#!/bin/sh\nexec {TOOLKIT}/bin/python3 {WISP} "$@"\n'
SESSIONS_ROOT = "/logs/agent/wisp-sessions"
SESSION = "wisp"
STREAM = "wisp.stream"


def proxy_env() -> tuple[str, str]:
    """The proxy URL as a container sees it, and its key; `ctb run` sets both."""
    url, key = os.environ.get("CTB_PROXY_URL"), os.environ.get("CTB_PROXY_KEY")
    if not url or not key:
        raise RuntimeError("wisp needs CTB_PROXY_URL and CTB_PROXY_KEY; start it with `ctb run`")
    return url, key


def usage_totals(text: str) -> dict:
    """Sum the usage chunks of one wisp.usage file.

    wisp writes one JSON usage chunk per turn; lines written before the
    data:-prefix removal still parse. input counts cache reads, like every
    other agent of the benchmark, so the list price subtracts the cache.
    """
    totals = {"input": 0, "cached": 0, "output": 0, "turns": 0}
    for line in text.split("\n"):
        raw = line.removeprefix("data: ")
        try:
            chunk = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(chunk, dict):
            continue
        usage = chunk.get("usage") if isinstance(chunk.get("usage"), dict) else chunk
        details = usage.get("prompt_tokens_details") or {}
        totals["input"] += int(usage.get("prompt_tokens") or 0)
        totals["cached"] += int(details.get("cached_tokens") or 0)
        totals["output"] += int(usage.get("completion_tokens") or 0)
        totals["turns"] += 1
    return totals


class Wisp(CuratedAgent):
    ARCHIVE = ARTIFACTS / ARCHIVE_NAME

    @staticmethod
    def name() -> str:
        return "wisp"

    def get_version_command(self) -> str:
        return f"printf %s {VERSION}"

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        await self.exec_as_root(
            environment,
            command=f"mkdir -p {BIN} && printf %s {shlex.quote(SHIM)} > {BIN}/wisp"
            f" && chmod a+rx {BIN}/wisp",
        )

    def secret_env(self) -> dict[str, str]:
        """The proxy address and key: the container gets no subscription token."""
        url, key = proxy_env()
        return {"WISP_LLM_URL": url, "WISP_LLM_API_KEY": key}

    def command(self, instruction: str) -> str:
        # WISP_LLM_STREAM=false is the one non-stream chat body that ctb.proxy answers;
        # a percent context guard would need /v1/model/info, which the proxy does not serve.
        return (
            f'export PATH={BIN}:"$PATH" WISP_SESSIONS_ROOT={SESSIONS_ROOT} WISP_LLM_STREAM=false; '
            f"printf %s {shlex.quote(instruction)} > $HOME/problem.txt; "
            f"session=$(wisp new --name {SESSION} -m {self.model} -t {EFFORT} "
            "--env-in-system-prompt -s <(wisp build-system-prompt --wisp)) || exit $?; "
            f'wisp run -d "$session" < $HOME/problem.txt > /logs/agent/{STREAM} '
            "2> /logs/agent/wisp-stderr.log"
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        path = self.logs_dir / "wisp-sessions" / SESSION / "wisp.usage"
        if not path.is_file():
            return
        totals = usage_totals(path.read_text())
        if not totals["turns"]:
            return
        context.n_input_tokens = totals["input"]
        context.n_cache_tokens = totals["cached"]
        context.n_output_tokens = totals["output"]
        context.metadata = {"wisp": {"turns": totals["turns"]}}

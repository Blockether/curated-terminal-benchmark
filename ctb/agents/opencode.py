"""OpenCode through Harbor, with pinned subscription authentication plugins."""

import asyncio
import json
import os
import time
from pathlib import Path

from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.agents.installed.opencode import OpenCode as HarborOpenCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ctb import credentials
from ctb.agents.base import EFFORT, min_token_seconds, split_model, stage_files

OPENCODE_VERSION = "1.19.3"
ANTHROPIC_PLUGIN = "@ex-machina/opencode-anthropic-auth@1.8.6"
#: OpenCode keeps a SQLite database and a git snapshot store in XDG_DATA_HOME. Keep them off
#: /logs, a host mount of the Podman VM, which gets only OpenCode's log.
LOCAL_DATA = "/tmp/ctb-opencode"
#: Seconds after a final answer before ctb stops an OpenCode process that does not exit.
FINAL_GRACE_S = 120


def stop_command(signal: str) -> str:
    """Send signal to the OpenCode process, not to its shell; task images can lack pkill."""
    # The pattern is built from $m so that this shell does not match itself.
    return (
        "m=--model=; for p in /proc/[0-9]*; do "
        "c=$(tr '\\0' ' ' < $p/cmdline 2>/dev/null); "
        f'case "$c" in "opencode $m"*|*"/opencode $m"*) kill -{signal} ${{p#/proc/}} 2>/dev/null;; esac; '
        "done; true"
    )


def processes_command(path: str) -> str:
    """Write pid, state, kernel wait channel and command of each container process to path."""
    return (
        f"mkdir -p $(dirname {path}); for p in /proc/[0-9]*; do "
        "printf '%s %s %s %s\\n' ${p#/proc/} \"$(cut -d' ' -f3 $p/stat 2>/dev/null)\" "
        "\"$(cat $p/wchan 2>/dev/null)\" \"$(tr '\\0' ' ' < $p/cmdline 2>/dev/null | cut -c1-300)\"; "
        f"done > {path} 2>/dev/null; true"
    )


class OpenCodeHangError(NonZeroAgentExitCodeError):
    """OpenCode wrote no output for CTB_OPENCODE_IDLE_S seconds before a final answer.

    Harbor runs the verifier after this error type, as after an agent timeout.
    """


def stopped_error(stopped: list[str], error: BaseException | None) -> BaseException | None:
    """The error of a run after the watchdog: a hang fails, a stop after a final answer does not."""
    if stopped == ["hang"]:
        return OpenCodeHangError(
            f"OpenCode wrote no output for {idle_limit_seconds()} s before a final answer"
        )
    if stopped == ["final"]:
        return None
    return error


def idle_limit_seconds() -> int:
    """Silence after which ctb stops OpenCode; its bash tool stops a command after 10 minutes."""
    return int(os.environ.get("CTB_OPENCODE_IDLE_S", "1800"))


def has_final_answer(output: str) -> bool:
    """Whether OpenCode's JSON lines contain the model's final answer."""
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        part = event.get("part") if isinstance(event, dict) else None
        if isinstance(part, dict) and part.get("type") == "text":
            openai = (part.get("metadata") or {}).get("openai") or {}
            if openai.get("phase") == "final_answer":
                return True
    return False


def watchdog_verdict(output: str, idle_s: float, idle_limit_s: float) -> str | None:
    """`final` or `hang` when ctb must stop OpenCode, else None."""
    if idle_s >= FINAL_GRACE_S and has_final_answer(output):
        return "final"
    if idle_s >= idle_limit_s:
        return "hang"
    return None


def opencode_auth_json(access: credentials.Access, family: str = "openai") -> str:
    """Create a subscription login without permission to rotate the host token."""
    entry = {"type": "oauth", "access": access.token, "refresh": "", "expires": access.expires_ms}
    if access.account_id:
        entry["accountId"] = access.account_id
    return json.dumps({family: entry})


class OpenCode(HarborOpenCode):
    """Run both benchmark models with medium effort and pinned OAuth support."""

    def __init__(self, *args, version: str | None = None, **kwargs) -> None:
        kwargs.setdefault("variant", EFFORT)
        super().__init__(*args, version=version or OPENCODE_VERSION, **kwargs)
        family, _ = split_model(self.model_name)
        if family == "anthropic":
            self._opencode_config = {
                **self._opencode_config,
                "plugin": [*self._opencode_config.get("plugin", []), ANTHROPIC_PLUGIN],
                "provider": {
                    **self._opencode_config.get("provider", {}),
                    "anthropic": {
                        "models": {
                            "claude-opus-5-5": {"limit": {"context": 1000000, "output": 128000}}
                        }
                    },
                },
            }

    async def exec_as_agent(self, environment: BaseEnvironment, command: str, env=None, **kwargs):
        if env and "XDG_DATA_HOME" in env:
            env = {
                **env,
                "XDG_DATA_HOME": f"{LOCAL_DATA}/xdg-data",
                "XDG_STATE_HOME": f"{LOCAL_DATA}/xdg-state",
            }
        if split_model(self.model_name)[0] == "anthropic":
            env = {**(env or {}), "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX": "128000"}
        return await super().exec_as_agent(environment, command=command, env=env, **kwargs)

    async def _watch(self, environment: BaseEnvironment, stopped: list[str]) -> None:
        """Stop OpenCode after a final answer, or after a long silence, when it does not exit."""
        output = Path(self.logs_dir) / self._OUTPUT_FILENAME
        last_size, last_change = -1, time.monotonic()
        while True:
            await asyncio.sleep(15)
            size = output.stat().st_size if output.exists() else 0
            if size != last_size:
                last_size, last_change = size, time.monotonic()
                continue
            text = output.read_text(errors="replace") if size else ""
            verdict = watchdog_verdict(text, time.monotonic() - last_change, idle_limit_seconds())
            if verdict:
                stopped.append(verdict)
                await self.exec_as_root(
                    environment,
                    command=f"{processes_command(self._processes_path())}; "
                    f"{stop_command('TERM')}; sleep 10; {stop_command('KILL')}",
                )
                return

    def _processes_path(self) -> str:
        return (self.environment_logs_dir / "opencode" / "processes.txt").as_posix()

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        data = f"{LOCAL_DATA}/xdg-data"
        family, _ = split_model(self.model_name)
        access = (
            credentials.refresh_anthropic(min_token_seconds())
            if family == "anthropic"
            else credentials.codex_vis(min_token_seconds())
        )
        await stage_files(
            self, environment, {"opencode/auth.json": opencode_auth_json(access, family)}, data
        )
        stopped: list[str] = []
        error: BaseException | None = None
        watch = asyncio.create_task(self._watch(environment, stopped))
        try:
            await super().run(instruction, environment, context)
        except NonZeroAgentExitCodeError as exc:
            # Harbor runs OpenCode with pipefail, so a stopped OpenCode exits with 143.
            error = exc
        finally:
            watch.cancel()
            # Remove the token before the verifier runs, then keep OpenCode's own log.
            logs = (self.environment_logs_dir / "opencode").as_posix()
            await self.exec_as_root(
                environment,
                command=f"rm -f {data}/opencode/auth.json; mkdir -p {logs} && "
                f"cp -r {data}/opencode/log {logs}/ 2>/dev/null; true",
            )
        if (result := stopped_error(stopped, error)) is not None:
            raise result

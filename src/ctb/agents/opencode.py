"""OpenCode 2 through Harbor, with the ChatGPT subscription login."""

import asyncio
import json
import os
import shlex
import time
from pathlib import Path
from typing import override

from harbor.agents.installed.base import NonZeroAgentExitCodeError, with_prompt_template
from harbor.agents.installed.node_install import nvm_node_install_snippet
from harbor.agents.installed.opencode import OpenCode as HarborOpenCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ctb import credentials
from ctb.agents.base import EFFORT, VERBOSITY, min_token_seconds, split_model, stage_files
from ctb.toolkit import Toolkit

OPENCODE_VERSION = "2.2.0"
#: OpenCode 2 is published as @opencode/cli; the opencode-ai package stops at 1.x.
OPENCODE_PACKAGE = "@opencode/cli"
#: OpenCode keeps a SQLite database and a git snapshot store in XDG_DATA_HOME. Keep them off
#: /logs, a host mount of the Podman VM, which gets only OpenCode's log.
LOCAL_DATA = "/tmp/ctb-opencode"
#: Seconds after a final answer before ctb stops an OpenCode process that does not exit.
FINAL_GRACE_S = 120
#: The session export under the trial's opencode log directory; it has the usage of every step.
SESSION_FILE = "session.json"
#: ~/.nvm is absent on musl images, where Node comes from apk.
NVM = "[ -f ~/.nvm/nvm.sh ] && . ~/.nvm/nvm.sh; "


def stop_command(signal: str) -> str:
    """Send signal to OpenCode and its private server, not to a shell; task images can lack pkill."""
    # Only argv[0] is matched, so a shell whose command line names OpenCode is never hit.
    return (
        "for p in /proc/[0-9]*; do "
        "a=$(tr '\\0' '\\n' < $p/cmdline 2>/dev/null | head -n 1); "
        f'case "${{a##*/}}" in opencode|opencode.exe) kill -{signal} ${{p#/proc/}} 2>/dev/null;; esac; '
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


def json_lines(output: str) -> list[dict]:
    """The JSON objects of OpenCode's output; other lines are skipped."""
    events = []
    # JSON Lines end only at \n; splitlines() also splits at \x85 or \u2028 inside a string.
    for line in output.split("\n"):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def has_final_answer(output: str) -> bool:
    """Whether OpenCode's JSON lines contain the end of the model's final step."""
    # OpenCode 2 marks no final text part; a step that ends with `stop` calls no more tools.
    for event in json_lines(output):
        part = event.get("part")
        if (
            event.get("type") == "step_finish"
            and isinstance(part, dict)
            and part.get("reason") == "stop"
        ):
            return True
    return False


def session_id(output: str) -> str | None:
    """The OpenCode session of a run, from the first JSON line that names it."""
    for event in json_lines(output):
        if isinstance(event.get("sessionID"), str):
            return event["sessionID"]
    return None


def session_steps(path: Path) -> list[dict]:
    """The assistant messages of an OpenCode session export, one for each step."""
    try:
        exported = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    messages = exported.get("messages") if isinstance(exported, dict) else None
    return [m for m in messages or [] if isinstance(m, dict) and m.get("type") == "assistant"]


def session_usage(steps: list[dict]) -> tuple[int, int, int, float | None]:
    """Input with cache reads, cache reads, output and cost, the way Harbor counts OpenCode."""
    used = [step.get("tokens") or {} for step in steps]
    cached = sum((u.get("cache") or {}).get("read") or 0 for u in used)
    input_tokens = sum(u.get("input") or 0 for u in used) + cached
    output_tokens = sum(u.get("output") or 0 for u in used)
    cost = sum(step.get("cost") or 0 for step in steps)
    return input_tokens, cached, output_tokens, cost or None


def watchdog_verdict(output: str, idle_s: float, idle_limit_s: float) -> str | None:
    """`final` or `hang` when ctb must stop OpenCode, else None."""
    if idle_s >= FINAL_GRACE_S and has_final_answer(output):
        return "final"
    if idle_s >= idle_limit_s:
        return "hang"
    return None


def opencode_auth_json(access: credentials.Access) -> str:
    """Create a subscription login without permission to rotate the host token.

    OpenCode 2 keeps logins in its database, so ctb loads this list with `opencode auth import`.
    """
    value = {
        "type": "oauth",
        "methodID": "chatgpt-browser",
        "refresh": "",
        "access": access.token,
        "expires": access.expires_ms,
    }
    if access.account_id:
        value["metadata"] = {"accountID": access.account_id}
    credential = {
        "id": "cred_ctb",
        "integrationID": "openai",
        "label": "OAuth",
        "active": True,
        "value": value,
    }
    return json.dumps([credential])


class OpenCode(Toolkit, HarborOpenCode):
    """Run the benchmark model with medium effort, the subscription login and the toolkit."""

    def __init__(self, *args, version: str | None = None, **kwargs) -> None:
        kwargs.setdefault("variant", EFFORT)
        super().__init__(*args, version=version or OPENCODE_VERSION, **kwargs)
        _, model = split_model(self.model_name)
        # OpenCode sets GPT verbosity only for gpt-5.x ids; send pi's `low` for every GPT.
        self._opencode_config = {
            **self._opencode_config,
            "provider": {
                **self._opencode_config.get("provider", {}),
                "openai": {"models": {model: {"options": {"textVerbosity": VERBOSITY}}}},
            },
        }

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        # Harbor's install runs `npm i -g opencode-ai`, which has no 2.x release.
        await self.ensure_system_dependencies(
            environment, ("curl", "bash", "coreutils", "nodejs", "npm")
        )
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                "if ldd --version 2>&1 | grep -qi musl || [ -f /etc/alpine-release ]; then "
                "node --version && npm --version; "
                f"else {nvm_node_install_snippet()}; fi && "
                f"npm i -g {OPENCODE_PACKAGE}@{self._version or OPENCODE_VERSION} && "
                "opencode --version"
            ),
        )

    def opencode_env(self) -> dict[str, str]:
        """The environment of every OpenCode command, with its data off the host-mounted logs."""
        return {
            **self.model_connection.env,
            "OPENCODE_FAKE_VCS": "git",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "XDG_DATA_HOME": f"{LOCAL_DATA}/xdg-data",
            "XDG_STATE_HOME": f"{LOCAL_DATA}/xdg-state",
        }

    def run_command(self, instruction: str) -> str:
        """Run OpenCode once; the instruction comes on stdin, where OpenCode 2 keeps it verbatim."""
        # OpenCode 2.2.0 reads the words after `--` twice and quotes each one that has a space.
        variant = self.options.variant
        model = f"{self.model_name}#{variant}" if variant else self.model_name
        resume = "--continue " if self._resume else ""
        output = (self.environment_logs_dir / self._OUTPUT_FILENAME).as_posix()
        return (
            f"{NVM}printf '%s' {shlex.quote(instruction)} | "
            f"opencode run --standalone --model={shlex.quote(model)} --format=json {resume}"
            f"--thinking --auto 2>&1 | stdbuf -oL tee {shlex.quote(output)}"
        )

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

    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        # Harbor's run uses OpenCode 1 flags, so ctb runs OpenCode 2 itself.
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("Model name must be in the format provider/model_name")
        self._instruction = instruction
        access = credentials.codex_vis(min_token_seconds())
        env = self.opencode_env()
        login = f"{LOCAL_DATA}/credentials.json"
        await stage_files(
            self, environment, {"credentials.json": opencode_auth_json(access)}, LOCAL_DATA
        )
        stopped: list[str] = []
        error: BaseException | None = None
        watch: asyncio.Task | None = None
        try:
            await self.exec_as_agent(
                environment,
                command=f"{NVM}opencode --standalone auth import {login}; s=$?; rm -f {login}; exit $s",
                env=env,
            )
            for command in (
                self._build_register_skills_command(),
                self._build_register_config_command(),
            ):
                if command:
                    await self.exec_as_agent(environment, command=command, env=env)
            watch = asyncio.create_task(self._watch(environment, stopped))
            await self.exec_as_agent(environment, command=self.run_command(instruction), env=env)
            if messages := self._error_messages():
                raise NonZeroAgentExitCodeError(
                    "OpenCode emitted error event(s): " + "; ".join(messages[:3])
                )
        except NonZeroAgentExitCodeError as exc:
            # ctb runs OpenCode with pipefail, so a stopped OpenCode exits with 143.
            error = exc
        finally:
            if watch:
                watch.cancel()
            await self._collect(environment, env)
        if (result := stopped_error(stopped, error)) is not None:
            raise result

    async def _collect(self, environment: BaseEnvironment, env: dict[str, str]) -> None:
        """Keep the session export and OpenCode's log, then delete the database with the login."""
        data = f"{LOCAL_DATA}/xdg-data/opencode"
        logs = (self.environment_logs_dir / "opencode").as_posix()
        output = Path(self.logs_dir) / self._OUTPUT_FILENAME
        sid = session_id(output.read_text(errors="replace")) if output.is_file() else None
        try:
            if sid:
                await self.exec_as_agent(
                    environment,
                    command=f"{NVM}opencode --standalone session export {shlex.quote(sid)} "
                    f"> {LOCAL_DATA}/{SESSION_FILE} 2>/dev/null; true",
                    env=env,
                )
        finally:
            # The verifier must not find the login.
            await self.exec_as_root(
                environment,
                command=f"mkdir -p {logs}; cp {LOCAL_DATA}/{SESSION_FILE} {logs}/ 2>/dev/null; "
                f"cp -r {data}/log {logs}/ 2>/dev/null; rm -f {data}/opencode.db*; true",
            )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        """Take the run totals from the session export: OpenCode 2 omits the last step_finish."""
        super().populate_context_post_run(context)
        steps = session_steps(Path(self.logs_dir) / "opencode" / SESSION_FILE)
        if steps:
            input_tokens, cached, output_tokens, cost = session_usage(steps)
            context.n_input_tokens = input_tokens
            context.n_cache_tokens = cached
            context.n_output_tokens = output_tokens
            context.cost_usd = cost

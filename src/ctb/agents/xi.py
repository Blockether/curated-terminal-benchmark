"""Xi (xi-agent.com) in one-shot prompt mode."""

import hashlib
import json
import shlex

from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ctb import credentials
from ctb.agents.base import (
    EFFORT,
    REASONING_SUMMARY,
    VERBOSITY,
    CuratedAgent,
    min_token_seconds,
)
from ctb.paths import ARTIFACTS, ROOT
from ctb.resources import files_footprint

XI = "/installed-agent/xi"
BUN = f"{XI}/bin/bun"
MAIN = f"{XI}/app/target/main.js"
VERSION = "0.6.0"
USAGE = "xi-usage.jsonl"
BUN_VERSION = "1.4.4"
PACKAGE_URL = f"https://registry.npmjs.org/xi-agent/-/xi-agent-{VERSION}.tgz"
PACKAGE_SHA256 = "79a482850530a866dad1442a37323ae854089326be7e87b9b83a3c01f4f7f374"
# Xi sends no reasoning effort to its Codex provider, so GPT gets the provider default "low",
# and the provider fixes the text verbosity at "medium". The first patch lets the environment
# set the effort, the verbosity and the reasoning summary. Xi keeps the usage only in memory;
# the second patch appends the usage of each turn to a file. The base prompt of Xi says that
# Python is not available, which is false in the task images; the third patch removes only
# that sentence. The patches change nothing else.
PATCHES = ("xi-codex-settings.patch", "xi-codex-usage.patch", "xi-python-claim.patch")
PATCH_SHA = hashlib.sha256(b"".join((ROOT / "patches" / p).read_bytes() for p in PATCHES))
ARCHIVE_NAME = f"xi-{VERSION}-{PATCH_SHA.hexdigest()[:8]}.tar.gz"
BUN_ASSETS = {
    ("aarch64", "gnu"): (
        "bun-linux-aarch64.zip",
        "baa6bb36f51d6e58561e02bbea825038a9df454f673d6d776e8bbf86f8c7850e",
    ),
    ("aarch64", "musl"): (
        "bun-linux-aarch64-musl.zip",
        "57f01b02ac7113c61c935f28f38c2eab849e3c970dab21f8b640a87b1ef65009",
    ),
    ("x64", "gnu"): (
        "bun-linux-x64.zip",
        "bf8ccbfb2b0b80159e0f9a6d15c55317add8077534dbc6130a3edb52fccb395c",
    ),
    ("x64", "musl"): (
        "bun-linux-x64-musl.zip",
        "a5a8ccc5e74dcb883fe974f125a7bdb3b41e5195ed1b870e73f17effd3eb2b2d",
    ),
}


def bun_url(asset: str) -> str:
    return f"https://github.com/oven-sh/bun/releases/download/bun-v{BUN_VERSION}/{asset}"


def install_command() -> str:
    """Install the pinned Bun runtime next to the patched Xi package of the archive."""
    assets = " ".join(
        f'"{machine}:{libc}") url={bun_url(asset)}; sha256={digest};;'
        for (machine, libc), (asset, digest) in BUN_ASSETS.items()
    )
    launcher = f'#!/bin/sh\nexec {BUN} {MAIN} "$@"\n'
    return (
        "set -eu; "
        f"rm -rf {XI}/bin /tmp/xi-bun; mkdir -p {XI}/bin; "
        'case "$(uname -m)" in '
        "x86_64|amd64) machine=x64;; "
        "aarch64|arm64) machine=aarch64;; "
        "*) echo 'xi: unsupported CPU' >&2; exit 1;; "
        "esac; "
        "if ldd --version 2>&1 | grep -qi musl || [ -f /etc/alpine-release ]; "
        "then libc=musl; else libc=gnu; fi; "
        f'case "$machine:$libc" in {assets} '
        "*) echo 'xi: no Bun build for this system' >&2; exit 1;; "
        "esac; "
        'curl -fsSL --retry 3 -o /tmp/xi-bun.zip "$url"; '
        "printf '%s  %s\\n' \"$sha256\" /tmp/xi-bun.zip | sha256sum -c -; "
        "unzip -q /tmp/xi-bun.zip -d /tmp/xi-bun; "
        f"mv /tmp/xi-bun/bun-linux-*/bun {BUN}; "
        f"printf %s {shlex.quote(launcher)} > {XI}/bin/xi; "
        f"chmod 755 {BUN} {XI}/bin/xi; "
        f"chmod -R a+rX {XI}; "
        f"rm -rf /tmp/xi-bun.zip /tmp/xi-bun; "
        f"{BUN} --version && {XI}/bin/xi help > /dev/null"
    )


class Xi(CuratedAgent):
    """Run the benchmark model with the ChatGPT subscription login and the toolkit."""

    ARCHIVE = ARTIFACTS / ARCHIVE_NAME
    FOOTPRINT = files_footprint(package=(XI,))

    @staticmethod
    def name() -> str:
        return "xi"

    def get_version_command(self) -> str:
        return f"printf %s {VERSION}"

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        await self.ensure_system_dependencies(environment, ("curl", "unzip"))
        await self.exec_as_root(environment, command=install_command())

    def config(self) -> str:
        # Only the Codex provider: its siblings install their own dependencies.
        return "{:type :xi/config\n :version 1\n :providers [:openai]}\n"

    def rules(self) -> str:
        # One allow rule above the defaults: the one-shot prompt cannot answer asks.
        return "{:type :xi/rules\n :version 1\n :rules [{:match {} :action {:type :allow}}]}\n"

    def secret_files(self) -> dict[str, str]:
        access = credentials.codex_vis(min_token_seconds())
        return {
            ".codex/auth.json": credentials.codex_auth_json(access),
            ".config/xi/config.edn": self.config(),
            ".config/xi/rules.edn": self.rules(),
        }

    def command(self, instruction: str) -> str:
        logs = "/logs/agent"
        return (
            f"export PATH={XI}/bin:$PATH CODEX_HOME=$HOME/.codex "
            f"CLAUDE_CONFIG_DIR=$HOME/.claude XI_CODEX_REASONING_EFFORT={EFFORT} "
            f"XI_CODEX_TEXT_VERBOSITY={VERBOSITY} XI_CODEX_REASONING_SUMMARY={REASONING_SUMMARY} "
            f"XI_CODEX_USAGE_LOG={logs}/{USAGE}; "
            f"printf %s {shlex.quote(instruction)} | "
            f"xi prompt --model {self.model_name} --json "
            f"> {logs}/xi.json 2> {logs}/xi-stderr.log; code=$?; "
            f"mkdir -p {logs}/xi-sessions {logs}/xi-transcripts; "
            f'cp -a "$HOME/.config/xi/sessions/." {logs}/xi-sessions/ 2>/dev/null; '
            f'cp -a "$HOME/.claude/projects/." {logs}/xi-transcripts/ 2>/dev/null; '
            "exit $code"
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        # xi-codex-usage.patch: one line for each turn of the agent and of its subagents.
        path = self.logs_dir / USAGE
        if not path.is_file():
            return
        turns = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if not turns:
            return
        cached = sum(turn.get("cache_read_input_tokens", 0) for turn in turns)
        # The provider counts input without the cached part; Harbor counts all input.
        context.n_input_tokens = sum(turn.get("input_tokens", 0) for turn in turns) + cached
        context.n_cache_tokens = cached
        context.n_output_tokens = sum(turn.get("output_tokens", 0) for turn in turns)

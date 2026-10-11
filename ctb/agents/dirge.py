"""Dirge (github.com/yogthos/dirge) in one-shot print mode."""

import json
import shlex

from ctb import credentials
from ctb.agents.base import ARTIFACTS, EFFORT, CuratedAgent, min_token_seconds

DIRGE = "/installed-agent/dirge"


class Dirge(CuratedAgent):
    ARCHIVE = ARTIFACTS / "dirge-x86_64-unknown-linux-musl.tar.gz"

    @staticmethod
    def name() -> str:
        return "dirge"

    def get_version_command(self) -> str:
        return f"{DIRGE} --version"

    def config(self) -> dict:
        auth = "anthropic" if self.family == "anthropic" else "chatgpt"
        return {
            "provider": self.family,
            "yolo": True,
            "providers": {self.family: {"auth": auth, "model": self.model, "effort": EFFORT}},
        }

    def secret_files(self) -> dict[str, str]:
        files = {".config/dirge/config.json": json.dumps(self.config())}
        if self.family == "openai":
            access = credentials.codex_cli(min_token_seconds())
            files[".codex/auth.json"] = credentials.codex_auth_json(access)
        return files

    def secret_env(self) -> dict[str, str]:
        if self.family == "anthropic":
            return {"ANTHROPIC_OAUTH_TOKEN": credentials.anthropic(min_token_seconds()).token}
        return {}

    def command(self, instruction: str) -> str:
        return (
            "export DIRGE_DATA_DIR=$HOME/.local/share/dirge DIRGE_CONFIG_DIR=$HOME/.config/dirge; "
            f"{DIRGE} --yolo --no-session --verbose --output-format stream-json "
            f"-p {shlex.quote(instruction)} > /logs/agent/dirge.jsonl 2> /logs/agent/dirge-stderr.log"
        )

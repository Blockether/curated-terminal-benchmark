"""Keep trials on this machine: no Harbor telemetry, no Hub upload, no secrets in trial files."""

import asyncio
import codecs
import json
import os
import re
import shutil
from collections.abc import Collection, Iterator, Mapping
from pathlib import Path
from typing import Any

from harbor.models.job.plugin import BaseJobPlugin
from harbor.utils.env import is_sensitive_env_key

from ctb import credentials

TELEMETRY = "HARBOR_TELEMETRY"
REDACTOR = "ctb.privacy:Redactor"
REDACTED = "[REDACTED]"
# `harbor run` flags that send a job, its trials or its secrets to Harbor Hub.
HUB_FLAGS = frozenset(
    {"--upload", "--launch", "--public", "--share-org", "--share-user", "--stream"}
)
# The agents give trials the tokens in these files.
CREDENTIAL_FILES = (credentials.VIS_CODEX, credentials.CODEX_CLI)
# Shorter values also match ordinary words and numbers.
MIN_SECRET = 8
# Bytes of a file that redaction reads at one time.
CHUNK = 1 << 20


def telemetry_off(env: dict[str, str]) -> dict[str, str]:
    env[TELEMETRY] = "off"
    return env


def check_command(command: list[str]) -> list[str]:
    """Return a `harbor run` command; refuse other commands and the Harbor Hub flags."""
    if command[1:2] != ["run"]:
        raise ValueError(f"only `harbor run` keeps trials local, not {command[1:2]}")
    refused = sorted({arg.split("=", 1)[0] for arg in command} & HUB_FLAGS)
    if refused:
        raise ValueError(f"{', '.join(refused)} would send trials to Harbor Hub")
    return command


def private_dir(path: Path) -> Path:
    """Create a directory that only this user can open."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def json_secrets(path: Path) -> set[str]:
    """String values under sensitive keys of a JSON credential file."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return set()
    found: set[str] = set()

    def walk(node: Any, key: str) -> None:
        if isinstance(node, dict):
            for name, value in node.items():
                walk(value, str(name))
        elif isinstance(node, list):
            for value in node:
                walk(value, key)
        elif isinstance(node, str) and is_sensitive_env_key(key):
            found.add(node)

    walk(data, "")
    return found


def secret_values(
    environ: Mapping[str, str] | None = None, files: Collection[Path] | None = None
) -> set[str]:
    """Secrets that a trial can get: sensitive environment values and credential tokens."""
    environ = os.environ if environ is None else environ
    found: set[str] = set()
    for name, value in environ.items():
        if not is_sensitive_env_key(name):
            continue
        if value.endswith(".json") and Path(value).is_file():
            found |= json_secrets(Path(value))
        else:
            found.add(value)
    for path in CREDENTIAL_FILES if files is None else files:
        found |= json_secrets(path)
    return {value for value in found if len(value) >= MIN_SECRET and not value.isdigit()}


def redact_tree(root: Path, secrets: Collection[str]) -> list[Path]:
    """Replace secret values in the text files under root; return the changed files."""
    ordered = sorted(secrets, key=len, reverse=True)
    pattern = re.compile("|".join(map(re.escape, ordered)))
    changed = []
    for path in sorted(root.rglob("*")) if ordered else []:
        if path.is_symlink() or not path.is_file():
            continue
        # A first pass only looks: most files have no secret and are not written again.
        temp = path.with_name(f".{path.name}.redacting")
        try:
            if not any(hit for _, hit in redacted(path, ordered, pattern)):
                continue
            with temp.open("w", encoding="utf-8", newline="") as out:
                for text, _ in redacted(path, ordered, pattern):
                    out.write(text)
            shutil.copymode(path, temp)
            os.replace(temp, path)
        except (OSError, UnicodeDecodeError, ValueError):
            temp.unlink(missing_ok=True)
            continue
        changed.append(path)
    return changed


def redacted(
    path: Path, ordered: list[str], pattern: re.Pattern[str]
) -> Iterator[tuple[str, bool]]:
    """The text of a file in parts with each secret replaced, and whether a part had a secret.

    An agent log can be larger than the memory, so the file is never read whole. Raises
    UnicodeDecodeError for a file that is not UTF-8 and ValueError for a binary file.
    """
    # A match that starts in the last `keep` characters can continue in the next part.
    keep = len(ordered[0]) - 1
    decoder = codecs.getincrementaldecoder("utf-8")()
    tail = ""
    with path.open("rb") as source:
        while True:
            raw = source.read(CHUNK)
            text = tail + decoder.decode(raw, final=not raw)
            if "\0" in text:
                raise ValueError(f"{path} is binary")
            cut = max(0, len(text) - keep) if raw else len(text)
            parts, done = [], 0
            if any(secret in text for secret in ordered):
                for match in pattern.finditer(text):
                    if match.start() >= cut:
                        break
                    parts += [text[done : match.start()], REDACTED]
                    done = match.end()
            cut = max(cut, done)
            yield "".join(parts) + text[done:cut], done > 0
            tail = text[cut:]
            if not raw:
                return


class Redactor(BaseJobPlugin):
    """Harbor job plugin: remove secret values from each trial when it ends, then from the job."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.job_dir: Path | None = None
        self.secrets: set[str] = set()

    def current_secrets(self) -> set[str]:
        # Tokens can refresh during a trial, so keep the old values too.
        self.secrets |= secret_values()
        return self.secrets

    async def on_job_start(self, job: Any) -> None:
        self.job_dir = Path(job.job_dir)
        self.current_secrets()
        job.on_trial_ended(self.on_trial_end)

    async def on_trial_end(self, event: Any) -> None:
        trial = Path(event.config.trials_dir) / event.config.trial_name
        await asyncio.to_thread(redact_tree, trial, self.current_secrets())

    async def on_job_end(self, job_result: Any) -> None:
        del job_result
        if self.job_dir is not None:
            await asyncio.to_thread(redact_tree, self.job_dir, self.current_secrets())

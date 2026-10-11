"""Vis with and without Council subagents, on one pinned build and named profiles."""

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ctb import credentials
from ctb.agents.base import (
    ARTIFACTS,
    EFFORT,
    FAMILIES,
    ROOT,
    CuratedAgent,
    min_token_seconds,
    split_model,
)

#: Vis providers of the benchmark families. They use the subscription token files.
PROVIDERS = {"anthropic": "anthropic-coding-plan", "openai": "openai-codex"}
#: Other Vis providers read an API key from this host variable.
API_KEYS = {"zai-coding-plan": "ZAI_CODING_API_KEY"}
SETTINGS = ROOT / "profiles.toml"
VIS = "/installed-agent/vis-agent"
LOGS = "/logs/agent"
TRACE = f"{LOGS}/vis-trace.jsonl"
TRAJECTORY = f"{LOGS}/trajectory.json"
STAMP = "vis-agent-native.build"
CHUNK = 1 << 20


def settings() -> dict:
    """The build pin and the profiles of profiles.toml."""
    return tomllib.loads(SETTINGS.read_text(encoding="utf-8"))


def archive(build: dict) -> Path:
    """The local copy of the pinned bundle. `fetch` puts it there."""
    return ARTIFACTS / "vis-builds" / str(build["run_id"]) / build["asset"]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stamp(path: Path) -> list[str]:
    """The build stamp of a bundle: version, commit, track and build data."""
    with tarfile.open(path) as bundle:
        for member in bundle:
            if Path(member.name).name == STAMP and member.isfile():
                handle = bundle.extractfile(member)
                if handle is not None:
                    return handle.read().decode("utf-8").split()
    raise ValueError(f"{path} has no {STAMP}")


def check(path: Path, build: dict) -> None:
    """Refuse a bundle that is not the pinned build."""
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing Vis build {path}; run `uv run python -m ctb.agents.vis fetch`."
        )
    digest = file_sha256(path)
    if digest != build["sha256"]:
        raise ValueError(f"{path} has sha256 {digest}, not the pinned {build['sha256']}")


def fetch(build: dict | None = None) -> Path:
    """Download the pinned workflow artifact once, then check its commit and sha256.

    GitHub keeps a dry-run artifact for 7 days. After that, only the local copy is left.
    """
    build = build or settings()["build"]
    target = archive(build)
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ctb-vis-", dir=target.parent) as work:
            subprocess.run(
                ["gh", "run", "download", str(build["run_id"]), "-R", build["repo"]]
                + ["-n", build["asset"], "-D", work],
                check=True,
            )
            download = Path(work) / build["asset"]
            commit = stamp(download)[1]
            if commit != build["commit"]:
                raise ValueError(
                    f"Run {build['run_id']} built {commit}, not the pinned {build['commit']}"
                )
            check(download, build)
            download.replace(target)
    check(target, build)
    return target


def provider_model(model_name: str | None) -> tuple[str, str]:
    """A benchmark model, or `provider/model` for a Vis provider with an API key."""
    family, _, model = (model_name or "").partition("/")
    if family in API_KEYS and model:
        return family, model
    if family not in FAMILIES:
        allowed = [f"{f}/{m}" for f, m in FAMILIES.items()] + [f"{p}/<model>" for p in API_KEYS]
        raise ValueError(f"Use --model {', '.join(allowed)}; got {model_name!r}")
    return split_model(model_name)


def toggle_list(toggles: dict) -> str:
    """The `--toggles` value: `name=value` pairs, with booleans in lower case."""
    return ",".join(
        f"{name}={str(value).lower() if isinstance(value, bool) else value}"
        for name, value in toggles.items()
    )


def overlay(base: dict, extra: dict) -> dict:
    """Merge `extra` into `base`; nested tables merge, other values replace."""
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = overlay(merged[key], value)
        else:
            merged[key] = value
    return merged


def result_frame(path: Path) -> dict | None:
    """Read the last `result` frame of a full-trace JSON stream."""
    if not path.is_file():
        return None
    result = None
    for line in path.read_text(errors="replace").splitlines():
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        if frame.get("event") == "result" and isinstance(frame.get("payload"), dict):
            result = frame["payload"]
    return result


def count(value: object) -> int | None:
    """A token count, or None for a value that is not a whole number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value < 0 or int(value) != value:
        return None
    return int(value)


class VisSolo(CuratedAgent):
    """One Vis agent: Council and subagents off.

    The Harbor kwarg `profile` selects another profile of profiles.toml.
    """

    PROFILE = "solo"

    def __init__(self, *args, profile: str | None = None, **kwargs) -> None:
        # CuratedAgent.__init__ accepts only the two benchmark models. Vis also runs
        # providers with an API key (terminal_bench/), so this class checks the model.
        BaseInstalledAgent.__init__(self, *args, **kwargs)
        self.family, self.model = provider_model(self.model_name)
        found = settings()
        self.profile_name = profile or self.PROFILE
        if self.profile_name not in found["profiles"]:
            names = ", ".join(sorted(found["profiles"]))
            raise ValueError(f"Unknown Vis profile {self.profile_name!r}; use one of {names}")
        self.profile = found["profiles"][self.profile_name]
        self.build = found["build"]
        self.ARCHIVE = archive(self.build)

    @staticmethod
    def name() -> str:
        return "vis-solo"

    def get_version_command(self) -> str:
        return f"{VIS} --version"

    async def install(self, environment: BaseEnvironment) -> None:
        check(self.ARCHIVE, self.build)
        await super().install(environment)

    def secret_files(self) -> dict[str, str]:
        if self.family == "anthropic":
            access = credentials.anthropic(min_token_seconds())
            return {".vis/anthropic-auth.json": credentials.vis_anthropic_file(access)}
        if self.family == "openai":
            access = credentials.codex_vis(min_token_seconds())
            return {".vis/openai-codex-auth.json": credentials.vis_codex_file(access)}
        return {}

    def secret_env(self) -> dict[str, str]:
        name = API_KEYS.get(self.family)
        if name is None:
            return {}
        key = os.environ.get(name)
        if not key:
            raise RuntimeError(f"{name} is required on the Harbor host")
        return {name: key}

    def command(self, instruction: str) -> str:
        provider = PROVIDERS.get(self.family, self.family)
        config = overlay(
            {
                "providers": [{"id": provider}],
                "default_provider": provider,
                "default_model": self.model,
            },
            self.profile.get("config", {}),
        )
        flags = [f"--name {shlex.quote(self.profile_name)}"]
        flags.append("--persist" if self.profile.get("persist") else "--db :memory")
        if self.profile.get("toggles"):
            flags.append(f"--toggles {shlex.quote(toggle_list(self.profile['toggles']))}")
        if "extensions" in self.profile:
            names = ",".join(self.profile["extensions"]) or "none"
            flags.append(f"--extensions {shlex.quote(names)}")
        effort = self.profile.get("effort", EFFORT)
        # JSON is valid YAML, so the overlay needs no YAML writer.
        return (
            f"mkdir -p $HOME/.vis && printf %s {shlex.quote(json.dumps(config))} "
            "> $HOME/.vis/config.yml && export VIS_HOME=$HOME/.vis && "
            f"{VIS} {' '.join(flags)} --model {provider}/{self.model} "
            f"--reasoning-effort {shlex.quote(effort)} --trajectory {TRAJECTORY} "
            f"--full-trace-json-stream -- {shlex.quote(instruction)} "
            f"> {TRACE} 2> {LOGS}/vis-stderr.log"
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        result = result_frame(self.logs_dir / "vis-trace.jsonl")
        if result is None:
            return
        tokens = result.get("tokens") or {}
        context.n_input_tokens = count(tokens.get("input"))
        context.n_cache_tokens = count(tokens.get("cached"))
        context.n_output_tokens = count(tokens.get("output"))
        estimate = (result.get("cost") or {}).get("total_cost")
        if isinstance(estimate, bool) or not isinstance(estimate, int | float):
            estimate = None
        # A subscription bills no price for each trial, so context.cost_usd stays None.
        context.metadata = {
            "vis": {
                "model": self.model_name,
                "profile": self.profile_name,
                "build": {key: self.build[key] for key in ("run_id", "commit", "sha256")},
                "status": result.get("status"),
                "duration_ms": result.get("duration-ms", result.get("duration_ms")),
                "iteration_count": result.get("iteration-count", result.get("iteration_count")),
                "estimated_metered_api_cost_usd": estimate,
                "eval": result.get("eval"),
            }
        }


class VisTeam(VisSolo):
    """Vis with Council on: the leader can run subagents with council.run."""

    PROFILE = "team"

    @staticmethod
    def name() -> str:
        return "vis-team"


#: The default profile of each Vis agent of the matrix.
AGENT_PROFILES = {VisSolo.name(): VisSolo.PROFILE, VisTeam.name(): VisTeam.PROFILE}


def run_record(agents: list[str]) -> dict:
    """The build pin and the profile of each Vis agent in one `ctb run` call."""
    found = settings()
    build = found["build"]
    return {
        "build": {key: build[key] for key in ("repo", "run_id", "commit", "sha256")},
        "profiles": {
            agent: {"name": AGENT_PROFILES[agent], **found["profiles"][AGENT_PROFILES[agent]]}
            for agent in agents
            if agent in AGENT_PROFILES
        },
    }


if __name__ == "__main__":
    if sys.argv[1:] != ["fetch"]:
        raise SystemExit("usage: python -m ctb.agents.vis fetch")
    path = fetch()
    print(f"{path.relative_to(ROOT)}: {path.stat().st_size} B")

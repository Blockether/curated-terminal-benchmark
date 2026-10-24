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
    VERBOSITY,
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

    def task_text(self, instruction: str) -> str:
        """The task instruction with the `instruction_note` of the profile, if it has one."""
        note = self.profile.get("instruction_note")
        return f"{instruction}\n\n{note}" if note else instruction

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
        # Vis sends `verbosity` only to models that take it, so it is safe for every family.
        toggles = {"verbosity": VERBOSITY, **self.profile.get("toggles", {})}
        flags.append(f"--toggles {shlex.quote(toggle_list(toggles))}")
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
            f"--full-trace-json-stream -- {shlex.quote(self.task_text(instruction))} "
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
                "instruction_note": self.profile.get("instruction_note"),
                "build": {key: self.build[key] for key in ("run_id", "commit", "sha256")},
                "error_count": sum(
                    isinstance(block, dict) and block.get("type") == "error"
                    for block in result.get("content") or []
                ),
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


class VisTeamNote(VisTeam):
    """Council on, and the task tells the leader to use subagents."""

    PROFILE = "team-note"

    @staticmethod
    def name() -> str:
        return "vis-team-note"


class VisTeamWait(VisTeam):
    """Council subagents with `council_orchestration = wait`: the leader waits for them."""

    PROFILE = "team-wait"

    @staticmethod
    def name() -> str:
        return "vis-team-wait"


class VisTeamActive(VisTeam):
    """Council subagents with `council_orchestration = active`: the leader steers them."""

    PROFILE = "team-active"

    @staticmethod
    def name() -> str:
        return "vis-team-active"


#: The default profile of each Vis agent of the matrix.
AGENT_PROFILES = {
    cls.name(): cls.PROFILE for cls in (VisSolo, VisTeam, VisTeamNote, VisTeamWait, VisTeamActive)
}


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


def run_build(run_id: int, repo: str = "Blockether/vis") -> dict:
    """The build pin of one successful native-release.yml run on any branch.

    The commit is the head commit of the run. The bundle stamp must name that commit.
    The sha256 comes from the downloaded bundle, which stays in artifacts/vis-builds/<run_id>/.
    """
    view = subprocess.run(
        ["gh", "run", "view", str(run_id), "-R", repo, "--json", "headSha,conclusion"],
        check=True,
        capture_output=True,
        text=True,
    )
    info = json.loads(view.stdout)
    if info.get("conclusion") != "success":
        raise ValueError(f"Run {run_id} has conclusion {info.get('conclusion')!r}, not success")
    build = {**settings()["build"], "repo": repo, "run_id": run_id, "commit": info["headSha"]}
    target = archive(build)
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ctb-vis-", dir=target.parent) as work:
            subprocess.run(
                ["gh", "run", "download", str(run_id), "-R", repo]
                + ["-n", build["asset"], "-D", work],
                check=True,
            )
            (Path(work) / build["asset"]).replace(target)
    commit = stamp(target)[1]
    if commit != build["commit"]:
        target.unlink()
        raise ValueError(f"Run {run_id} built {commit}, not its head commit {build['commit']}")
    build["sha256"] = file_sha256(target)
    return build


def write_pin(build: dict, path: Path | None = None) -> None:
    """Replace the run_id, commit and sha256 of the [build] table of profiles.toml."""
    path = path or SETTINGS
    text = path.read_text(encoding="utf-8")
    head, sep, rest = text.partition("[build]\n")
    if not sep:
        raise ValueError(f"{path} has no [build] table")
    lines = rest.split("\n")
    for index, line in enumerate(lines):
        if line.startswith("["):
            break
        key = line.partition("=")[0].strip()
        if key in ("repo", "run_id", "commit", "sha256"):
            lines[index] = f"{key} = {json.dumps(build[key])}"
    path.write_text(head + sep + "\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1:] == ["fetch"]:
        path = fetch()
    elif len(sys.argv) == 3 and sys.argv[1] == "pin":
        pinned = run_build(int(sys.argv[2]))
        write_pin(pinned)
        print(f"pinned run {pinned['run_id']}: commit {pinned['commit']}")
        path = archive(pinned)
    else:
        raise SystemExit("usage: python -m ctb.agents.vis fetch | pin <native-release.yml run id>")
    print(f"{path.relative_to(ROOT)}: {path.stat().st_size} B")

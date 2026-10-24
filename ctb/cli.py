"""Fetch agents, run the curated matrix in Harbor and summarize PASS rates."""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from importlib import metadata
from pathlib import Path

from blockether.vis.evals import HarborTrial, read_trial, read_trials, seal_round

from ctb import credentials, privacy
from ctb.agents import dirge, kmet, opencode, pi, samizdat, vis
from ctb.agents.base import EFFORT, min_token_seconds
from ctb.matrix import (
    AGENTS,
    DEFAULT_AGENTS,
    MODELS,
    ROOT,
    Trial,
    load_tasks,
    select_tasks,
    trials,
)
from ctb.metrics import round_metrics
from ctb.proxy import Proxy

ARTIFACTS = ROOT / "artifacts"
RESULTS = ROOT / "results"
# The full-suite Vis harness (moved out of the Vis repository) owns the shared artifacts.
SUITE_ARTIFACTS = ROOT / "terminal_bench" / "artifacts"
DATASET = Path(os.environ.get("CTB_DATASET", SUITE_ARTIFACTS / "datasets" / "terminal-bench"))
COMPOSE = SUITE_ARTIFACTS / "docker-compose"
# Pinned master commits. Dirge patches preserve provider token accounting.
DIRGE_REPO, DIRGE_COMMIT = "https://github.com/dirge-code/dirge.git", "f36b2890"
KMET_REPO, KMET_COMMIT = (
    "https://github.com/kmetia/kmet-agent.git",
    "4ab740ff02aa3f92fc887c7644c124021a4aa519",
)
SAMIZDAT_COMMIT = "56d2be18f9b4c073e2fbf244d64ff5017595c305"
# Task containers reach the host proxy here (Podman's name for the host).
PROXY_HOST = "host.containers.internal"
# The newest Vis release with a native Linux x64 bundle.
VIS_RELEASE = "v0.2.37"
BB_MIN = "1.13.225"
SRC = ARTIFACTS / "src"
DIRGE_BUILD = (
    "apt-get update -qq && apt-get install -y -qq musl-tools clang mold >/dev/null && "
    "rustup target add x86_64-unknown-linux-musl >/dev/null && "
    "cargo test --release --target x86_64-unknown-linux-musl --bin dirge -- "
    "codex_completed_response_keeps_usage codex_body_gets_gpt_settings && "
    "cargo build --release --target x86_64-unknown-linux-musl --bin dirge"
)


def sh(command: list[str], cwd: Path | None = None, env: dict | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def checkout(repo: str, commit: str, name: str) -> Path:
    path = SRC / name
    if not path.is_dir():
        SRC.mkdir(parents=True, exist_ok=True)
        sh(["git", "clone", "-q", repo, str(path)])
    sh(["git", "fetch", "-q", "origin"], cwd=path)
    sh(["git", "checkout", "-q", "--force", commit], cwd=path)
    sh(["git", "clean", "-qfd", "-e", "target-linux"], cwd=path)
    return path


def pack(binary: Path, archive: Path, name: str) -> None:
    # A running round uploads the archive for each trial, so replace it in one step.
    partial = archive.with_name(archive.name + ".partial")
    with tarfile.open(partial, "w:gz") as out:
        out.add(binary, arcname=name)
    partial.replace(archive)


def apply_patches(path: Path, *names: str) -> None:
    for name in names:
        sh(["git", "apply", str(ROOT / "patches" / name)], cwd=path)


def prepare_dirge_rig(path: Path) -> None:
    """Extract the locked Rig crate before applying the streaming parser patch."""
    packages = tomllib.loads((path / "Cargo.lock").read_text())["package"]
    package = next(p for p in packages if p["name"] == "rig-core")
    if package["version"] != "0.41.0":
        raise RuntimeError("The Dirge streaming patch requires rig-core 0.41.0")
    name = "rig-core-0.41.0.crate"
    archive = download(
        f"https://static.crates.io/crates/rig-core/{name}",
        ARTIFACTS / name,
        package["checksum"],
    )
    with tarfile.open(archive) as source:
        source.extractall(path / "vendor", filter="data")


def fetch_dirge() -> Path:
    archive = ARTIFACTS / dirge.ARCHIVE_NAME
    if not archive.is_file():
        path = checkout(DIRGE_REPO, DIRGE_COMMIT, "dirge")
        prepare_dirge_rig(path)
        apply_patches(
            path, "dirge-usage.patch", "dirge-rig-stream.patch", "dirge-codex-settings.patch"
        )
        sh(
            [
                "podman",
                "run",
                "--rm",
                "--platform",
                "linux/amd64",
                "-v",
                f"{path}:/src",
                "-w",
                "/src",
                "-e",
                "CARGO_TARGET_DIR=/src/target-linux",
                "docker.io/library/rust:1-bookworm",
                "bash",
                "-c",
                DIRGE_BUILD,
            ]
        )
        pack(path / "target-linux/x86_64-unknown-linux-musl/release/dirge", archive, "dirge")
    return archive


def download(url: str, path: Path, sha256: str | None = None) -> Path:
    """Download to `path`; check the SHA-256 from `url`.sha256 or the given digest."""
    if not path.is_file():
        with urllib.request.urlopen(url, timeout=300) as response:
            path.write_bytes(response.read())
    if sha256 is None:
        with urllib.request.urlopen(url + ".sha256", timeout=60) as response:
            sha256 = response.read().decode().split()[0]
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != sha256:
        path.unlink()
        raise RuntimeError(f"SHA-256 mismatch for {url}: {actual} != {sha256}")
    return path


def fetch_samizdat() -> Path:
    url = f"https://github.com/yogthos/samizdat/releases/download/{samizdat.VERSION}/{samizdat.ARCHIVE_NAME}"
    return download(url, samizdat.Samizdat.ARCHIVE, samizdat.ARCHIVE_SHA256)


def fetch_vis() -> Path:
    """The native Linux x64 Vis bundle of VIS_RELEASE, checked against its SHA256SUMS."""
    archive = ARTIFACTS / "vis-agent-linux-amd64.tar.gz"
    if not archive.is_file():
        name = "vis-agent-linux-x64.tar.gz"
        with tempfile.TemporaryDirectory(prefix="ctb-vis-", dir=ARTIFACTS) as work:
            sh(
                ["gh", "release", "download", VIS_RELEASE, "-R", "Blockether/vis"]
                + ["-p", name, "-p", "SHA256SUMS", "-D", work]
            )
            sums = dict(
                reversed(line.split())
                for line in (Path(work) / "SHA256SUMS").read_text().splitlines()
            )
            download_path = Path(work) / name
            actual = hashlib.sha256(download_path.read_bytes()).hexdigest()
            if actual != sums.get(name):
                raise RuntimeError(f"SHA-256 mismatch for {name}")
            download_path.rename(archive)
    return archive


def fetch_kmet() -> Path:
    archive = ARTIFACTS / kmet.ARCHIVE_NAME
    if not archive.is_file():
        path = checkout(KMET_REPO, KMET_COMMIT, "kmet-agent")
        env = dict(os.environ, PATH=f"{ARTIFACTS / 'tools'}:{os.environ['PATH']}")
        sh(["bb", "dist", "linux-amd64"], cwd=path, env=env)
        pack(path / "target/dist/linux-amd64/kmet", archive, "kmet")
    return archive


def fetch() -> None:
    """Get every agent for linux-amd64: native Vis, dirge, kmet and samizdat.

    Vis needs the gh CLI: it gets the release and the build of profiles.toml. kmet needs
    Babashka >= BB_MIN on PATH or in artifacts/tools; dirge and samizdat need Podman.
    """
    ARTIFACTS.mkdir(exist_ok=True)
    for archive in (fetch_vis(), vis.fetch(), fetch_dirge(), fetch_kmet(), fetch_samizdat()):
        print(f"{archive.name}: {archive.stat().st_size} B")


def podman_config() -> Path:
    """Harbor parses command output; the Podman compose banner breaks that parse."""
    path = ARTIFACTS / "containers.conf"
    if not path.is_file():
        ARTIFACTS.mkdir(exist_ok=True)
        path.write_text("[engine]\ncompose_warning_logs = false\n")
    return path


def host_env(trial: Trial, secret_dir: Path, proxy: Proxy | None = None) -> dict[str, str]:
    """Process environment of one Harbor job; built-in agents read tokens from it."""
    env = dict(os.environ, PYTHONPATH=str(ROOT), CONTAINERS_CONF_OVERRIDE=str(podman_config()))
    privacy.telemetry_off(env)
    if trial.agent.proxy:
        if proxy is None:
            raise credentials.CredentialError(f"{trial.agent.name} needs the model proxy")
        env["CTB_PROXY_URL"] = f"http://{PROXY_HOST}:{proxy.port}"
        env["CTB_PROXY_KEY"] = proxy.key
    if "PODMAN_COMPOSE_PROVIDER" not in env and COMPOSE.is_file():
        env["PODMAN_COMPOSE_PROVIDER"] = str(COMPOSE)
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(key, None)
    if trial.family == "anthropic" and not trial.agent.proxy:
        # Agents read the token again inside Harbor: a short token defers the trial here.
        credentials.anthropic(min_token_seconds())
    if trial.agent.name == "claude-code":
        env["CLAUDE_FORCE_OAUTH"] = "1"
        env["CLAUDE_CODE_OAUTH_TOKEN"] = credentials.anthropic(min_token_seconds()).token
    if trial.agent.name == "codex":
        auth = secret_dir / "auth.json"
        auth.write_text(credentials.codex_auth_json(credentials.codex_cli(min_token_seconds())))
        auth.chmod(0o600)
        env["CODEX_AUTH_JSON_PATH"] = str(auth)
    return env


def harbor_command(trial: Trial, jobs: Path, multiplier: float) -> list[str]:
    command = [
        str(Path(sys.executable).parent / "harbor"),
        "run",
        "-p",
        str(DATASET),
        "-a",
        trial.agent.harbor,
        "-m",
        trial.model,
        "-e",
        "podman",
        "-k",
        "1",
        "-n",
        "1",
        "-o",
        str(jobs),
        "--job-name",
        trial.job,
        "-i",
        trial.task,
        "--agent-timeout-multiplier",
        str(multiplier),
        "--agent-setup-timeout-multiplier",
        str(SETUP_MULTIPLIER),
        "--plugin",
        privacy.REDACTOR,
    ]
    if trial.agent.effort_kwarg:
        command += ["--ak", f"reasoning_effort={EFFORT}"]
    for kwarg in trial.agent.agent_kwargs:
        command += ["--ak", kwarg]
    if trial.agent.version:
        command += ["--ak", f"version={trial.agent.version}"]
    return privacy.check_command(command)


# Harbor's own timeouts can fail to stop a trial; this margin covers setup and teardown.
HARD_LIMIT_MARGIN_S = 1800
# Harbor gives an agent install 360 s. npm and nvm installs under amd64 emulation can take longer,
# and a setup timeout is a harness failure, not an agent failure.
AGENT_SETUP_TIMEOUT_S = 360
SETUP_MULTIPLIER = 5
TIMEOUT_DIR = "ctb-timeout"


def hard_limit(task: str, multiplier: float) -> float:
    """Seconds after which ctb stops a Harbor trial that its own timeouts did not stop."""
    info = tomllib.loads((DATASET / task / "task.toml").read_text())
    environment, verifier = info.get("environment", {}), info.get("verifier", {})
    return (
        info["agent"]["timeout_sec"] * multiplier
        + verifier.get("timeout_sec", 0)
        + environment.get("build_timeout_sec", 0)
        + verifier.get("environment", {}).get("build_timeout_sec", 0)
        + AGENT_SETUP_TIMEOUT_S * SETUP_MULTIPLIER
        + HARD_LIMIT_MARGIN_S
    )


def stop_group(process: subprocess.Popen) -> None:
    """Stop Harbor and its podman compose children."""
    for sig, grace in ((signal.SIGTERM, 60), (signal.SIGKILL, None)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def remove_containers(job: Path, env: dict[str, str]) -> None:
    """Remove the containers of a stopped trial; their names start with the trial name."""
    for trial_dir in (p for p in job.iterdir() if p.is_dir()) if job.is_dir() else []:
        found = subprocess.run(
            ["podman", "ps", "-aq", "--filter", f"name={trial_dir.name.lower()}"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        ids = found.stdout.split()
        if ids:
            subprocess.run(["podman", "rm", "-f", *ids], env=env, capture_output=True, check=False)


def record_timeout(jobs: Path, trial: Trial, limit: float) -> None:
    """Give a trial that ctb stopped a Harbor result without reward, so it counts as a fail."""
    path = jobs / trial.job / TIMEOUT_DIR / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    message = f"Harbor did not stop the trial; ctb stopped it after {limit:.0f} seconds"
    agent = {"name": trial.agent.name, "model_name": MODELS[trial.family]}
    path.write_text(
        json.dumps(
            {
                # read_trial needs the task, trial and finish keys of a Harbor result.
                "task_name": trial.task,
                "trial_name": trial.job,
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "config": {"agent": agent},
                "agent_info": {"name": trial.agent.name},
                "source": "ctb",
                "verifier_result": None,
                "agent_result": None,
                "exception_info": {
                    "exception_type": "HarnessTimeoutError",
                    "exception_message": message,
                },
            },
            indent=2,
        )
        + "\n"
    )


def trial_result(jobs: Path, trial: Trial) -> dict | None:
    for path in sorted((jobs / trial.job).glob("*/result.json")):
        return json.loads(path.read_text())
    return None


class PodmanDown(RuntimeError):
    """The container VM is not reachable, so no trial can run."""


def podman_ready(env: dict[str, str] | None = None) -> bool:
    try:
        probe = subprocess.run(
            ["podman", "info", "--format", "{{.Host.Arch}}"],
            env=env,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def run_trial(
    trial: Trial, jobs: Path, logs: Path, multiplier: float, proxy: Proxy | None = None
) -> str:
    if trial_result(jobs, trial) is not None:
        return f"skip {trial.job} (done)"
    if not podman_ready():
        raise PodmanDown("Podman is not reachable: start the VM, then run the round again")
    with tempfile.TemporaryDirectory(prefix="ctb-") as secrets:
        os.chmod(secrets, 0o700)
        try:
            env = host_env(trial, Path(secrets), proxy)
        except credentials.ExpiringToken as error:
            return f"defer {trial.job}: {error}"
        except credentials.CredentialError as error:
            return f"blocked {trial.job}: {error}"
        started = time.monotonic()
        limit = hard_limit(trial.task, multiplier)
        with open(logs / f"{trial.job}.log", "w") as log:
            harbor = subprocess.Popen(
                harbor_command(trial, jobs, multiplier),
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                code: int | str = harbor.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                stop_group(harbor)
                code = "timeout"
    if not podman_ready(env):
        # A VM stop is not an agent result: remove the job, so the next run repeats it.
        shutil.rmtree(jobs / trial.job, ignore_errors=True)
        raise PodmanDown(
            f"Podman stopped during {trial.job}: start the VM, then run the round again"
        )
    if code == "timeout":
        remove_containers(jobs / trial.job, env)
        if trial_result(jobs, trial) is None:
            record_timeout(jobs, trial, limit)
    reward = reward_of(trial_result(jobs, trial))
    return f"done {trial.job}: exit={code} reward={reward} {time.monotonic() - started:.0f}s"


def reward_of(result: dict | None) -> float | None:
    if not result:
        return None
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    return rewards.get("reward")


def run(args: argparse.Namespace) -> None:
    jobs = ROOT / "jobs" / args.round
    logs = ROOT / "runs" / args.round
    privacy.private_dir(jobs)
    privacy.private_dir(logs)
    selected = trials(
        args.agents.split(","), args.models.split(","), select_tasks(args.tasks, load_tasks())
    )
    print(f"{len(selected)} trials, parallel {args.parallel}, round {args.round}", flush=True)
    record_run(jobs, args)
    # Only proxy agents need it; its log has token counts and errors, never content.
    proxy = Proxy(logs / "proxy.jsonl").start() if any(t.agent.proxy for t in selected) else None
    try:
        pending = selected
        while pending:
            deferred = []
            with ThreadPoolExecutor(args.parallel) as pool:
                lines = pool.map(
                    lambda t: run_trial(t, jobs, logs, args.timeout_multiplier, proxy), pending
                )
                for trial, line in zip(pending, lines):
                    print(line, flush=True)
                    if line.startswith("defer "):
                        deferred.append(trial)
            if deferred:
                print(f"{len(deferred)} trials wait for a Claude token refresh", flush=True)
                credentials.refresh_anthropic(min_token_seconds())
            pending = deferred
    except PodmanDown as error:
        raise SystemExit(str(error)) from error
    finally:
        if proxy is not None:
            proxy.stop()


def git_state() -> dict:
    def git(*argv: str) -> str:
        return subprocess.run(
            ["git", *argv], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()

    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def pins() -> dict:
    return {
        "vis": VIS_RELEASE,
        "dirge": DIRGE_COMMIT,
        "kmet": KMET_COMMIT,
        "samizdat": SAMIZDAT_COMMIT,
        "samizdat_release": samizdat.VERSION,
        "harbor": metadata.version("harbor"),
        "harnesses": {
            "claude-code": AGENTS["claude-code"].version,
            "codex": AGENTS["codex"].version,
            "opencode": opencode.OPENCODE_VERSION,
            "pi": pi.PI_VERSION,
        },
        "auth_plugins": {"opencode": opencode.ANTHROPIC_PLUGIN, "pi": pi.ANTHROPIC_PLUGIN},
        "opencode_anthropic_max_output_tokens": 128000,
        "models": MODELS,
        "effort": EFFORT,
    }


def record_run(jobs: Path, args: argparse.Namespace) -> None:
    """Append the harness state of one `run` call to the round; `seal` copies it."""
    path = jobs / "ctb-runs.json"
    record = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **git_state(),
        "pins": pins(),
        "artifacts": {p.name: file_sha256(p) for p in sorted(ARTIFACTS.glob("*.tar.gz"))},
        "agents": args.agents,
        "models": args.models,
        "tasks": args.tasks,
        "parallel": args.parallel,
        "timeout_multiplier": args.timeout_multiplier,
    }
    vis_agents = [name for name in args.agents.split(",") if name in vis.AGENT_PROFILES]
    if vis_agents:
        # The Vis agents run the build and the profiles of profiles.toml.
        record["vis"] = vis.run_record(vis_agents)
    # Several `run` calls can share one round, so read, append and replace under a lock.
    with open(jobs / "ctb-runs.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        runs = json.loads(path.read_text()) if path.is_file() else []
        runs.append(record)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(runs, indent=2) + "\n")
        os.replace(temp, path)


def rows(jobs: Path) -> list[dict]:
    if not jobs.is_dir():
        return []
    return [
        {
            "agent": trial.agent,
            "model": trial.model,
            "task": trial.task,
            "reward": trial.reward,
            "error": trial.error,
            "input": trial.input_tokens,
            "output": trial.output_tokens,
        }
        for trial in read_trials(jobs)
    ]


def summary(args: argparse.Namespace) -> None:
    table = rows(ROOT / "jobs" / args.round)
    levels = {t["name"]: t["level"] for t in load_tasks()}
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in table:
        groups.setdefault((row["agent"], row["model"]), []).append(row)
    print(
        f"{'agent':15} {'model':28} {'pass':>7} {'easy':>6} {'hard':>6} {'errors':>6} {'in_med':>9}"
    )
    for (agent, model), items in sorted(groups.items()):

        def passed(rows_):
            return sum(1 for r in rows_ if (r["reward"] or 0) >= 1)

        # Harbor names a task with its dataset, for example terminal-bench/<task>.
        easy = [r for r in items if levels.get(r["task"].rpartition("/")[2]) == "easy"]
        hard = [r for r in items if levels.get(r["task"].rpartition("/")[2]) == "hard"]
        inputs = [r["input"] for r in items if isinstance(r["input"], int)]
        median = int(statistics.median(inputs)) if inputs else "-"
        print(
            f"{agent:15} {model:28} {passed(items):>3}/{len(items):<3} "
            f"{passed(easy):>2}/{len(easy):<3} {passed(hard):>2}/{len(hard):<3} "
            f"{sum(1 for r in items if r['error']):>6} {median:>9}"
        )
    if args.json:
        Path(args.json).write_text(json.dumps(table, indent=2))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def harbor_trial(jobs: Path, trial: Trial) -> HarborTrial | None:
    """Return the first finished trial of a ctb job, or None."""
    for path in sorted((jobs / trial.job).glob("*/result.json")):
        if (found := read_trial(path)) is not None:
            return found
    return None


def seal(args: argparse.Namespace) -> None:
    """Write results/<round>.json once: run records, one row per trial and result hashes."""
    jobs = ROOT / "jobs" / args.round
    out = RESULTS / f"{args.round}.json"
    if out.exists():
        raise SystemExit(f"{out} is already sealed; run a new round")
    runs_path = jobs / "ctb-runs.json"
    runs = json.loads(runs_path.read_text()) if runs_path.is_file() else []
    if not runs or any(r["dirty"] for r in runs):
        raise SystemExit(f"{args.round} has no run record from a clean harness commit")
    expected = trials(
        args.agents.split(","), args.models.split(","), select_tasks(args.tasks, load_tasks())
    )
    found = {t.job: harbor_trial(jobs, t) for t in expected}
    missing = [job for job, trial in found.items() if trial is None]
    if missing:
        raise SystemExit(f"{len(missing)} trials have no result: {', '.join(missing)}")
    body = seal_round(args.round, runs, found.values(), ROOT)
    RESULTS.mkdir(exist_ok=True)
    out.write_text(json.dumps(body, indent=2) + "\n")
    print(f"sealed {len(body['rows'])} trials in {out.relative_to(ROOT)}, digest {body['digest']}")


def metrics(args: argparse.Namespace) -> None:
    """Write results/<round>-metrics.json from a sealed round and print one row per agent."""
    sealed = RESULTS / f"{args.round}.json"
    if not sealed.is_file():
        raise SystemExit(f"{sealed} does not exist; seal the round first")
    body = round_metrics(ROOT, json.loads(sealed.read_text()))
    out = RESULTS / f"{args.round}-metrics.json"
    out.write_text(json.dumps(body, indent=2) + "\n")
    print(
        f"{'agent':15} {'pass':>6} {'agent_min':>9} {'median':>6} {'cost_usd':>8} {'usd/pass':>8} "
        f"{'input':>10} {'output':>8} {'steps':>5} {'tools':>5} {'subagents':>9}"
    )
    for t in body["agents"]:
        cost = "-" if t["cost_usd"] is None else f"{t['cost_usd']:.2f}"
        per_pass = "-" if t["cost_per_pass_usd"] is None else f"{t['cost_per_pass_usd']:.2f}"
        print(
            f"{t['agent']:15} {t['passed']:>2}/{t['trials']:<3} {t['agent_minutes']:>9} "
            f"{t['median_agent_minutes']:>6} {cost:>8} {per_pass:>8} {t['input']:>10} "
            f"{t['output']:>8} {t['steps']:>5} {t['tool_calls']:>5} {t['subagents']:>9}"
        )
    print(f"wrote {out.relative_to(ROOT)}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ctb", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch", help="get native Vis, dirge, kmet and samizdat for linux-amd64")
    run_parser = sub.add_parser("run", help="run the matrix in Harbor")
    run_parser.add_argument("--round", required=True)
    run_parser.add_argument("--agents", default=DEFAULT_AGENTS)
    run_parser.add_argument("--models", default=",".join(MODELS))
    run_parser.add_argument("--tasks", default="all", help="all, easy, hard or names")
    run_parser.add_argument("--parallel", type=int, default=1)
    run_parser.add_argument(
        "--timeout-multiplier", type=float, default=0.25, help="of the 8 h task timeout; 0.25 = 2 h"
    )
    summary_parser = sub.add_parser("summary", help="PASS table of one round")
    summary_parser.add_argument("--round", required=True)
    summary_parser.add_argument("--json")
    seal_parser = sub.add_parser("seal", help="write results/<round>.json once, with hashes")
    seal_parser.add_argument("--round", required=True)
    seal_parser.add_argument("--agents", default=DEFAULT_AGENTS)
    seal_parser.add_argument("--models", default=",".join(MODELS))
    seal_parser.add_argument("--tasks", default="all", help="all, easy, hard or names")
    metrics_parser = sub.add_parser("metrics", help="cost, time and token table of a sealed round")
    metrics_parser.add_argument("--round", required=True)
    args = parser.parse_args(argv)
    commands = {
        "fetch": lambda _: fetch(),
        "run": run,
        "summary": summary,
        "seal": seal,
        "metrics": metrics,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()

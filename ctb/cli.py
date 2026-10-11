"""Fetch agents, run the curated matrix in Harbor and summarize PASS rates."""

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from importlib import metadata
from pathlib import Path

from ctb import credentials, privacy
from ctb.agents import vis
from ctb.agents.base import EFFORT, min_token_seconds
from ctb.matrix import AGENTS, MODELS, ROOT, Trial, load_tasks, select_tasks, trials
from ctb.proxy import Proxy

ARTIFACTS = ROOT / "artifacts"
RESULTS = ROOT / "results"
# The full-suite Vis harness (moved out of the Vis repository) owns the shared artifacts.
SUITE_ARTIFACTS = ROOT / "terminal_bench" / "artifacts"
DATASET = Path(os.environ.get("CTB_DATASET", SUITE_ARTIFACTS / "datasets" / "terminal-bench"))
COMPOSE = SUITE_ARTIFACTS / "docker-compose"
# Pinned master commits. Local patches: kmet print mode honors --thinking, and dirge and
# kmet report token usage.
DIRGE_REPO, DIRGE_COMMIT = "https://github.com/yogthos/dirge.git", "f36b2890"
KMET_REPO, KMET_COMMIT = "https://github.com/kmetia/kmet-agent.git", "58f23e83"
SAMIZDAT_REPO, SAMIZDAT_COMMIT = "https://github.com/yogthos/samizdat.git", "e7889d4a"
# Jolt 0.8.21 moved runtime timer deadlines to the monotonic clock, but ebb (samizdat's task
# library) still schedules on the wall clock, so on 0.8.21 no samizdat run ever starts.
JOLT_VERSION = "v0.8.20"
JOLT_URL = "https://github.com/jolt-lang/jolt/releases/download/{v}/jolt-{v}-x86_64-linux.tar.gz"
# Task containers reach the host proxy here (Podman's name for the host).
PROXY_HOST = "host.containers.internal"
# The newest Vis release with a native Linux x64 bundle.
VIS_RELEASE = "v0.2.37"
BB_MIN = "1.13.225"
SRC = ARTIFACTS / "src"
DIRGE_BUILD = (
    "apt-get update -qq && apt-get install -y -qq musl-tools clang mold >/dev/null && "
    "rustup target add x86_64-unknown-linux-musl >/dev/null && "
    "cargo build --release --target x86_64-unknown-linux-musl"
)
# Paths inside the archive are absolute (jolt caches them), so build at /installed-agent.
SAMIZDAT_BUILD = """set -e
apt-get update -qq && apt-get install -y -qq git ca-certificates >/dev/null
B=/installed-agent/samizdat
mkdir -p $B/home $B/src
tar -xzf /work/jolt.tar.gz -C $B && mv $B/jolt-*-x86_64-linux $B/jolt
cp -r /src/. $B/src && rm -rf $B/src/.git
mkdir -p $B/src/src/ctb && cp /work/drive.clj $B/src/src/ctb/drive.clj
cd $B/src && export HOME=$B/home PATH=$B/jolt:$PATH
jolt path >/dev/null
JOLT_AOT_ASYNC=0 jolt -e "(require 'samizdat.core 'ctb.drive) (System/exit 0)"
tar -czf /out/samizdat-linux-amd64.tar.gz -C /installed-agent samizdat
"""


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


def fetch_dirge() -> Path:
    archive = ARTIFACTS / "dirge-x86_64-unknown-linux-musl.tar.gz"
    if not archive.is_file():
        path = checkout(DIRGE_REPO, DIRGE_COMMIT, "dirge")
        apply_patches(path, "dirge-usage.patch")
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
    archive = ARTIFACTS / "samizdat-linux-amd64.tar.gz"
    if not archive.is_file():
        path = checkout(SAMIZDAT_REPO, SAMIZDAT_COMMIT, "samizdat")
        jolt = download(
            JOLT_URL.format(v=JOLT_VERSION), ARTIFACTS / f"jolt-{JOLT_VERSION}-x86_64-linux.tar.gz"
        )
        with tempfile.TemporaryDirectory(prefix="ctb-sz-", dir=ARTIFACTS) as work:
            work_path = Path(work)
            (work_path / "jolt.tar.gz").write_bytes(jolt.read_bytes())
            (work_path / "drive.clj").write_text(
                (ROOT / "ctb/agents/samizdat_drive.clj").read_text()
            )
            sh(
                [
                    "podman",
                    "run",
                    "--rm",
                    "--memory",
                    "4g",
                    "--platform",
                    "linux/amd64",
                    "-v",
                    f"{path}:/src:ro",
                    "-v",
                    f"{work_path}:/work:ro",
                    "-v",
                    f"{ARTIFACTS}:/out",
                    "docker.io/library/python:3.12-slim-bookworm",
                    "bash",
                    "-c",
                    SAMIZDAT_BUILD,
                ]
            )
    return archive


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
    archive = ARTIFACTS / "kmet-linux-amd64.tar.gz"
    if not archive.is_file():
        path = checkout(KMET_REPO, KMET_COMMIT, "kmet-agent")
        apply_patches(path, "kmet-print-thinking.patch", "kmet-usage.patch")
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
        "--plugin",
        privacy.REDACTOR,
    ]
    if trial.agent.effort_kwarg:
        command += ["--ak", f"reasoning_effort={EFFORT}"]
    return privacy.check_command(command)


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
        with open(logs / f"{trial.job}.log", "w") as log:
            code = subprocess.run(
                harbor_command(trial, jobs, multiplier),
                check=False,
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            ).returncode
    if not podman_ready(env):
        # A VM stop is not an agent result: remove the job, so the next run repeats it.
        shutil.rmtree(jobs / trial.job, ignore_errors=True)
        raise PodmanDown(
            f"Podman stopped during {trial.job}: start the VM, then run the round again"
        )
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
        "jolt": JOLT_VERSION,
        "harbor": metadata.version("harbor"),
        "models": MODELS,
        "effort": EFFORT,
    }


def record_run(jobs: Path, args: argparse.Namespace) -> None:
    """Append the harness state of one `run` call to the round; `seal` copies it."""
    path = jobs / "ctb-runs.json"
    runs = json.loads(path.read_text()) if path.is_file() else []
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
    runs.append(record)
    path.write_text(json.dumps(runs, indent=2) + "\n")


def rows(jobs: Path) -> list[dict]:
    out = []
    for path in sorted(jobs.glob("*/*/result.json")):
        agent, family, task = path.parent.parent.name.split("__")
        result = json.loads(path.read_text())
        agent_result = result.get("agent_result") or {}
        error = (result.get("exception_info") or {}).get("exception_type")
        out.append(
            {
                "agent": agent,
                "model": MODELS[family],
                "task": task,
                "reward": reward_of(result),
                "error": error,
                "input": agent_result.get("n_input_tokens"),
                "output": agent_result.get("n_output_tokens"),
            }
        )
    return out


def summary(args: argparse.Namespace) -> None:
    table = rows(ROOT / "jobs" / args.round)
    levels = {t["name"]: t["level"] for t in load_tasks()}
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in table:
        groups.setdefault((row["agent"], row["model"]), []).append(row)
    print(
        f"{'agent':12} {'model':28} {'pass':>7} {'easy':>6} {'hard':>6} {'errors':>6} {'in_med':>9}"
    )
    for (agent, model), items in sorted(groups.items()):

        def passed(rows_):
            return sum(1 for r in rows_ if (r["reward"] or 0) >= 1)

        easy = [r for r in items if levels.get(r["task"]) == "easy"]
        hard = [r for r in items if levels.get(r["task"]) == "hard"]
        inputs = [r["input"] for r in items if isinstance(r["input"], int)]
        median = int(statistics.median(inputs)) if inputs else "-"
        print(
            f"{agent:12} {model:28} {passed(items):>3}/{len(items):<3} "
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
    missing = [t.job for t in expected if trial_result(jobs, t) is None]
    if missing:
        raise SystemExit(f"{len(missing)} trials have no result: {', '.join(missing)}")
    table = []
    for trial in expected:
        path = min((jobs / trial.job).glob("*/result.json"))
        result = json.loads(path.read_text())
        agent_result = result.get("agent_result") or {}
        table.append(
            {
                "agent": trial.agent.name,
                "model": MODELS[trial.family],
                "task": trial.task,
                "reward": reward_of(result),
                "error": (result.get("exception_info") or {}).get("exception_type"),
                "input": agent_result.get("n_input_tokens"),
                "cache": agent_result.get("n_cache_tokens"),
                "output": agent_result.get("n_output_tokens"),
                "agent_version": (result.get("agent_info") or {}).get("version"),
                "task_checksum": result.get("task_checksum"),
                "started_at": result.get("started_at"),
                "finished_at": result.get("finished_at"),
                "result": str(path.relative_to(ROOT)),
                "result_sha256": file_sha256(path),
            }
        )
    body = {
        "round": args.round,
        "sealed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runs": runs,
        "rows": table,
    }
    body["digest"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    RESULTS.mkdir(exist_ok=True)
    out.write_text(json.dumps(body, indent=2) + "\n")
    print(f"sealed {len(table)} trials in {out.relative_to(ROOT)}, digest {body['digest']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ctb", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch", help="get native Vis, dirge, kmet and samizdat for linux-amd64")
    run_parser = sub.add_parser("run", help="run the matrix in Harbor")
    run_parser.add_argument("--round", required=True)
    run_parser.add_argument("--agents", default=",".join(AGENTS))
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
    seal_parser.add_argument("--agents", default=",".join(AGENTS))
    seal_parser.add_argument("--models", default=",".join(MODELS))
    seal_parser.add_argument("--tasks", default="all", help="all, easy, hard or names")
    args = parser.parse_args(argv)
    commands = {"fetch": lambda _: fetch(), "run": run, "summary": summary, "seal": seal}
    commands[args.command](args)


if __name__ == "__main__":
    main()

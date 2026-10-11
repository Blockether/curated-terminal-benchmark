"""Fetch agents, run the curated matrix in Harbor and summarize PASS rates."""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ctb import credentials
from ctb.agents.base import EFFORT, min_token_seconds
from ctb.matrix import AGENTS, MODELS, ROOT, Trial, load_tasks, select_tasks, trials

ARTIFACTS = ROOT / "artifacts"
# The full-suite Vis harness (moved out of the Vis repository) owns the shared artifacts.
SUITE_ARTIFACTS = ROOT / "terminal_bench" / "artifacts"
DATASET = Path(os.environ.get("CTB_DATASET", SUITE_ARTIFACTS / "datasets" / "terminal-bench"))
COMPOSE = SUITE_ARTIFACTS / "docker-compose"
# Pinned master commits. kmet gets one local patch: print mode honors --thinking.
DIRGE_REPO, DIRGE_COMMIT = "https://github.com/yogthos/dirge.git", "f36b2890"
KMET_REPO, KMET_COMMIT = "https://github.com/kmetia/kmet-agent.git", "58f23e83"
BB_MIN = "1.13.225"
SRC = ARTIFACTS / "src"
DIRGE_BUILD = (
    "apt-get update -qq && apt-get install -y -qq musl-tools clang mold >/dev/null && "
    "rustup target add x86_64-unknown-linux-musl >/dev/null && "
    "cargo build --release --target x86_64-unknown-linux-musl"
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
    with tarfile.open(archive, "w:gz") as out:
        out.add(binary, arcname=name)


def fetch_dirge() -> Path:
    archive = ARTIFACTS / "dirge-x86_64-unknown-linux-musl.tar.gz"
    if not archive.is_file():
        path = checkout(DIRGE_REPO, DIRGE_COMMIT, "dirge")
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


def fetch_kmet() -> Path:
    archive = ARTIFACTS / "kmet-linux-amd64.tar.gz"
    if not archive.is_file():
        path = checkout(KMET_REPO, KMET_COMMIT, "kmet-agent")
        sh(["git", "apply", str(ROOT / "patches/kmet-print-thinking.patch")], cwd=path)
        env = dict(os.environ, PATH=f"{ARTIFACTS / 'tools'}:{os.environ['PATH']}")
        sh(["bb", "dist", "linux-amd64"], cwd=path, env=env)
        pack(path / "target/dist/linux-amd64/kmet", archive, "kmet")
    return archive


def fetch() -> None:
    """Build pinned dirge and kmet masters for linux-amd64; check the Vis bundle.

    kmet needs Babashka >= BB_MIN on PATH or in artifacts/tools; dirge needs Podman.
    """
    ARTIFACTS.mkdir(exist_ok=True)
    for archive in (fetch_dirge(), fetch_kmet()):
        print(f"{archive.name}: {archive.stat().st_size} B")
    vis = ARTIFACTS / "vis-agent-linux-amd64.tar.gz"
    print(
        f"vis bundle {'present' if vis.is_file() else 'MISSING: copy vis-agent-linux-amd64.tar.gz here'}"
    )


def podman_config() -> Path:
    """Harbor parses command output; the Podman compose banner breaks that parse."""
    path = ARTIFACTS / "containers.conf"
    if not path.is_file():
        ARTIFACTS.mkdir(exist_ok=True)
        path.write_text("[engine]\ncompose_warning_logs = false\n")
    return path


def host_env(trial: Trial, secret_dir: Path) -> dict[str, str]:
    """Process environment of one Harbor job; built-in agents read tokens from it."""
    env = dict(os.environ, PYTHONPATH=str(ROOT), CONTAINERS_CONF_OVERRIDE=str(podman_config()))
    if "PODMAN_COMPOSE_PROVIDER" not in env and COMPOSE.is_file():
        env["PODMAN_COMPOSE_PROVIDER"] = str(COMPOSE)
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(key, None)
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
    ]
    if trial.agent.effort_kwarg:
        command += ["--ak", f"reasoning_effort={EFFORT}"]
    return command


def trial_result(jobs: Path, trial: Trial) -> dict | None:
    for path in sorted((jobs / trial.job).glob("*/result.json")):
        return json.loads(path.read_text())
    return None


def run_trial(trial: Trial, jobs: Path, logs: Path, multiplier: float) -> str:
    if trial_result(jobs, trial) is not None:
        return f"skip {trial.job} (done)"
    with tempfile.TemporaryDirectory(prefix="ctb-") as secrets:
        os.chmod(secrets, 0o700)
        try:
            env = host_env(trial, Path(secrets))
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
    jobs.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    selected = trials(
        args.agents.split(","), args.models.split(","), select_tasks(args.tasks, load_tasks())
    )
    print(f"{len(selected)} trials, parallel {args.parallel}, round {args.round}", flush=True)
    with ThreadPoolExecutor(args.parallel) as pool:
        for line in pool.map(lambda t: run_trial(t, jobs, logs, args.timeout_multiplier), selected):
            print(line, flush=True)


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ctb", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch", help="download dirge and kmet; check the Vis bundle")
    run_parser = sub.add_parser("run", help="run the matrix in Harbor")
    run_parser.add_argument("--round", required=True)
    run_parser.add_argument("--agents", default=",".join(AGENTS))
    run_parser.add_argument("--models", default=",".join(MODELS))
    run_parser.add_argument("--tasks", default="all", help="all, easy, hard or names")
    run_parser.add_argument("--parallel", type=int, default=2)
    run_parser.add_argument(
        "--timeout-multiplier", type=float, default=0.25, help="of the 8 h task timeout; 0.25 = 2 h"
    )
    summary_parser = sub.add_parser("summary", help="PASS table of one round")
    summary_parser.add_argument("--round", required=True)
    summary_parser.add_argument("--json")
    args = parser.parse_args(argv)
    {"fetch": lambda _: fetch(), "run": run, "summary": summary}[args.command](args)


if __name__ == "__main__":
    main()

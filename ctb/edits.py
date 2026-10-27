"""Build the `vis-edits` dataset: the Vis edit tasks in Harbor container form.

The Vis evals run these tasks on the host with the Vis agent only. Here every agent
gets the same container and the same file checks. Harbor then writes one ATIF
`trajectory.json` for each trial, so the edits of all agents can be compared.
"""

import argparse
import json
import shutil
from pathlib import Path

from ctb import edit_stats
from ctb.matrix import ROOT

DATASET = ROOT / "datasets" / "vis-edits"
TASKS = ROOT / "tasks-vis-edits.toml"
GRADER = Path(__file__).with_name("edits_grade.py")
# These tasks grade the final answer or Vis tools, not only files.
SKIP = {"find-usages", "ls-source-root-discovery", "py-project-paths"}
# Vis tool names in an instruction would favor Vis.
WORDING = {" read their numbered source and edit them with patch.": " read them and edit them."}

DOCKERFILE = """FROM --platform=linux/amd64 docker.io/library/python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git ripgrep \\
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY workspace/ /app/
RUN git init -q && git add -A \\
 && git -c user.name=ctb -c user.email=ctb@example.com commit -qm "task start"
"""

TEST_SH = """#!/usr/bin/env bash
set -euo pipefail
cd /app
{prepare}exec python3 /tests/grade.py /tests/checks.json /app /logs/verifier
"""
# The Vis grader variables of a source test.sh and their container values.
HOST_VALUES = {
    '"$VIS_HARBOR_WORKSPACE"': "/app",
    '"$VIS_HARBOR_PYTHON"': "python3",
    '"$VIS_HARBOR_TESTS"': "/tests",
}

TASK_TOML = """schema_version = "1.0"

[task]
name = "vis-edits/{name}"

[metadata]
source = "Vis e2e edit task {name}"

[agent]
timeout_sec = 900.0

[verifier]
timeout_sec = 120.0

[environment]
build_timeout_sec = 900.0
cpus = 2
memory_mb = 4096
"""


def instruction(text: str) -> str:
    for old, new in WORDING.items():
        text = text.replace(old, new)
    return text


def prepare(test_sh: Path) -> str:
    """The commands of a source test.sh before its grade, for example a hidden check run."""
    if not test_sh.exists():
        return ""
    commands = []
    for line in test_sh.read_text().splitlines():
        if line.startswith("exec "):
            break
        if not line.strip() or line.startswith(("#", "set ", "cd ")):
            continue
        for old, new in HOST_VALUES.items():
            line = line.replace(old, new)
        commands.append(line + "\n")
    return "".join(commands)


def build_task(source: Path, target: Path) -> None:
    """Write one container task from one Vis edit task."""
    checks = json.loads((source / "tests" / "checks.json").read_text())
    checks = {key: checks[key] for key in ("want", "wantnot") if key in checks}
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source / "environment" / "workspace", target / "environment" / "workspace")
    (target / "environment" / "Dockerfile").write_text(DOCKERFILE)
    (target / "instruction.md").write_text(instruction((source / "instruction.md").read_text()))
    (target / "task.toml").write_text(TASK_TOML.format(name=source.name))
    tests = target / "tests"
    tests.mkdir()
    (tests / "checks.json").write_text(json.dumps(checks, indent=2) + "\n")
    shutil.copy(GRADER, tests / "grade.py")
    (tests / "test.sh").write_text(TEST_SH.format(prepare=prepare(source / "tests" / "test.sh")))
    (tests / "test.sh").chmod(0o755)


def build(source: Path) -> list[str]:
    """Write the dataset and its task list. Return the task names."""
    names = sorted(
        path.name
        for path in source.iterdir()
        if (path / "tests" / "checks.json").is_file() and path.name not in SKIP
    )
    for name in names:
        build_task(source / name, DATASET / name)
    rows = [f'[[task]]\nname = "{name}"\nlevel = "edit"\nhours = 0.1\n' for name in names]
    TASKS.write_text(
        "# The vis-edits tasks. `ctb.edits build` writes this file.\n\n" + "\n".join(rows)
    )
    return names


def table(lines: list[dict]) -> str:
    """A plain text table of summary lines."""
    if not lines:
        return "no finished trials"
    keys = list(lines[0])
    widths = {key: max(len(key), *(len(str(line[key])) for line in lines)) for key in keys}
    rows = [keys] + [[str(line[key]) for key in keys] for line in lines]
    return "\n".join(
        "  ".join(cell.ljust(widths[key]) for key, cell in zip(keys, row)) for row in rows
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ctb.edits")
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build", help="write datasets/vis-edits from Vis edit tasks")
    build_parser.add_argument("--source", type=Path, required=True)
    stats_parser = sub.add_parser("stats", help="print the edit counts of one round")
    stats_parser.add_argument("--round", required=True)
    stats_parser.add_argument("--json", type=Path, help="also write every trial row to this file")
    args = parser.parse_args()
    if args.command == "build":
        print(f"{len(build(args.source))} tasks in {DATASET}")
        return
    rows = edit_stats.trial_rows(ROOT / "jobs" / args.round, DATASET)
    if args.json:
        args.json.write_text(json.dumps(rows, indent=2) + "\n")
    print(table(edit_stats.summary(rows)))


if __name__ == "__main__":
    main()

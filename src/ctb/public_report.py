"""Build a public, closed-schema comparison. Never load transcripts or raw trials."""

import argparse
import hashlib
import html
import json
import math
import re
from datetime import datetime
from pathlib import Path

from ctb.report import render as layout
from ctb.report.comparison import setup_id

#: The sealed rounds of each agent, in order: round 1 first. Each round has every task once.
SELECTION = {
    "codex": tuple(f"gpt-23a3a92/r{n}" for n in range(1, 6)),
    "kmet": tuple(f"kmet-0557d54/r{n}" for n in range(1, 6)),
    "kmet-codemode": tuple(f"kmet-0557d54/r{n}" for n in range(1, 6)),
    "kmet-eval-only": tuple(f"kmet-0557d54/r{n}" for n in range(1, 6)),
    "kmet-shell-only": tuple(f"kmet-0557d54/r{n}" for n in range(1, 6)),
    # Round 4 is a repeat: in gpt-23a3a92/r4 one call lost its usage, so the round had no cost.
    "opencode": (
        *(f"gpt-23a3a92/r{n}" for n in (1, 2, 3)),
        "opencode-0557d54/r1",
        "gpt-23a3a92/r5",
    ),
    "pi": tuple(f"gpt-23a3a92/r{n}" for n in range(1, 6)),
    "samizdat": tuple(f"samizdat-da4c221/r{n}" for n in range(1, 6)),
    "vis-solo": ("gpt-vis-solo-057-43a8048",),
    "wisp": tuple(f"gpt-wisp-5818f6d/r{n}" for n in range(1, 6)),
    "xi": tuple(f"xi-93f57e6/r{n}" for n in range(1, 6)),
}
#: Trials that infrastructure interrupted, repeated alone with the same harness commit:
#: (agent, round, task) -> the round of the repeat. The repeat must have the same setup.
REPLACED = {
    # The provider connection failed (TLS bad_record_mac) and Kmet did not retry.
    ("kmet-eval-only", "kmet-0557d54/r2", "interleaved-vigenere"): "kmet-retry-0557d54/r1",
    # The Codex backend failed (TLS handshake EPROTO or server_is_overloaded) and Xi did not retry.
    ("xi", "xi-93f57e6/r2", "kv-live-surgery"): "xi-retry-93f57e6/r2",
    ("xi", "xi-93f57e6/r2", "photonic-waveguide-routing"): "xi-retry-93f57e6/r1",
    ("xi", "xi-93f57e6/r4", "react-lead-form"): "xi-retry-93f57e6/r3",
    ("xi", "xi-93f57e6/r4", "vf2-speedup-networkx"): "xi-retry-93f57e6/r5",
    ("xi", "xi-93f57e6/r5", "bun-sourcemap-leak"): "xi-retry-93f57e6/r1",
    ("xi", "xi-93f57e6/r5", "kv-live-surgery"): "xi-retry-93f57e6/r3",
    ("xi", "xi-93f57e6/r5", "vf2-speedup-networkx"): "xi-retry-93f57e6/r6",
    # The verifier of the task timed out after its 60 s limit and wrote no result.
    ("samizdat", "samizdat-da4c221/r1", "music-harmony"): "samizdat-retry-da4c221/r1",
}
TASKS = (
    "bun-sourcemap-leak",
    "html-js-filter",
    "interleaved-vigenere",
    "kv-live-surgery",
    "music-harmony",
    "mvcc-lsm-compaction",
    "payments-pipeline-fix",
    "photonic-waveguide-routing",
    "react-lead-form",
    "session-window-debug",
    "vf2-speedup-networkx",
    "wal-recovery-ordering",
)
ERRORS = {
    None,
    "AgentTimeoutError",
    "NonZeroAgentExitCodeError",
    "OpenCodeHangError",
    "RuntimeError",
    "VerifierTimeoutError",
}


def selected():
    """(agent, run, round) for each selected round; run 1 is the first round of the agent."""
    return [(a, n, name) for a, names in SELECTION.items() for n, name in enumerate(names, 1)]


def trial_count():
    return len(selected()) * len(TASKS)


def require(ok):
    if not ok:
        raise ValueError("Public report validation failed")


def keys(value, names):
    require(type(value) is dict and set(value) == set(names))


def sha(value, length=64):
    require(type(value) is str and re.fullmatch("[0-9a-f]{" + str(length) + "}", value))


def digest(body):
    return hashlib.sha256(
        json.dumps({k: v for k, v in body.items() if k != "digest"}, sort_keys=True).encode()
    ).hexdigest()


def read(path):
    require(not any(p.is_symlink() for p in (path, *path.parents)))
    require(path.is_file() and path.stat().st_size <= 2_000_000)

    def unique(pairs):
        result = {}
        for k, v in pairs:
            require(k not in result)
            result[k] = v
        return result

    return json.loads(path.read_text(), object_pairs_hook=unique)


def validate(data):
    keys(data, ("schema", "sources", "rows"))
    require(type(data["schema"]) is int and data["schema"] == 2)
    keys(data["sources"], SELECTION)
    for agent, sources in data["sources"].items():
        require(type(sources) is list and len(sources) == len(SELECTION[agent]))
        for source, name in zip(sources, SELECTION[agent], strict=True):
            keys(
                source,
                (
                    "round",
                    "seal_sha256",
                    "harness_commits",
                    "agent_version",
                    "emulated",
                    "replaced",
                ),
            )
            require(source["round"] == name)
            require(type(source["replaced"]) is list)
            for item in source["replaced"]:
                keys(item, ("task", "round", "seal_sha256"))
                require(REPLACED.get((agent, name, item["task"])) == item["round"])
                sha(item["seal_sha256"])
            version = source["agent_version"]
            require(
                type(version) is str and re.fullmatch("[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}", version)
            )
            sha(source["seal_sha256"])
            commits = source["harness_commits"]
            require(type(commits) is list and commits and commits == sorted(set(commits)))
            for commit in commits:
                sha(commit, 40)
            require(type(source["emulated"]) is bool)
    require(type(data["rows"]) is list and len(data["rows"]) == trial_count())
    seen = set()
    for row in data["rows"]:
        keys(row, ("agent", "run", "task", "passed", "error", "result_sha256"))
        require(type(row["agent"]) is str and row["agent"] in SELECTION)
        require(type(row["run"]) is int and 1 <= row["run"] <= len(SELECTION[row["agent"]]))
        require(type(row["task"]) is str and row["task"] in TASKS)
        require(type(row["passed"]) is bool)
        require(row["error"] is None or type(row["error"]) is str)
        require(row["error"] in ERRORS)
        sha(row["result_sha256"])
        trial = row["agent"], row["run"], row["task"]
        require(trial not in seen)
        seen.add(trial)
    require(seen == {(a, n, t) for a, n, _ in selected() for t in TASKS})
    return data


def agent_runs(seal, agent):
    return [r for r in seal["runs"] if agent in r["agents"].split(",")]


def trial_run(seal, agent, row):
    """The run record of a trial: the last run of its agent that started before the trial."""
    start = datetime.fromisoformat(row["started_at"])
    runs = [r for r in agent_runs(seal, agent) if datetime.fromisoformat(r["started_at"]) <= start]
    require(bool(runs))
    return max(runs, key=lambda r: datetime.fromisoformat(r["started_at"]))


def sealed(root, name):
    seal = read(root / "results" / (name + ".json"))
    require(seal["digest"] == digest(seal) and seal["round"] == name)
    require(bool(seal["runs"]) and all(r["dirty"] is False for r in seal["runs"]))
    return seal


def round_trials(root, agent, name):
    """(seal, row) of each trial of the agent in a selected round, a REPLACED trial from its repeat."""
    seal = sealed(root, name)
    trials = []
    for row in seal["rows"]:
        if row["agent"] != agent:
            continue
        repeat = REPLACED.get((agent, name, row["task"].removeprefix("terminal-bench/")))
        if repeat is None:
            trials.append((seal, row))
            continue
        other = sealed(root, repeat)
        [new] = [r for r in other["rows"] if r["agent"] == agent and r["task"] == row["task"]]
        # Only an interrupted trial is repeated, on the same task, agent and setup.
        require(row["error"] is not None)
        require(new["task_checksum"] == row["task_checksum"])
        require(new["agent_version"] == row["agent_version"])
        require(setup_id(trial_run(other, agent, new)) == setup_id(trial_run(seal, agent, row)))
        trials.append((other, new))
    return seal, trials


def from_seals(root):
    data = {"schema": 2, "sources": {a: [] for a in SELECTION}, "rows": []}
    for agent, run, name in selected():
        seal, trials = round_trials(root, agent, name)
        runs = agent_runs(seal, agent)
        require(bool(runs))
        versions = {r["agent_version"] for r in seal["rows"] if r["agent"] == agent}
        require(len(versions) == 1)
        repeats = [(s, r) for s, r in trials if s is not seal]
        data["sources"][agent].append(
            {
                "round": name,
                "seal_sha256": seal["digest"],
                "harness_commits": sorted({r["commit"] for r in runs}),
                "agent_version": next(iter(versions)),
                # Run records without an engine predate native runs; all of those ran emulated.
                "emulated": any((r.get("engine") or {"emulated": True})["emulated"] for r in runs),
                "replaced": [
                    {
                        "task": r["task"].removeprefix("terminal-bench/"),
                        "round": s["round"],
                        "seal_sha256": s["digest"],
                    }
                    for s, r in repeats
                ],
            }
        )
        for _, row in trials:
            require(row["model"] == "openai/gpt-6.1-sol")
            require(type(row["reward"]) in (int, float, type(None)))
            require(row["reward"] in (None, 0, 1))
            task = row["task"]
            require(task.startswith("terminal-bench/"))
            data["rows"].append(
                {
                    "agent": agent,
                    "run": run,
                    "task": task.removeprefix("terminal-bench/"),
                    "passed": row["reward"] == 1,
                    "error": row["error"],
                    "result_sha256": row["result_sha256"],
                }
            )
    return validate(data)


def encoded(data):
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()


# Only these tool labels can enter the public metrics, never arbitrary log text.
TOOLS = frozenset(
    [
        "__no_call__",
        "__parse_error__",
        "__provider_error__",
        "add",
        "adopt",
        "always",
        "apply_clojure_changes",
        "apply_patch",
        "ask_human",
        "bash",
        "bash_output",
        "battery",
        "branch_theses",
        "buffers",
        "cancel",
        "canvas_review_add_block",
        "canvas_review_add_comment",
        "canvas_review_add_prose",
        "canvas_review_connect",
        "canvas_review_highlight",
        "canvas_review_load",
        "canvas_review_set_plan",
        "cd",
        "cell",
        "cells",
        "clear",
        "clj",
        "clj_deps",
        "clj_extract",
        "clj_fix_declares",
        "clj_fix_parens",
        "clj_mv",
        "clj_outline",
        "clj_rename_ns",
        "clj_replace",
        "clj_topo",
        "clj_tree",
        "codemode",
        "commit",
        "commits",
        "complete",
        "context_edit",
        "debug",
        "diagnosis",
        "diff",
        "difft",
        "disable",
        "doc",
        "done",
        "edit",
        "edit_clojure",
        "edit_file",
        "enable",
        "eval",
        "events",
        "exec",
        "execution",
        "experiment",
        "ext",
        "ext_reload",
        "fetch_artifact",
        "fetch_turn",
        "find",
        "finding",
        "forget",
        "fork",
        "git",
        "git_commit",
        "git_file_diff",
        "git_hunk",
        "git_overview",
        "git_stage_hunks",
        "give_up",
        "glob",
        "grep",
        "help",
        "holds",
        "inference",
        "inspect_clojure",
        "intervene",
        "introspect",
        "jobs",
        "kill",
        "kill_shell",
        "lineage",
        "list",
        "list_dir",
        "list_subagents",
        "load",
        "load_image",
        "login",
        "logout",
        "ls",
        "lsp",
        "manifest",
        "manual",
        "mcp",
        "merge",
        "message",
        "model",
        "new",
        "off",
        "outcome",
        "papercut",
        "patch",
        "plan",
        "policy",
        "processes",
        "project",
        "prompt",
        "propose_cell",
        "ps",
        "python3",
        "python_execution",
        "quit",
        "read",
        "read_digest",
        "read_file",
        "read_minified",
        "read_source",
        "recall",
        "refresh",
        "release",
        "relentless",
        "reload",
        "reload_cells",
        "remember",
        "remove",
        "repo",
        "repo_overview",
        "request_user_input_async",
        "resource",
        "resume",
        "retire",
        "rollover",
        "rules",
        "run_check",
        "run_code",
        "run_task",
        "sane",
        "save",
        "session_search",
        "session_view",
        "sessions",
        "shell",
        "skill",
        "sleep",
        "spawn_subagent",
        "split",
        "staged",
        "stop_subagent",
        "structural",
        "subagent_result",
        "subagents",
        "summary",
        "task",
        "term",
        "thesis",
        "tool_search",
        "transform_clojure",
        "tree",
        "trim",
        "truncate",
        "trust",
        "ui_block",
        "ui_state",
        "ui_update",
        "unstaged",
        "untrust",
        "verdict",
        "view_image",
        "wait",
        "webfetch",
        "websearch",
        "worktree",
        "write",
        "write_file",
        "write_todo_list",
        "xi_events",
        "yes",
        "yolo",
    ]
)
METRIC_NUMBERS = (
    "run_ms",
    "agent_ms",
    "cost",
    "provider_calls",
    "provider_ms",
    "provider_timed",
    "tool_calls",
    "tool_errors",
    "provider_errors",
    "tool_ms",
    "input",
    "cache",
    "output",
)
COUNTS = {
    "provider_calls",
    "provider_timed",
    "tool_calls",
    "tool_errors",
    "provider_errors",
    "input",
    "cache",
    "output",
}


class MissingMetrics(ValueError):
    """Selected trials without a value that the comparison shows."""


def measurement(value, *, count=False):
    require(
        value is None
        or (
            type(value) in ((int,) if count else (int, float))
            and math.isfinite(value)
            and 0 <= value <= 1e15
        )
    )


def validate_metrics(metrics, data):
    keys(metrics, {r["result_sha256"] for r in data["rows"]})
    # The agents compare only when each trial has each value; a gap needs a repeat of the round.
    gaps = [
        f"{row['agent']} round {row['run']} {row['task']}: {', '.join(missing)}"
        for row in data["rows"]
        if type(metrics[row["result_sha256"]]) is dict
        and (missing := [k for k in METRIC_NUMBERS if metrics[row["result_sha256"]].get(k) is None])
    ]
    if gaps:
        raise MissingMetrics("; ".join(gaps))
    for row in data["rows"]:
        m = metrics[row["result_sha256"]]
        keys(
            m,
            (*METRIC_NUMBERS, "tools", "phases", "started_at", "reward", "task_checksum", "setup"),
        )
        sha(m["task_checksum"])
        sha(m["setup"])
        for key in METRIC_NUMBERS:
            measurement(m[key], count=key in COUNTS)
        require(m["provider_timed"] <= m["provider_calls"])
        require(type(m["reward"]) in (int, float, type(None)) and m["reward"] in (None, 0, 1))
        require(row["passed"] == (m["reward"] == 1))
        require(
            type(m["started_at"]) is str and re.fullmatch(r"[0-9T:.+Z-]{19,40}", m["started_at"])
        )
        datetime.fromisoformat(m["started_at"])
        require(type(m["tools"]) is dict and set(m["tools"]) <= TOOLS)
        for tool in m["tools"].values():
            keys(tool, ("calls", "errors", "ms", "timed"))
            for key, value in tool.items():
                require(value is not None)
                measurement(value, count=key != "ms")
            require(tool["errors"] <= tool["calls"] and tool["timed"] <= tool["calls"])
        require(sum(t["calls"] for t in m["tools"].values()) == m["tool_calls"])
        require(m["tool_errors"] == sum(t["errors"] for t in m["tools"].values()))
        require(type(m["phases"]) is list and len(m["phases"]) == 4)
        require({p["name"] for p in m["phases"]} == set(layout.PHASE_NAMES))
        for phase in m["phases"]:
            keys(phase, ("name", "start", "end"))
            for key in ("start", "end"):
                measurement(phase[key])
            require(
                phase["start"] is None or phase["end"] is None or phase["end"] >= phase["start"]
            )
    return metrics


def collect_metrics(root):
    """Read local evidence once; publish only checked numeric fields and known tool names."""
    from ctb.report.trials import load

    data = from_seals(root)
    metrics = {}
    for agent, _, name in selected():
        for seal, row in round_trials(root, agent, name)[1]:
            path = root / row["result"]
            require(path.resolve().is_relative_to((root / "jobs").resolve()))
            require(hashlib.sha256(path.read_bytes()).hexdigest() == row["result_sha256"])
            trial = load(path.parent, root / "jobs")
            facts, summary = layout.facts(trial), trial["summary"]
            setup = setup_id(trial_run(seal, agent, row))
            require(setup is not None)
            metrics[row["result_sha256"]] = {
                "task_checksum": row["task_checksum"],
                "setup": setup,
                **{k: facts[k] for k in ("run_ms", "agent_ms", "cost")},
                **{
                    k: summary[k]
                    for k in (
                        "provider_calls",
                        "provider_ms",
                        "provider_timed",
                        "tool_calls",
                        "tool_errors",
                        "provider_errors",
                        "tool_ms",
                        "tools",
                    )
                },
                "phases": trial["phases"],
                "started_at": trial["started_at"],
                "reward": trial["reward"],
                **dict(zip(("input", "cache", "output"), layout.usage(trial))),
            }
    return validate_metrics(metrics, data)


def checked_metrics(root, data):
    metrics = validate_metrics(read(root / "public/report-metrics.json"), data)
    for agent, _, name in selected():
        for seal, row in round_trials(root, agent, name)[1]:
            m = metrics[row["result_sha256"]]
            require(m["task_checksum"] == row["task_checksum"])
            require(setup_id(trial_run(seal, agent, row)) == m["setup"])
            require(m["reward"] == row["reward"] and m["started_at"] == row["started_at"])
    return metrics


def public_trials(data, metrics):
    validate(data)
    validate_metrics(metrics, data)
    trials = []
    for r in data["rows"]:
        m = metrics[r["result_sha256"]]
        source = data["sources"][r["agent"]][r["run"] - 1]
        trials.append(
            {
                **r,
                "id": r["result_sha256"],
                "task_checksum": m["task_checksum"],
                "setup": m["setup"],
                "model": "openai/gpt-6.1-sol",
                "round": next(
                    (x["round"] for x in source["replaced"] if x["task"] == r["task"]),
                    source["round"],
                ),
                "reward": m["reward"],
                "started_at": m["started_at"],
                "version": source["agent_version"],
                "error_message": None,
                "events": [],
                "verification": [],
                "phases": m["phases"],
                "harbor": {k: m[k] for k in ("input", "cache", "output")},
                "facts": {
                    **{k: m[k] for k in ("run_ms", "agent_ms", "cost")},
                    "outcome": "error" if r["error"] else "passed" if r["passed"] else "failed",
                },
                "summary": {
                    **{k: m[k] for k in METRIC_NUMBERS},
                    "tools": m["tools"],
                    "commands": {},
                },
            }
        )
    return trials


#: Every harness of ctb.matrix: display name, True for a paused one, project page.
#: The publication check runs without Harbor, so it cannot import ctb.matrix.
AGENTS = {
    "vis": ("Vis", False, "https://github.com/Blockether/vis"),
    "codex": ("Codex", False, "https://github.com/openai/codex"),
    "opencode": ("OpenCode", False, "https://github.com/anomalyco/opencode"),
    "pi": ("Pi", False, "https://github.com/earendil-works/pi"),
    "kmet": ("Kmet", False, "https://github.com/kmetia/kmet-agent"),
    "xi": ("Xi", False, "https://xi-agent.com/"),
    # Experiment arms: the same Kmet with one tool selected by `--tools`.
    "kmet-eval-only": ("Kmet eval-only", False, "https://github.com/kmetia/kmet-agent"),
    "kmet-codemode": ("Kmet codemode", False, "https://github.com/kmetia/kmet-agent"),
    "kmet-shell-only": ("Kmet shell-only", False, "https://github.com/kmetia/kmet-agent"),
    "wisp": ("Wisp", False, "https://github.com/xificurC/wisp"),
    "samizdat": ("Samizdat", False, "https://github.com/samizdat-code/samizdat"),
}
#: The public address of the site, and the site name in titles and link previews.
SITE_URL = "https://blockether.github.io/curated-terminal-benchmark/"
SITE_NAME = "Curated Terminal-Bench 4.0"
TITLE = SITE_NAME + ": coding agent benchmark | Blockether"
BLOCKETHER = "https://blockether.com/"
ORGANIZATION = BLOCKETHER + "#organization"
#: The link preview image of blockether.com (src/root.tsx).
PREVIEW_IMAGE = BLOCKETHER + "og-image.png"
#: The sources that the page names: label and address.
LINKS = (
    ("Terminal-Bench 4.0", "https://tbench.ai/news/terminal-bench-4-0"),
    (
        "Harbor Hub tasks",
        "https://hub.harborframework.com/datasets/terminal-bench/terminal-bench/4",
    ),
    ("Harbor", "https://www.harborframework.com/"),
    ("GitHub", "https://github.com/Blockether/curated-terminal-benchmark"),
)
MACHINE = "a MacBook Pro M4 Max (Podman VM, 12 CPUs, 24 GiB)"
EMULATION = f"Run under Rosetta emulation on {MACHINE}. Times include the emulation overhead."
NATIVE = f"Run natively (arm64) on {MACHINE}."
RERUN = "* To be run again natively."
FONT_FILES = ("inter-latin.woff2", "jetbrains-mono-latin.woff2")


def agent_key(agent):
    """The AGENTS entry of an agent: its own entry, else the program (vis-solo is vis)."""
    return agent if agent in AGENTS else agent.split("-")[0]


def agent_names():
    """The display names of the selected programs, in the order of AGENTS."""
    chosen = {name.split("-")[0] for name in SELECTION}
    return [label for key, (label, _, _) in AGENTS.items() if key in chosen]


def description():
    names = agent_names()
    return (
        f"{', '.join(names[:-1])} and {names[-1]} on {len(TASKS)} CPU-only Terminal-Bench 4.0 "
        "tasks with GPT-6.1-sol: pass rates, cost, tokens, run time and sanitized transcripts."
    )


def head(data):
    """The page head of the sealed results."""
    # A sealed version can carry the program name ("vis-agent 0.5.7"); the list shows the name.
    versions = {
        agent_key(a): s[-1]["agent_version"].split()[-1] for a, s in data["sources"].items()
    }
    # An agent with an emulated round gets a mark: its rounds will run again natively.
    rerun = {agent_key(a) for a, s in data["sources"].items() if any(r["emulated"] for r in s)}
    note = NATIVE + (f" {RERUN}" if rerun else "")
    return head_html(versions, len(TASKS), "GPT-6.1-sol with medium reasoning", note, rerun)


def local_head(trials):
    """The page head of a local report, from the trials and the run records of their rounds."""
    trials = layout.reported(trials)
    versions, records = {}, []
    for t in sorted(trials, key=lambda t: t.get("started_at") or ""):
        key = agent_key(t["agent"])
        if key in AGENTS and t.get("version"):
            versions[key] = t["version"].split()[-1]
    for root in sorted({Path(t["path"]).parents[1] for t in trials if t.get("path")}):
        manifest = root / "ctb-runs.json"
        records += json.loads(manifest.read_text()) if manifest.is_file() else []
    models = sorted({t["model"].rsplit("/", 1)[-1] for t in trials if t.get("model")})
    model = ", ".join(m.replace("gpt-", "GPT-", 1) for m in models) or "one model"
    efforts = sorted({r["pins"]["effort"] for r in records if r.get("pins", {}).get("effort")})
    if len(efforts) == 1:
        model += f" with {efforts[0]} reasoning"
    engines = [r.get("engine") or {} for r in records]
    note = EMULATION if any(e.get("emulated") for e in engines) else NATIVE if engines else ""
    return head_html(versions, len({t["task"] for t in trials}), model, note)


def head_html(versions, tasks, model, note, rerun=()):
    """The page head: one lead line, then the tested versions, the paused agents and the sources."""

    def agent(key):
        label, _, url = AGENTS[key]
        version = (
            f' <span class="version">({layout.esc(versions[key])})</span>'
            if key in versions
            else ""
        )
        mark = "*" if key in rerun else ""
        return f'<li><a href="{url}">{label}</a>{mark}{version}</li>'

    rows = (
        ("Tested", "".join(agent(key) for key in AGENTS if key in versions)),
        ("Paused", "".join(agent(key) for key, (_, paused, _) in AGENTS.items() if paused)),
        (
            "Sources",
            "".join(f'<li><a href="{url}">{label}{layout.ARROW}</a></li>' for label, url in LINKS),
        ),
    )
    facts = "".join(
        f"<div><dt>{name}</dt><dd><ul>{items}</ul></dd></div>" for name, items in rows if items
    )
    tail = f"{tasks} CPU-only Terminal-Bench 4.0 tasks, all on {layout.esc(model)}."
    programs = len({key.split("-")[0] for key in versions})
    intro = (
        f"Our agent Vis vs. {programs - 1} other coding agents on {tail}"
        if "vis" in versions
        else f"{programs} coding agents on {tail}"
    )
    extra = f'<p class="head-note">{note}</p>\n' if note else ""
    return (
        f'<section class="head">\n<h1>{SITE_NAME}</h1>\n<p class="lead">{intro}</p>\n'
        f'<dl class="head-facts">{facts}</dl>\n{extra}</section>'
    )


def structured_data(path, title, summary):
    """Schema.org data that joins the page to the Blockether organization of blockether.com."""
    url = SITE_URL + path
    graph = [
        {
            "@type": "Organization",
            "@id": ORGANIZATION,
            "name": "Blockether",
            "legalName": "BLOCKETHER sp. z o.o.",
            "url": BLOCKETHER,
            "logo": BLOCKETHER + "icon-512.png",
            "sameAs": [
                "https://www.linkedin.com/company/blockether",
                "https://github.com/Blockether",
            ],
        },
        {
            "@type": "WebSite",
            "@id": SITE_URL + "#website",
            "url": SITE_URL,
            "name": SITE_NAME,
            "inLanguage": "en",
            "publisher": {"@id": ORGANIZATION},
        },
        {
            "@type": "WebPage",
            "@id": url,
            "url": url,
            "name": title,
            "description": summary,
            "inLanguage": "en",
            "isPartOf": {"@id": SITE_URL + "#website"},
            "publisher": {"@id": ORGANIZATION},
        },
    ]
    if not path:
        graph.append(
            {
                "@type": "Dataset",
                "@id": SITE_URL + "#dataset",
                "name": SITE_NAME + " results",
                "description": summary,
                "url": SITE_URL,
                "creator": {"@id": ORGANIZATION},
                "publisher": {"@id": ORGANIZATION},
                "isBasedOn": LINKS[0][1],
                "isAccessibleForFree": True,
                "keywords": [
                    "Terminal-Bench",
                    "coding agents",
                    "AI agent benchmark",
                    *agent_names(),
                ],
                "distribution": [
                    {
                        "@type": "DataDownload",
                        "encodingFormat": "application/json",
                        "contentUrl": SITE_URL + name,
                    }
                    for name in ("comparison.json", "report-metrics.json")
                ],
            }
        )
    raw = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False)
    return raw.replace("<", "\\u003c")


def search_tags(text, path):
    """Search and link preview tags. A page without a path is a viewer shell: it stays out of the index."""
    if path is None:
        return '\n<meta name="robots" content="noindex, follow">'
    title = re.search(r"<title>(.*?)</title>", text)[1]
    summary = re.search(r'<meta name="description" content="([^"]*)">', text)[1]
    url = layout.esc(SITE_URL + path)
    tags = (
        f'<link rel="canonical" href="{url}">',
        '<meta name="author" content="Blockether">',
        '<meta name="theme-color" content="#faf3eb">',
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="Blockether">',
        f'<meta property="og:title" content="{title}">',
        f'<meta property="og:description" content="{summary}">',
        f'<meta property="og:url" content="{url}">',
        f'<meta property="og:image" content="{PREVIEW_IMAGE}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        '<meta property="og:image:alt" content="Blockether logo">',
        '<meta property="og:locale" content="en_US">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{title}">',
        f'<meta name="twitter:description" content="{summary}">',
        f'<meta name="twitter:image" content="{PREVIEW_IMAGE}">',
        '<script type="application/ld+json">'
        + structured_data(path, html.unescape(title), html.unescape(summary))
        + "</script>",
    )
    return "\n" + "\n".join(tags)


def sitemap(paths):
    urls = "".join(f"<url><loc>{layout.esc(SITE_URL + path)}</loc></url>\n" for path in paths)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + urls + "</urlset>\n"
    ).encode()


def public_page(text, path=None):
    """Keep the original layout. Use only local assets and inert transcript text.

    path is the page address under SITE_URL ("" for the index); None keeps the page out of search.
    """
    if path:
        text = re.sub(r"<title>(.*?)</title>", rf"<title>\1 | {SITE_NAME}</title>", text, count=1)
    up = "../" if 'href="../report.css"' in text else ""
    text = re.sub(r'<link rel="preconnect" [^>]*>\n', "", text)
    # Preload the fonts: the browser finds them in fonts.css only after it parses that file.
    text = text.replace(
        f'<link rel="stylesheet" href="{layout.FONTS}">',
        "".join(
            f'<link rel="preload" href="{up}{name}" as="font" type="font/woff2" crossorigin>\n'
            for name in FONT_FILES
        )
        + f'<link rel="stylesheet" href="{up}fonts.css">',
    )
    policy = "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'self'"
    return version_assets(
        text.replace(
            '<meta charset="utf-8">',
            '<meta charset="utf-8">\n<meta http-equiv="Content-Security-Policy" content="'
            + policy
            + '">'
            + search_tags(text, path),
        )
    )


def source_assets():
    result = {name: (layout.STATIC / name).read_bytes() for name in layout.ASSETS}
    static = Path(__file__).parent / "publication_static"
    for name in (
        "fonts.css",
        "inter-latin.woff2",
        "jetbrains-mono-latin.woff2",
        "font-licenses.txt",
    ):
        result[name] = (static / name).read_bytes()
    return result


def asset_revision():
    """Invalidate every dependent URL when any approved UI asset changes."""
    digest = hashlib.sha256()
    raw = source_assets()
    raw["viewer.js"] = (Path(__file__).parent / "publication_static/viewer.js").read_bytes()
    for name, content in sorted(raw.items()):
        digest.update(name.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()[:20]


def version_assets(text):
    names = (*source_assets(), "viewer.js")
    pattern = (
        r"(?P<name>"
        + "|".join(re.escape(n) for n in names)
        + r")(?P<query>\?v=[0-9a-f]+)?(?=[\"')])"
    )
    revision = asset_revision()
    return re.sub(pattern, lambda m: m["name"] + "?v=" + revision, text)


def assets():
    return {
        name: version_assets(raw.decode()).encode() if name.endswith(".css") else raw
        for name, raw in source_assets().items()
    }


def render(data, metrics, *, transcripts=False):
    text = layout.index(
        public_trials(data, metrics),
        "from sealed results",
        head=head(data),
        title=TITLE,
        description=description(),
    )
    if not transcripts:
        text = re.sub(r'<a href="trial/[0-9a-f]{64}\.html">([^<]+)</a>', r"\1", text)
    return public_page(text, "").encode()


def expected(data, metrics):
    return {
        "index.html": render(data, metrics),
        "comparison.json": encoded(data),
        "report-metrics.json": encoded(metrics),
        **assets(),
    }


def check_output(out, data, metrics):
    require(not any(p.is_symlink() for p in (out, *out.parents)))
    files = list(out.rglob("*"))
    require({p.relative_to(out).as_posix() for p in files} == set(expected(data, metrics)))
    for name, raw in expected(data, metrics).items():
        path = out / name
        require(path.is_file() and not path.is_symlink() and path.read_bytes() == raw)


def build(root, out, check=False):
    data = validate(read(root / "public/comparison.json"))
    require(data == from_seals(root))
    metrics = checked_metrics(root, data)
    require(not any(p.is_symlink() for p in (out, *out.parents)))
    if not check:
        # Refuse a reused or contaminated output rather than upload stale content.
        require(not out.exists())
        out.mkdir(parents=True)
        for name, raw in expected(data, metrics).items():
            (out / name).write_bytes(raw)
    check_output(out, data, metrics)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        build(args.root, args.out, args.check)
    except MissingMetrics as error:
        raise SystemExit(f"Public report refused: trials without metrics: {error}") from None
    except (ValueError, KeyError, TypeError, OSError):
        raise SystemExit("Public report refused. No artifact may be uploaded.") from None
    print(f"Public report checked: {trial_count()} trials; exact report assets and metrics.")


if __name__ == "__main__":
    main()

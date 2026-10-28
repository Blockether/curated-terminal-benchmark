"""Find the Harbor trials under jobs/ and turn each into one report trial."""

import json
import os
import re
from pathlib import Path

from ctb.metrics import PHASES, trial_metrics
from ctb.report.transcripts import READERS, epoch_ms, event, harness, instruction


def find(jobs: Path) -> list[Path]:
    """Every trial directory: it holds result.json and agent/. Skips agent and verifier trees."""
    found = []
    for directory, names, files in os.walk(jobs):
        if "result.json" in files and "agent" in names:
            found.append(Path(directory))
            names.clear()
        elif Path(directory).name in ("agent", "verifier", "artifacts"):
            names.clear()
    return sorted(found)


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def clean_version(text: str | None) -> str | None:
    """The last line of a version output. Harbor also keeps stderr, such as a Podman banner."""
    lines = [line.strip() for line in ANSI.sub("", text or "").splitlines() if line.strip()]
    return lines[-1] if lines else None


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")


def summarize(events: list[dict]) -> dict:
    """Counts, times and costs of the events. A total covers only the events that record it."""
    calls = [e for e in events if e["kind"] == "call"]
    timed = [e["ms"] for e in calls if e["ms"] is not None]
    priced = [e["cost"] for e in calls if e["cost"] is not None]

    def group(kind: str) -> dict:
        by_name: dict[str, dict] = {}
        for e in events:
            if e["kind"] != kind:
                continue
            row = by_name.setdefault(
                e["name"] or "unnamed", {"calls": 0, "errors": 0, "ms": 0.0, "timed": 0}
            )
            row["calls"] += 1
            row["errors"] += 1 if e["error"] else 0
            if e["ms"] is not None:
                row["ms"] += e["ms"]
                row["timed"] += 1
        return dict(sorted(by_name.items(), key=lambda item: (-item[1]["calls"], item[0])))

    tools, commands = group("tool"), group("command")
    return {
        "provider_calls": len(calls),
        "provider_ms": sum(timed) if timed else None,
        "provider_timed": len(timed),
        "provider_cost": sum(priced) if priced else None,
        "provider_priced": len(priced),
        "provider_cost_sources": sorted({e["cost_source"] for e in calls if e["cost_source"]}),
        "provider_errors": sum(1 for e in calls if e["error"]),
        "input": sum(e["tokens"]["input"] for e in calls if e["tokens"]),
        "cache": sum(e["tokens"]["cache"] for e in calls if e["tokens"]),
        "output": sum(e["tokens"]["output"] for e in calls if e["tokens"]),
        "tools": tools,
        "tool_calls": sum(t["calls"] for t in tools.values()),
        # None when the harness marks no tool event as failed or not failed.
        "tool_errors": sum(t["errors"] for t in tools.values())
        if any(e["error"] is not None for e in events if e["kind"] == "tool") or not tools
        else None,
        "tool_ms": sum(t["ms"] for t in tools.values())
        if any(t["timed"] for t in tools.values())
        else None,
        "commands": commands,
        "command_calls": sum(t["calls"] for t in commands.values()),
        "command_errors": sum(t["errors"] for t in commands.values()),
    }


def load(trial: Path, jobs: Path) -> dict:
    """One trial: Harbor facts, its transcript events and their summary."""
    result_path = trial / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    info = result.get("agent_info") or {}
    model = ((result.get("config") or {}).get("agent") or {}).get("model_name") or (
        (info.get("model_info") or {}).get("name") or ""
    )
    parts = trial.relative_to(jobs).parts
    round_ = parts[0] if parts[0] != "suite" else f"suite/{parts[2]}"
    key = harness(info.get("name") or "")
    events, notes = READERS[key](trial / "agent", model) if key else ([], [])
    if not events:
        notes.append("No transcript was found for this trial.")
    tools = [e for e in events if e["kind"] == "tool"]
    if tools and all(e["error"] is None for e in tools):
        notes.append("This harness does not mark tool errors, so tool errors are not recorded.")
    if not any(e["kind"] == "user" for e in events):
        prompt = instruction(trial, result)
        if prompt:
            events.insert(0, event("user", text=prompt))
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    exception = result.get("exception_info") or {}
    return {
        "id": slug(f"{round_}-{result.get('trial_name') or trial.name}"),
        "round": round_,
        "agent": info.get("name") or "unknown",
        "harness": key,
        "version": clean_version(info.get("version")),
        "model": model,
        "task": result.get("task_name") or trial.name,
        "reward": reward,
        "passed": reward is not None and reward >= 1,
        "error": exception.get("exception_type"),
        "error_message": exception.get("exception_message"),
        "started_at": result.get("started_at"),
        "path": str(trial),
        "harbor": trial_metrics(result_path),
        "phases": [
            {
                "name": name,
                "start": epoch_ms((result.get(name) or {}).get("started_at")),
                "end": epoch_ms((result.get(name) or {}).get("finished_at")),
            }
            for name in PHASES
        ],
        "events": events,
        "notes": notes,
        "summary": summarize(events),
    }

"""Matched task cohorts. Never select a best retry or turn missing measurements into zero."""

import hashlib
import json
import math
from collections import defaultdict


def cohorts(trials, *, strict=True, basis="solved"):
    if basis not in ("solved", "attempted"):
        raise ValueError("Unknown comparison basis")
    groups = defaultdict(list)
    for trial in trials:
        groups[(trial.get("model"), trial.get("setup") if strict else "observed")].append(trial)
    result = []
    for (model, setup), group in sorted(groups.items(), key=lambda item: str(item[0])):
        agents = sorted({t["agent"] for t in group})
        by_task = defaultdict(lambda: defaultdict(list))
        for trial in group:
            by_task[trial["task"]][trial["agent"]].append(trial)
        matched, ambiguous, mismatched, excluded = [], [], [], []
        for task, cells in sorted(by_task.items()):
            if any(len(v) != 1 for v in cells.values()):
                ambiguous.append(task)
            elif not model or setup is None or len(agents) < 2 or set(cells) != set(agents):
                excluded.append(task)
            elif len({v[0].get("task_checksum") for v in cells.values()}) != 1 or any(
                not v[0].get("task_checksum") for v in cells.values()
            ):
                mismatched.append(task)
            else:
                matched.append(task)
        solved = [task for task in matched if all(by_task[task][a][0]["passed"] for a in agents)]
        metric_tasks = {}
        for metric in ("cost", "agent_ms", "run_ms"):
            metric_tasks[metric] = [
                task
                for task in (solved if basis == "solved" else matched)
                if all(
                    type(by_task[task][a][0]["facts"].get(metric)) in (int, float)
                    and math.isfinite(by_task[task][a][0]["facts"][metric])
                    and by_task[task][a][0]["facts"][metric] >= 0
                    for a in agents
                )
            ]
        rows = {}
        for agent in agents:
            rows[agent] = {"passed": sum(by_task[t][agent][0]["passed"] for t in matched)}
            for metric, tasks in metric_tasks.items():
                rows[agent][metric] = (
                    (sum(by_task[t][agent][0]["facts"][metric] for t in tasks) / len(tasks))
                    if tasks
                    else None
                )
                # Each task has equal weight. A per-task ratio removes task difficulty from
                # an aggregate efficiency comparison. Zero baselines have no finite ratio.
                ratios = []
                for task in tasks:
                    baseline = min(by_task[task][a][0]["facts"][metric] for a in agents)
                    if baseline > 0:
                        ratios.append(
                            math.log(by_task[task][agent][0]["facts"][metric]) - math.log(baseline)
                        )
                rows[agent][metric + "_ratio"] = (
                    math.exp(sum(ratios) / len(ratios))
                    if ratios and sum(ratios) / len(ratios) < 709
                    else None
                )
                rows[agent][metric + "_ratio_tasks"] = len(ratios)
        result.append(
            {
                "model": model,
                "setup": setup,
                "setups": sorted({t.get("setup") or "unknown" for t in group}),
                "strict": strict,
                "basis": basis,
                "agents": agents,
                "tasks": matched,
                "solved": solved,
                "metric_tasks": metric_tasks,
                "rows": rows,
                "ambiguous": ambiguous,
                "mismatched": mismatched,
                "excluded": excluded,
            }
        )
    return result


def setup_id(run):
    """A conservative match: code, shared Python, task fixes, parallelism and timeout."""
    required = ("commit", "pins", "parallel", "timeout_multiplier")
    if not all(k in run for k in required) or run.get("dirty") is not False:
        return None
    pins = run["pins"]
    if not isinstance(pins, dict) or not all(
        pins.get(k) for k in ("python_toolkit", "harbor", "effort")
    ):
        return None
    python = pins["python_toolkit"]
    if not isinstance(python, dict) or not all(
        python.get(k) for k in ("cpython", "requirements_sha256")
    ):
        return None
    if not isinstance(pins.get("task_fixes"), list) or not any(
        k.startswith("ctb-python-") for k in run.get("artifacts", {})
    ):
        return None
    body = {
        "commit": run["commit"],
        "python": pins.get("python_toolkit"),
        "harbor": pins.get("harbor"),
        "effort": pins.get("effort"),
        "fixes": pins.get("task_fixes"),
        "parallel": run["parallel"],
        "timeout": run["timeout_multiplier"],
        "toolkit_archives": {
            k: v for k, v in run.get("artifacts", {}).items() if k.startswith("ctb-python-")
        },
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def selected_cohorts(trials, agents, *, strict=True, basis="solved"):
    """Return only complete groups for the explicit selection, never a smaller substitute."""
    selected = set(agents)
    if len(selected) < 2:
        return []
    return [
        group
        for group in cohorts(
            [t for t in trials if t["agent"] in selected], strict=strict, basis=basis
        )
        if set(group["agents"]) == selected and group["model"] and group["setup"]
    ]

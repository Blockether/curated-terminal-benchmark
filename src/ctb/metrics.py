"""Cost, time, token, step, tool and subagent metrics of the trials of a sealed round."""

import hashlib
import json
import re
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

#: Tools that start a subagent: Codex spawn_agent and OpenCode task.
SUBAGENT_TOOLS = frozenset({"spawn_agent", "task"})
#: API list prices in USD per token: input, cache read and output (litellm, 2026-10-16).
#: The trials run on subscriptions, so a cost is the API equivalent, not a bill.
PRICES = {
    "gpt-6.1-sol": (2e-06, 1e-07, 1e-05),
}
PHASES = ("environment_setup", "agent_setup", "agent_execution", "verifier")


def seconds(timing: dict | None) -> float | None:
    """The length of one Harbor timing entry, or None when it did not finish."""
    if not timing or not timing.get("started_at") or not timing.get("finished_at"):
        return None
    start = datetime.fromisoformat(timing["started_at"])
    end = datetime.fromisoformat(timing["finished_at"])
    return round((end - start).total_seconds(), 1)


def list_price(model: str, input_: int, cache: int, output: int) -> float | None:
    """The API list price of one trial; input includes the cache reads."""
    prices = PRICES.get(model.rpartition("/")[2])
    if prices is None:
        return None
    fresh, read, out = prices
    return (input_ - cache) * fresh + cache * read + output * out


#: A Vis code block that starts subagents, and the child ids in its printed records.
COUNCIL_RUN = re.compile(r"\bcouncil\.run\(")
CHILD_ID = re.compile(r"""['"]session_id['"]: ['"]([0-9a-f-]{36})['"]""")


def council_children(step: dict) -> set[str]:
    """The child session ids that one Vis step started with council.run."""
    codes = [
        str((call.get("arguments") or {}).get("code") or "")
        for call in step.get("tool_calls") or []
    ]
    if not any(COUNCIL_RUN.search(code) for code in codes):
        return set()
    return set(CHILD_ID.findall(json.dumps(step.get("observation") or {})))


def trajectory_counts(path: Path) -> dict:
    """Agent steps, model calls, tool calls by name and subagent starts of an ATIF trajectory.

    Vis starts subagents inside a code block, so its count is the distinct council.run children.
    """
    steps = calls = 0
    tools: Counter[str] = Counter()
    children: set[str] = set()
    if path.is_file():
        for step in json.loads(path.read_text()).get("steps", []):
            if step.get("source") == "agent":
                steps += 1
                calls += step.get("llm_call_count") or 0
            for call in step.get("tool_calls") or []:
                tools[call.get("function_name") or "?"] += 1
            children |= council_children(step)
    return {
        "steps": steps,
        "llm_calls": calls,
        "tool_calls": sum(tools.values()),
        "tools": dict(tools.most_common()),
        "subagents": sum(n for name, n in tools.items() if name in SUBAGENT_TOOLS) + len(children),
    }


def trial_metrics(result_path: Path) -> dict:
    """Metrics of one Harbor trial from its result.json and agent/trajectory.json."""
    result = json.loads(result_path.read_text())
    usage = result.get("agent_result") or {}
    model = (result.get("config", {}).get("agent") or {}).get("model_name") or ""
    input_ = usage.get("n_input_tokens") or 0
    cache = usage.get("n_cache_tokens") or 0
    output = usage.get("n_output_tokens") or 0
    cost, source = usage.get("cost_usd"), "agent"
    if cost is None:
        cost, source = list_price(model, input_, cache, output), "list price"
    times = {phase: seconds(result.get(phase)) for phase in PHASES}
    times["total"] = seconds(result)
    return {
        "seconds": times,
        "input": input_,
        "cache": cache,
        "output": output,
        "cost_usd": None if cost is None else round(cost, 4),
        "cost_source": source if cost is not None else None,
        **trajectory_counts(result_path.parent / "agent" / "trajectory.json"),
    }


def round_metrics(root: Path, sealed: dict) -> dict:
    """One metrics row per sealed row, checked against the sealed result hash, and agent totals."""
    rows = []
    for row in sealed["rows"]:
        path = root / row["result"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["result_sha256"]:
            raise ValueError(f"{row['result']} changed after the seal")
        rows.append(
            {
                "agent": row["agent"],
                "model": row["model"],
                "agent_version": row.get("agent_version"),
                "task": row["task"],
                "reward": row["reward"],
                "error": row["error"],
                "result_sha256": row["result_sha256"],
                **trial_metrics(path),
            }
        )
    return {
        "round": sealed["round"],
        "digest": sealed["digest"],
        "rows": rows,
        "agents": totals(rows),
    }


def totals(rows: list[dict]) -> list[dict]:
    """Pass count, time, cost, tokens, steps, tools and subagents of each agent and model."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((row["agent"], row["model"]), []).append(row)
    out = []
    for (agent, model), items in sorted(groups.items()):
        passed = sum(1 for r in items if (r["reward"] or 0) >= 1)
        agent_s = [r["seconds"]["agent_execution"] or 0 for r in items]
        costs = [r["cost_usd"] for r in items if r["cost_usd"] is not None]
        cost = round(sum(costs), 2) if len(costs) == len(items) else None
        out.append(
            {
                "agent": agent,
                "model": model,
                "trials": len(items),
                "agent_versions": sorted({str(r.get("agent_version")) for r in items}),
                "passed": passed,
                "agent_minutes": round(sum(agent_s) / 60, 1),
                "median_agent_minutes": round(statistics.median(agent_s) / 60, 1),
                "total_minutes": round(sum(r["seconds"]["total"] or 0 for r in items) / 60, 1),
                "cost_usd": cost,
                "cost_per_pass_usd": round(cost / passed, 2)
                if cost is not None and passed
                else None,
                "input": sum(r["input"] for r in items),
                "cache": sum(r["cache"] for r in items),
                "output": sum(r["output"] for r in items),
                "steps": sum(r["steps"] for r in items),
                "tool_calls": sum(r["tool_calls"] for r in items),
                "subagents": sum(r["subagents"] for r in items),
            }
        )
    return out

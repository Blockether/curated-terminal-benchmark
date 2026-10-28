"""Write the report trials as a static HTML site: index.html and one page for each trial.

The pages work without JavaScript. report.js adds filters, sorting and the URL state.
A value that a harness does not record shows as "not recorded", never as zero.
"""

import html
import os
import shutil
import statistics
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from itertools import repeat
from pathlib import Path

from ctb.report.trials import find, load

STATIC = Path(__file__).parent / "static"
ASSETS = ("tokens.css", "base.css", "report.css", "report.js", "logo.svg")
FONTS = (
    "https://fonts.googleapis.com/css2?family=Inter:wght@400..900"
    "&family=JetBrains+Mono:wght@400;500&display=swap"
)
#: The text length above which an input or output starts closed.
OPEN_LIMIT = 1_200
#: The number of columns in the activity strip of a trial.
BINS = 72
KINDS = {
    "user": "Prompt",
    "assistant": "Assistant",
    "reasoning": "Reasoning",
    "call": "Provider call",
    "tool": "Tool",
    "command": "Command",
    "result": "Run result",
}
PHASE_NAMES = {
    "environment_setup": "Environment setup",
    "agent_setup": "Agent setup",
    "agent_execution": "Agent execution",
    "verifier": "Verifier",
}
SHOW = (
    ("all", "All"),
    ("messages", "Messages"),
    ("tools", "Tools and commands"),
    ("calls", "Provider calls"),
    ("errors", "Errors"),
)
NONE = '<span class="none">not recorded</span>'


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def esc_text(value: object) -> str:
    """Escape for a text node only, where quotes need no escape: JSON stays readable and short."""
    return html.escape(str(value), quote=False)


def duration(ms: float | None) -> str | None:
    """A length of time for a person: 850 ms, 12.3 s, 4 min 05 s or 1 h 02 min."""
    if ms is None:
        return None
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    total = round(ms / 1000)
    if total < 3600:
        return f"{total // 60} min {total % 60:02d} s"
    return f"{total // 3600} h {total % 3600 // 60:02d} min"


def money(usd: float | None) -> str | None:
    if usd is None:
        return None
    return f"${usd:.4f}" if usd < 1 else f"${usd:,.2f}"


def number(value: float | None) -> str | None:
    return None if value is None else f"{value:,.0f}"


def percent(part: int | None, whole: int) -> str | None:
    if part is None or not whole:
        return None
    return "<1%" if 0 < part / whole < 0.01 else f"{100 * part / whole:.0f}%"


def stamp(iso: str | None) -> str | None:
    """An ISO time as 2026-10-16 10:00 UTC."""
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def shown(text: str | None) -> str:
    return NONE if text is None else esc(text)


def td(text: str | None, sort: object = None, cls: str = "num", title: str | None = None) -> str:
    """One table cell. `sort` is the value that report.js sorts on; text cells sort on text."""
    attrs = f' class="{cls}"' if cls else ""
    if sort is not None:
        attrs += f' data-v="{esc(sort)}"'
    if title:
        attrs += f' title="{esc(title)}"'
    return f"<td{attrs}>{shown(text)}</td>"


def th(label: str, kind: str = "num", sortable: bool = True) -> str:
    cls = ' class="num"' if kind == "num" else ""
    if not sortable:
        return f'<th scope="col"{cls}>{esc(label)}</th>'
    return (
        f'<th scope="col"{cls} data-type="{kind}"><button type="button" class="sort">'
        f"{esc(label)}</button></th>"
    )


def marked_errors(trials: list[dict]) -> tuple[int | None, int]:
    """Tool errors of the trials whose harness marks them, and the tool calls of those trials."""
    marked = [t["summary"] for t in trials if t["summary"]["tool_errors"] is not None]
    if not marked:
        return None, 0
    return sum(s["tool_errors"] for s in marked), sum(s["tool_calls"] for s in marked)


def facts(trial: dict) -> dict:
    """The derived values of one trial that both pages show."""
    seconds = trial["harbor"]["seconds"]
    summary = trial["summary"]
    cost, source = trial["harbor"]["cost_usd"], trial["harbor"]["cost_source"]
    run = run_result(trial["events"])
    if cost is None and run and run["cost"] is not None:
        cost, source = run["cost"], "agent run total"
    if cost is None and summary["provider_cost"] is not None:
        cost, source = summary["provider_cost"], "transcript"
    outcome = "error" if trial["error"] else "passed" if trial["passed"] else "failed"
    return {
        "run_ms": None if seconds["total"] is None else seconds["total"] * 1000,
        "agent_ms": None
        if seconds["agent_execution"] is None
        else seconds["agent_execution"] * 1000,
        "cost": cost,
        "cost_source": source,
        "outcome": outcome,
    }


def run_result(events: list[dict]) -> dict | None:
    """The last run result event, which holds the totals that the agent wrote."""
    return next((e for e in reversed(events) if e["kind"] == "result"), None)


def task_name(task: str) -> str:
    """The task without its dataset prefix: terminal-bench/x is x."""
    return task.split("/", 1)[1] if task.startswith("terminal-bench/") else task


def short_model(model: str) -> str:
    return model.rsplit("/", 1)[-1]


def chip(outcome: str, title: str | None = None) -> str:
    label = {"passed": "Passed", "failed": "Failed", "error": "Error"}[outcome]
    extra = f' title="{esc(title)}"' if title else ""
    return f'<span class="chip chip-{outcome}"{extra}>{label}</span>'


def page(title: str, body: str, *, depth: int, nav: str, description: str) -> str:
    up = "../" * depth
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="{FONTS}">
<link rel="stylesheet" href="{up}report.css">
<script src="{up}report.js" defer></script>
</head>
<body>
<header class="topbar">
<div class="container topbar-row">
<a class="brand" href="{up}index.html"><img src="{up}logo.svg" alt="" width="25" height="28">
<span class="brand-word">BLOCKETHER<span class="brand-dot">.</span></span>
<span class="brand-name">CTB report</span></a>
<nav aria-label="Sections">{nav}</nav>
</div>
</header>
<main class="container">
{body}
</main>
</body>
</html>
"""


def readings(items: list[tuple[str, str | None, str | None]]) -> str:
    """A row of large values: label, value and an optional line under it."""
    cells = []
    for label, value, note in items:
        under = f'<p class="reading-note">{esc(note)}</p>' if note else ""
        cells.append(
            f'<div class="reading-cell"><p class="reading-label">{esc(label)}</p>'
            f'<p class="reading">{shown(value)}</p>{under}</div>'
        )
    return f'<div class="panels readings">{"".join(cells)}</div>'


def table(head: list[str], rows: list[str], *, cls: str = "", empty: str, attrs: str = "") -> str:
    if not rows:
        return f'<p class="empty">{esc(empty)}</p>'
    return (
        f'<div class="scroll"><table class="data {cls}"{attrs}><thead><tr>{"".join(head)}'
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


# Index


def agent_rows(trials: list[dict]) -> list[str]:
    by_agent: dict[str, list[dict]] = defaultdict(list)
    for trial in trials:
        by_agent[trial["agent"]].append(trial)
    rows = []
    for agent, group in sorted(by_agent.items()):
        passed = sum(t["passed"] for t in group)
        runs = [t["facts"]["run_ms"] for t in group if t["facts"]["run_ms"] is not None]
        timed = [
            t["summary"]["provider_ms"] for t in group if t["summary"]["provider_ms"] is not None
        ]
        costs = [t["facts"]["cost"] for t in group if t["facts"]["cost"] is not None]
        calls = sum(t["summary"]["provider_calls"] for t in group)
        tool_calls = sum(t["summary"]["tool_calls"] for t in group)
        tool_errors, marked_calls = marked_errors(group)
        cost_note = None if len(costs) == len(group) else f"{len(costs)} of {len(group)} trials"
        time_note = None if len(timed) == len(group) else f"{len(timed)} of {len(group)} trials"
        rows.append(
            "<tr>"
            f'<th scope="row" data-v="{esc(agent)}"><a href="?agent={esc(agent)}#trials">'
            f"{esc(agent)}</a></th>"
            + td(number(len(group)), len(group))
            + td(number(passed), passed)
            + td(percent(passed, len(group)), passed / len(group))
            + td(duration(sum(runs)) if runs else None, sum(runs) if runs else -1)
            + td(
                duration(statistics.median(runs)) if runs else None,
                statistics.median(runs) if runs else -1,
            )
            + td(number(calls), calls)
            + td(
                duration(sum(timed)) if timed else None,
                sum(timed) if timed else -1,
                title=time_note,
            )
            + td(money(sum(costs)) if costs else None, sum(costs) if costs else -1, title=cost_note)
            + td(number(tool_calls), tool_calls)
            + td(
                number(tool_errors),
                -1 if tool_errors is None else tool_errors,
                cls="num bad" if tool_errors else "num",
            )
            + td(
                percent(tool_errors, marked_calls),
                tool_errors / marked_calls if tool_errors is not None and marked_calls else -1,
            )
            + "</tr>"
        )
    return rows


def tool_error_rows(trials: list[dict]) -> list[str]:
    totals: dict[tuple[str, str], dict] = {}
    for trial in trials:
        for name, tool in trial["summary"]["tools"].items():
            row = totals.setdefault((trial["agent"], name), {"calls": 0, "errors": 0, "trials": 0})
            row["calls"] += tool["calls"]
            row["errors"] += tool["errors"]
            row["trials"] += 1 if tool["errors"] else 0
    ranked = sorted(
        ((key, row) for key, row in totals.items() if row["errors"]),
        key=lambda item: (-item[1]["errors"], item[0]),
    )
    return [
        "<tr>"
        f'<td data-v="{esc(agent)}">{esc(agent)}</td>'
        f'<td class="mono" data-v="{esc(name)}">{esc(name)}</td>'
        + td(number(row["calls"]), row["calls"])
        + td(number(row["errors"]), row["errors"], cls="num bad")
        + td(percent(row["errors"], row["calls"]), row["errors"] / row["calls"])
        + td(number(row["trials"]), row["trials"])
        + "</tr>"
        for (agent, name), row in ranked
    ]


def trial_rows(trials: list[dict]) -> list[str]:
    rows = []
    for t in trials:
        f, s = t["facts"], t["summary"]
        search = " ".join([t["task"], t["agent"], t["model"], t["round"]]).lower()
        rows.append(
            f'<tr data-agent="{esc(t["agent"])}" data-round="{esc(t["round"])}" '
            f'data-outcome="{f["outcome"]}" data-text="{esc(search)}">'
            f'<th scope="row" data-v="{esc(task_name(t["task"]))}" title="{esc(t["task"])}">'
            f'<a href="trial/{esc(t["id"])}.html">{esc(task_name(t["task"]))}</a></th>'
            f'<td data-v="{esc(t["agent"])}">{esc(t["agent"])}</td>'
            f'<td data-v="{f["outcome"]}">{chip(f["outcome"], t["error"])}</td>'
            + td(duration(f["run_ms"]), -1 if f["run_ms"] is None else f["run_ms"])
            + td(money(f["cost"]), -1 if f["cost"] is None else f["cost"], title=f["cost_source"])
            + td(number(s["provider_calls"]), s["provider_calls"])
            + td(duration(s["provider_ms"]), -1 if s["provider_ms"] is None else s["provider_ms"])
            + td(number(s["tool_calls"]), s["tool_calls"])
            + td(
                number(s["tool_errors"]),
                -1 if s["tool_errors"] is None else s["tool_errors"],
                cls="num bad" if s["tool_errors"] else "num",
            )
            + td(
                None if t["reward"] is None else f"{t['reward']:g}",
                -1 if t["reward"] is None else t["reward"],
            )
            + td(duration(f["agent_ms"]), -1 if f["agent_ms"] is None else f["agent_ms"])
            + f'<td class="mono" data-v="{esc(t["model"])}" title="{esc(t["model"])}">'
            f"{esc(short_model(t['model'])) or NONE}</td>"
            f'<td data-v="{esc(t["round"])}">{esc(t["round"])}</td>'
            + td(stamp(t["started_at"]), t["started_at"] or "", cls="mono")
            + "</tr>"
        )
    return rows


def options(name: str, label: str, values: list[str]) -> str:
    items = "".join(f'<option value="{esc(v)}">{esc(v)}</option>' for v in values)
    return (
        f'<label class="field field-{name}"><span>{esc(label)}</span><select name="{name}">'
        f'<option value="">All</option>{items}</select></label>'
    )


def index(trials: list[dict], generated: str) -> str:
    agents = sorted({t["agent"] for t in trials})
    rounds = sorted({t["round"] for t in trials})
    passed = sum(t["passed"] for t in trials)
    timed = [t["summary"]["provider_ms"] for t in trials if t["summary"]["provider_ms"] is not None]
    costs = [t["facts"]["cost"] for t in trials if t["facts"]["cost"] is not None]
    calls = sum(t["summary"]["provider_calls"] for t in trials)
    tool_calls = sum(t["summary"]["tool_calls"] for t in trials)
    tool_errors, marked_calls = marked_errors(trials)
    lead = (
        f"{len(trials)} trials from {len(agents)} agents in {len(rounds)} rounds. "
        f"Generated {generated}."
    )
    top = readings(
        [
            ("Trials", number(len(trials)), f"{passed} passed"),
            ("Pass rate", percent(passed, len(trials)), None),
            ("Provider calls", number(calls), f"{duration(sum(timed)) or 'no time'} recorded"),
            ("Cost", money(sum(costs)) if costs else None, f"{len(costs)} of {len(trials)} trials"),
            ("Tool calls", number(tool_calls), None),
            (
                "Tool errors",
                number(tool_errors),
                f"{percent(tool_errors, marked_calls)} of the calls that mark errors",
            ),
        ]
    )
    agent_head = [
        th("Agent", "text"),
        th("Trials"),
        th("Passed"),
        th("Pass rate"),
        th("Run time"),
        th("Median run"),
        th("Provider calls"),
        th("Provider time"),
        th("Cost"),
        th("Tool calls"),
        th("Tool errors"),
        th("Error rate"),
    ]
    error_head = [
        th("Agent", "text"),
        th("Tool", "text"),
        th("Calls"),
        th("Errors"),
        th("Error rate"),
        th("Trials with errors"),
    ]
    trial_head = [
        th("Task", "text"),
        th("Agent", "text"),
        th("Outcome", "text"),
        th("Run time"),
        th("Cost"),
        th("Provider calls"),
        th("Provider time"),
        th("Tool calls"),
        th("Tool errors"),
        th("Reward"),
        th("Agent time"),
        th("Model", "text"),
        th("Round", "text"),
        th("Started", "text"),
    ]
    filters = f"""<form class="filters" method="get" action="#trials" data-filters>
<label class="field field-q"><span>Search</span>
<input type="search" name="q" placeholder="Task, agent or model" autocomplete="off"></label>
{options("agent", "Agent", agents)}
{options("round", "Round", rounds)}
<label class="field field-outcome"><span>Outcome</span><select name="outcome"><option value="">All</option>
<option value="passed">Passed</option><option value="failed">Failed</option>
<option value="error">Error</option></select></label>
<noscript><button type="submit" class="filter-submit">Apply</button></noscript>
</form>
<div class="count-row"><p class="count" aria-live="polite" data-count>{len(trials)} trials</p>
<a class="clear" href="index.html#trials" data-clear hidden>Clear filters</a></div>
<p class="empty" data-empty hidden>No trials match these filters.</p>"""
    body = f"""<section class="head">
<h1>Benchmark runs</h1>
<p class="lead">{esc(lead)}</p>
</section>
{top}
<section id="agents" class="block">
<h2>Agents</h2>
<p class="hint">Cost is the API list-price equivalent where the agent reports none. A dash
marks a value that no trial of the agent records.</p>
{table(agent_head, agent_rows(trials), cls="sortable", empty="No trials were found.")}
</section>
<section id="tool-errors" class="block">
<h2>Tool errors by tool</h2>
{
        table(
            error_head,
            tool_error_rows(trials),
            cls="sortable",
            empty="No harness recorded a tool error.",
        )
    }
</section>
<section id="trials" class="block">
<h2>Trials</h2>
{filters}
{
        table(
            trial_head,
            trial_rows(trials),
            cls="sortable trials",
            empty="No trials were found.",
            attrs=" data-trials",
        )
    }
</section>"""
    nav = (
        '<a href="#agents">Agents</a><a href="#tool-errors">Tool errors</a>'
        '<a href="#trials">Trials</a>'
    )
    return page("CTB report", body, depth=0, nav=nav, description=lead)


# Trial page


def offset(at: float | None, base: float | None) -> str | None:
    if at is None or base is None:
        return None
    seconds = max(0, round((at - base) / 1000))
    hours, rest = divmod(seconds, 3600)
    return (
        f"+{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"+{rest // 60}:{rest % 60:02d}"
    )


def run_timeline(trial: dict) -> str:
    """The phase bar of the run and the activity strip of its events: the signature object."""
    phases = [p for p in trial["phases"] if p["start"] is not None and p["end"] is not None]
    parts = []
    if phases:
        start, end = min(p["start"] for p in phases), max(p["end"] for p in phases)
        span = max(end - start, 1)
        bars, legend = [], []
        for p in phases:
            left, width = 100 * (p["start"] - start) / span, 100 * (p["end"] - p["start"]) / span
            name = PHASE_NAMES[p["name"]]
            bars.append(
                f'<span class="phase phase-{p["name"]}" style="left:{left:.3f}%;'
                f'width:{max(width, 0.4):.3f}%" title="{esc(name)}"></span>'
            )
            legend.append(
                f'<li><span class="swatch phase-{p["name"]}"></span>{esc(name)} '
                f'<span class="mono">{esc(duration(p["end"] - p["start"]))}</span></li>'
            )
        parts.append(
            f'<div class="phasebar" role="img" aria-label="Run phases">{"".join(bars)}'
            f'</div><ul class="legend">{"".join(legend)}</ul>'
        )
    else:
        parts.append('<p class="empty">Harbor recorded no phase times for this trial.</p>')
    timed = [e for e in trial["events"] if e["at"] is not None]
    if not timed:
        parts.append(
            '<p class="empty">This harness records no event times, so there is no '
            "activity strip.</p>"
        )
        return "".join(parts)
    execution = next((p for p in phases if p["name"] == "agent_execution"), None)
    low = execution["start"] if execution else min(e["at"] for e in timed)
    high = execution["end"] if execution else max(e["at"] + (e["ms"] or 0) for e in timed)
    width = max(high - low, 1)
    rows = {"call": [0] * BINS, "tool": [0] * BINS, "error": [0] * BINS}
    for e in timed:
        column = min(BINS - 1, max(0, int((e["at"] - low) / width * BINS)))
        if e["kind"] == "call":
            rows["call"][column] += 1
        elif e["kind"] in ("tool", "command"):
            rows["tool"][column] += 1
        if e["error"]:
            rows["error"][column] += 1
    labels = {"call": "Provider calls", "tool": "Tools and commands", "error": "Errors"}
    lines = []
    for key, counts in rows.items():
        peak = max(counts) or 1
        bars = "".join(
            f'<rect class="strip-{key}" x="{column * 10}" y="{24 - (4 + 20 * value / peak):.1f}" '
            f'width="8" height="{4 + 20 * value / peak:.1f}"><title>{value}</title></rect>'
            for column, value in enumerate(counts)
            if value
        )
        lines.append(
            f'<div class="strip-row"><span class="strip-label">{labels[key]} '
            f'<span class="mono">{sum(counts):,}</span></span>'
            f'<svg viewBox="0 0 {BINS * 10} 25" preserveAspectRatio="none" aria-hidden="true">'
            f'{bars}<line class="strip-base" x1="0" x2="{BINS * 10}" y1="24.5" y2="24.5"/></svg>'
            "</div>"
        )
    parts.append(
        f'<figure class="strip" aria-label="Events over the agent execution">{"".join(lines)}'
        f'<figcaption><span class="mono">0:00</span><span>Agent execution in {BINS} equal '
        f'columns</span><span class="mono">{esc(duration(width))}</span></figcaption></figure>'
    )
    return "".join(parts)


def name_table(groups: dict, label: str, empty: str, *, marked: bool = True) -> str:
    """Calls, errors and times by name. `marked` is False when the harness marks no errors."""
    head = [th(label, "text"), th("Calls"), th("Errors"), th("Total time"), th("Mean time")]
    rows = []
    for name, row in groups.items():
        total = row["ms"] if row["timed"] else None
        mean = row["ms"] / row["timed"] if row["timed"] else None
        rows.append(
            f'<tr><th scope="row" class="mono" data-v="{esc(name)}">{esc(name)}</th>'
            + td(number(row["calls"]), row["calls"])
            + (
                td(number(row["errors"]), row["errors"], cls="num bad" if row["errors"] else "num")
                if marked
                else td(None, -1)
            )
            + td(duration(total), -1 if total is None else total)
            + td(duration(mean), -1 if mean is None else mean)
            + "</tr>"
        )
    return table(head, rows, cls="sortable", empty=empty)


def provider_list(trial: dict) -> str:
    s, calls = trial["summary"], [e for e in trial["events"] if e["kind"] == "call"]
    times = [e["ms"] for e in calls if e["ms"] is not None]
    sources = sorted({e["ms_source"] for e in calls if e["ms_source"]})
    timing = [
        ("Calls", number(s["provider_calls"])),
        ("Timed calls", number(s["provider_timed"])),
        ("Total time", duration(s["provider_ms"])),
        ("Mean time", duration(statistics.mean(times)) if times else None),
        ("Longest call", duration(max(times)) if times else None),
        ("Time source", ", ".join(sources) or None),
        ("Failed calls", number(s["provider_errors"])),
    ]
    pairs = [
        ("Priced calls", number(s["provider_priced"])),
        ("Total cost", money(s["provider_cost"])),
        ("Cost source", ", ".join(s["provider_cost_sources"]) or None),
        ("Input tokens", number(s["input"]) if s["input"] else None),
        ("Cached tokens", number(s["cache"]) if s["cache"] else None),
        ("Output tokens", number(s["output"]) if s["output"] else None),
    ]
    groups = [("Time", timing), ("Cost and tokens of the calls", pairs)]
    run = run_result(trial["events"])
    if run:
        used = run["tokens"] or {}
        groups.append(
            (
                "Run totals that the agent wrote",
                [
                    ("Cost", money(run["cost"])),
                    ("Input tokens", number(used.get("input"))),
                    ("Cached tokens", number(used.get("cache"))),
                    ("Output tokens", number(used.get("output"))),
                    ("Duration", duration(run["ms"])),
                ],
            )
        )
    return "".join(
        f'<div class="fact-group"><h3>{esc(title)}</h3><dl class="facts">'
        + "".join(f"<div><dt>{esc(k)}</dt><dd>{shown(v)}</dd></div>" for k, v in items)
        + "</dl></div>"
        for title, items in groups
    )


def body_block(label: str, text: str | None, *, error: bool = False) -> str:
    if text is None or text == "":
        return ""
    is_open = " open" if error or len(text) <= OPEN_LIMIT else ""
    cls = ' class="is-error"' if error else ""
    return (
        f'<details{is_open}{cls}><summary>{esc(label)} <span class="size">'
        f"{len(text):,} characters</span></summary><pre>{esc_text(text)}</pre></details>"
    )


def event_item(index: int, e: dict, base: float | None) -> str:
    kind = e["kind"]
    group = {
        "user": "messages",
        "assistant": "messages",
        "reasoning": "messages",
        "call": "calls",
        "tool": "tools",
        "command": "tools",
        "result": "messages",
    }[kind]
    meta = []
    when = offset(e["at"], base)
    if when:
        meta.append(f'<span class="mono" title="Time from the agent start">{esc(when)}</span>')
    if e["ms"] is not None:
        source = "derived from neighbouring events" if e["ms_source"] == "derived" else "recorded"
        meta.append(
            f'<span class="mono duration" title="Duration, {source}">'
            f"{esc(duration(e['ms']))}</span>"
        )
    if e["tokens"]:
        t = e["tokens"]
        meta.append(
            f'<span class="mono" title="Input, cached and output tokens">'
            f"{t['input']:,} in, {t['cache']:,} cached, {t['output']:,} out</span>"
        )
    if e["cost"] is not None:
        meta.append(
            f'<span class="mono" title="{esc(e["cost_source"] or "")}">'
            f"{esc(money(e['cost']))}</span>"
        )
    if e["error"]:
        meta.append('<span class="chip chip-error">Error</span>')
    name = f'<span class="ev-name mono">{esc(e["name"])}</span>' if e["name"] else ""
    content = []
    if e["text"]:
        content.append(f'<pre class="ev-text">{esc_text(e["text"])}</pre>')
    content.append(body_block("Input", e["input"]))
    content.append(body_block("Output", e["output"], error=bool(e["error"])))
    inner = "".join(content)
    cls = f"ev ev-{kind}" + (" ev-error" if e["error"] else "")
    return (
        f'<li id="e-{index}" class="{cls}" data-group="{group}" '
        f'data-error="{"1" if e["error"] else "0"}">'
        f'<div class="ev-head"><a class="ev-kind" href="#e-{index}">{KINDS[kind]}</a>{name}'
        f'<span class="ev-meta">{"".join(meta)}</span></div>'
        + (f'<section class="ev-body">{inner}</section>' if inner else "")
        + "</li>"
    )


def error_list(events: list[dict]) -> str:
    items = []
    for index, e in enumerate(events):
        if not e["error"]:
            continue
        excerpt = (e["output"] or e["text"] or "").strip().splitlines()
        first = excerpt[0][:160] if excerpt else "No error text was recorded."
        items.append(
            f'<li><div><a class="mono" href="#e-{index}">'
            f"{esc(e['name'] or KINDS[e['kind']])}</a></div>"
            f'<pre class="excerpt">{esc_text(first)}</pre></li>'
        )
    if not items:
        return '<p class="empty">No errors were recorded in this transcript.</p>'
    return f'<ol class="errors">{"".join(items)}</ol>'


def trial_page(trial: dict) -> str:
    f, s, events = trial["facts"], trial["summary"], trial["events"]
    execution = next((p for p in trial["phases"] if p["name"] == "agent_execution"), None)
    base = (
        execution["start"]
        if execution and execution["start"]
        else next((e["at"] for e in events if e["at"] is not None), None)
    )
    header = [
        ("Task", trial["task"]),
        ("Agent", trial["agent"]),
        ("Version", trial["version"]),
        ("Model", trial["model"] or None),
        ("Round", trial["round"]),
        ("Started", stamp(trial["started_at"])),
        ("Reward", None if trial["reward"] is None else f"{trial['reward']:g}"),
        ("Exception", trial["error"]),
    ]
    head_facts = "".join(
        f"<div><dt>{esc(k)}</dt><dd>{shown(v)}</dd></div>"
        for k, v in header
        if v is not None or k in ("Reward",)
    )
    message = (
        f'<pre class="exception">{esc(trial["error_message"])}</pre>'
        if trial["error_message"]
        else ""
    )
    top = readings(
        [
            ("Run time", duration(f["run_ms"]), None),
            ("Agent time", duration(f["agent_ms"]), None),
            (
                "Provider calls",
                number(s["provider_calls"]),
                f"{duration(s['provider_ms'])} in calls" if s["provider_ms"] is not None else None,
            ),
            ("Cost", money(f["cost"]), f["cost_source"]),
            (
                "Tool calls",
                number(s["tool_calls"]),
                f"{duration(s['tool_ms'])} in tools" if s["tool_ms"] is not None else None,
            ),
            ("Tool errors", number(s["tool_errors"]), percent(s["tool_errors"], s["tool_calls"])),
        ]
    )
    notes = "".join(f"<li>{esc(n)}</li>" for n in trial["notes"])
    notes_block = (
        (f'<section class="block"><h2>Recording notes</h2><ul class="notes">{notes}</ul></section>')
        if notes
        else ""
    )
    commands = (
        f'<section class="block"><h2>Commands</h2>'
        f"{name_table(s['commands'], 'Command', 'No commands.')}</section>"
        if s["commands"]
        else ""
    )
    chips = "".join(
        f'<a class="chip-link" href="?show={key}#transcript" data-show="{key}"'
        f"{' aria-current="true"' if key == 'all' else ''}>{label}</a>"
        for key, label in SHOW
    )
    transcript = "".join(event_item(i, e, base) for i, e in enumerate(events))
    body = f"""<section class="head">
<p class="crumb"><a href="../index.html#trials">All trials</a></p>
<h1>{esc(task_name(trial["task"]))}</h1>
<div class="title-row">{chip(f["outcome"], trial["error"])}</div>
<dl class="facts facts-head">{head_facts}</dl>
{message}
</section>
{top}
<section id="timeline" class="block">
<h2>Run timeline</h2>
{run_timeline(trial)}
</section>
<section id="provider" class="block">
<h2>Provider calls</h2>
{provider_list(trial)}
</section>
<section id="tools" class="block">
<h2>Tools</h2>
{
        name_table(
            s["tools"], "Tool", "No tool calls were recorded.", marked=s["tool_errors"] is not None
        )
    }
</section>
{commands}
<section id="errors" class="block">
<h2>Errors</h2>
{error_list(events)}
</section>
{notes_block}
<section id="transcript" class="block">
<h2>Transcript</h2>
<nav class="chips" aria-label="Transcript filter">{chips}</nav>
<p class="count" aria-live="polite" data-event-count>{len(events):,} events</p>
<p class="empty" data-empty hidden>No events of this kind.</p>
<ol class="events" data-events>{
        transcript or '<li class="empty">No transcript was found.</li>'
    }</ol>
</section>"""
    nav = (
        '<a href="#timeline">Timeline</a><a href="#tools">Tools</a><a href="#errors">Errors</a>'
        '<a href="#transcript">Transcript</a>'
    )
    description = (
        f"The {trial['agent']} trial of {task_name(trial['task'])}: times, costs, "
        "tool errors and the transcript."
    )
    return page(
        f"{task_name(trial['task'])}, {trial['agent']}",
        body,
        depth=1,
        nav=nav,
        description=description,
    )


# Build


def write_trial(trial_dir: Path, jobs: Path, out: Path) -> dict:
    """Load one trial, write its page and return it without the events."""
    trial = load(trial_dir, jobs)
    trial["facts"] = facts(trial)
    (out / "trial" / f"{trial['id']}.html").write_text(trial_page(trial), encoding="utf-8")
    trial["events"] = []
    return trial


def build(jobs: Path, out: Path, workers: int | None = None) -> list[dict]:
    """Write the whole site to `out`. A second run rewrites every page and removes stale ones."""
    trial_dirs = find(jobs)
    pages = out / "trial"
    pages.mkdir(parents=True, exist_ok=True)
    for name in ASSETS:
        shutil.copyfile(STATIC / name, out / name)
    workers = workers or min(8, os.cpu_count() or 1)
    if workers == 1:
        trials = [write_trial(t, jobs, out) for t in trial_dirs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            trials = list(pool.map(write_trial, trial_dirs, repeat(jobs), repeat(out)))
    ids = [t["id"] for t in trials]
    if len(set(ids)) != len(ids):
        raise ValueError("Two trials have the same report id.")
    for stale in pages.glob("*.html"):
        if stale.stem not in set(ids):
            stale.unlink()
    trials.sort(key=lambda t: (t["round"], t["agent"], t["task"]))
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    (out / "index.html").write_text(index(trials, generated), encoding="utf-8")
    return trials

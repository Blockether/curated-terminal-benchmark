"""Write the report trials as a static HTML site: index.html and one page for each trial.

The pages work without JavaScript. report.js adds filters, sorting, the URL state and the
Mermaid diagrams, which Pele draws.
A value that a harness does not record shows as "not recorded", never as zero.
"""

import html
import math
import os
import re
import shutil
import statistics
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from itertools import repeat
from pathlib import Path

from ctb.metrics import list_price
from ctb.report.comparison import cohorts

STATIC = Path(__file__).parent / "static"
ASSETS = ("tokens.css", "base.css", "report.css", "pele.global.js", "report.js", "logo.svg")
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
    "call": "Model call",
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
    ("tools", "Tools"),
    ("calls", "Calls"),
    ("errors", "Errors"),
)
NONE = '<span class="none">not recorded</span>'
#: The footer of blockether.com (src/components/Footer.tsx), with a call to action above it.
#: The year is fixed so that a later check of the same site gives the same bytes.
FOOTER = """<footer class="footer">
<div class="container">
<div class="footer-cta">
<p><strong>A benchmark by Blockether.</strong> We build Vis and deliver software engineering for regulated industries:
Clojure, product engineering, data and agentic AI with evaluations.</p>
<p class="footer-actions"><a class="footer-button" href="https://blockether.com/">Visit blockether.com <span aria-hidden="true">↗</span></a>
<a href="https://blockether.com/contact">Contact us <span aria-hidden="true">↗</span></a></p>
</div>
<div class="footer-main">
<div><p class="footer-name">BLOCKETHER sp. z o.o.</p>
<address>Aleja Pokoju 28/117<br>31-564 Cracow, Poland</address></div>
<ul class="footer-list">
<li><a href="https://github.com/Blockether">GitHub <span aria-hidden="true">↗</span></a></li>
<li><a href="https://www.linkedin.com/company/blockether">LinkedIn <span aria-hidden="true">↗</span></a></li>
</ul>
</div>
<div class="footer-legal">
<dl><div><dt>VAT-ID:</dt> <dd>PL6751813221</dd></div><div><dt>KRS:</dt> <dd>0001171097</dd></div></dl>
<ul class="footer-list footer-contact">
<li><a href="tel:+48123002331">+48 123 002 331</a></li>
<li><a href="mailto:contact@blockether.com">contact@blockether.com</a></li>
</ul>
<ul class="footer-links">
<li><a href="https://blockether.com/developers">Developers</a></li>
<li><a href="https://blockether.com/legal">Legal Notice</a></li>
<li><a href="https://blockether.com/sitemap.xml">Sitemap</a></li>
</ul>
<p class="footer-copy">&copy; 2026 BLOCKETHER sp. z o.o. <span>All rights reserved.</span></p>
</div>
</div>
</footer>"""


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


def covered_td(text, sort, recorded, total, unit="trials"):
    cell = td(text, sort)
    note = f'<small class="reading-note">{recorded}/{total} {unit} recorded</small>'
    return cell.replace("</td>", "<br>" + note + "</td>")


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
    if run and run.get("usage_complete") is True and source in (None, "list price"):
        used = run["tokens"]
        cost = list_price(trial["model"], used["input"], used["cache"], used["output"])
        source = "list price from complete session export" if cost is not None else None
    if cost is None and run and run["cost"] is not None:
        cost, source = run["cost"], "agent run total"
    if (
        cost is None
        and summary["provider_cost"] is not None
        and summary["provider_priced"] == summary["provider_calls"]
    ):
        cost, source = summary["provider_cost"], "transcript"
    if run and run.get("usage_complete") is False and source != "agent":
        cost, source = None, None
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


def page(title: str, body: str, *, depth: int, description: str) -> str:
    up = "../" * depth
    # The diagram library is large: only pages with a diagram load it, after report.js.
    diagrams = (
        f'<script src="{up}pele.global.js" defer data-diagrams></script>\n'
        if 'class="mermaid"' in body
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}">
<link rel="stylesheet" href="{up}tokens.css">
<link rel="stylesheet" href="{up}base.css">
<link rel="stylesheet" href="{up}report.css">
<noscript><style>.diagram {{ display: none; }}</style></noscript>
<script src="{up}report.js" defer></script>
{diagrams}</head>
<body>
<header class="topbar">
<div class="container topbar-row">
<a class="brand" href="{up}index.html"><img src="{up}logo.svg" alt="" width="25" height="28">
<span class="brand-word">BLOCKETHER<span class="brand-dot">.</span></span></a>
<span class="brand-name">Terminal-Bench 4.0</span>
</div>
</header>
<main class="container">
{body}
</main>
{FOOTER}
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


def mermaid_text(value: object) -> str:
    """A label that a quoted Mermaid string can hold."""
    return re.sub(r'["#;:]', " ", str(value)).strip()


def diagram(source: str, caption: str) -> str:
    """A Mermaid diagram that report.js draws with Pele. Without JavaScript the tables carry the data."""
    return (
        f'<figure class="diagram"><figcaption>{esc(caption)}</figcaption>'
        f'<pre class="mermaid" role="img" aria-label="{esc(caption)}">{esc_text(source)}</pre>'
        "</figure>"
    )


def bar_chart(
    names: str, values: list[float], axis: str, caption: str, *, top: float | None = None
) -> str:
    """One horizontal bar chart; without `top` the axis ends at a round value above the data."""
    top = top or max(values) or 1
    scale = 10 ** max(0, math.floor(math.log10(top)))
    source = (
        "---\nconfig:\n  xyChart:\n    showDataLabel: true\n---\nxychart-beta horizontal\n"
        f'  x-axis [{names}]\n  y-axis "{axis}" 0 --> {math.ceil(top / scale) * scale}\n'
        f"  bar [{', '.join(map(str, values))}]"
    )
    return diagram(source, caption)


def agent_charts(trials: list[dict], *, shared: bool = False) -> str:
    """Pass rate, cost and tokens of each agent as bar charts; on shared tasks no pass rate."""
    by_agent: dict[str, list[dict]] = defaultdict(list)
    for trial in trials:
        by_agent[trial["agent"]].append(trial)
    if not by_agent:
        return ""
    groups = [group for _, group in sorted(by_agent.items())]
    names = ", ".join(f'"{mermaid_text(agent)}"' for agent in sorted(by_agent))
    scope = "on the shared tasks" if shared else "(not task-normalized)"
    charts = []
    if not shared:
        rates = [round(100 * sum(t["passed"] for t in group) / len(group)) for group in groups]
        charts.append(bar_chart(names, rates, "Pass rate (%)", "Pass rate by agent", top=100))
    # A total needs a value from every trial; a partial sum is not a total.
    if all(t["facts"]["cost"] is not None for t in trials):
        costs = [round(sum(t["facts"]["cost"] for t in group), 2) for group in groups]
        charts.append(bar_chart(names, costs, "Cost (USD)", f"Total cost by agent {scope}"))
    else:
        charts.append('<p class="empty">Cost chart omitted: some trials have no recorded cost.</p>')
    used = {id(t): usage(t) for t in trials}
    for index, label in ((0, "Input tokens"), (2, "Output tokens")):
        if any(used[id(t)][index] is None for t in trials):
            charts.append(
                f'<p class="empty">{label} chart omitted: some trials have no recorded tokens.</p>'
            )
            continue
        totals = [sum(used[id(t)][index] for t in group) for group in groups]
        unit, size = ("millions", 1e6) if max(totals) >= 1e6 else ("thousands", 1e3)
        note = ", cached included" if index == 0 else ""
        charts.append(
            bar_chart(
                names,
                [round(v / size, 1) for v in totals],
                f"{label} ({unit})",
                f"{label} by agent {scope}{note}",
            )
        )
    # Input includes cached tokens, so the cached share of input is the cache hit rate.
    if any(None in used[id(t)][:2] for t in trials):
        charts.append(
            '<p class="empty">Cache chart omitted: some trials have no recorded tokens.</p>'
        )
    else:
        rates = []
        for group in groups:
            given = sum(used[id(t)][0] for t in group)
            rates.append(round(100 * sum(used[id(t)][1] for t in group) / given) if given else 0)
        charts.append(
            bar_chart(
                names,
                rates,
                "Cached share of input (%)",
                f"Cache hit rate by agent {scope}",
                top=100,
            )
        )
    return '<div class="charts">' + "".join(charts) + "</div>"


def normalized(trials: list[dict]) -> tuple[list[dict], list[str]]:
    """The one attempt of each agent on each task that every agent of the same model passed."""
    keep = []
    for group in cohorts(trials, strict=False, basis="solved"):
        solved, agents = set(group["solved"]), set(group["agents"])
        keep += [
            t
            for t in trials
            if t.get("model") == group["model"] and t["agent"] in agents and t["task"] in solved
        ]
    return keep, sorted({t["task"] for t in keep})


def agent_view(trials: list[dict]) -> str:
    """One agent table in two tabs: all attempts, and only the tasks that every agent passed."""
    head = [
        th("Agent", "text"),
        th("Trials"),
        th("Passed"),
        th("Pass rate"),
        th("Run time"),
        th("Median run"),
        th("Calls"),
        th("Recorded call time"),
        th("Cost"),
        th("Tool calls"),
        th("Tool errors"),
        th("Error rate"),
    ]
    general = agent_charts(trials) + table(
        head, agent_rows(trials), cls="sortable", empty="No trials were found."
    )
    shared, tasks = normalized(trials)
    if tasks:
        names = ", ".join(task_name(t) for t in tasks)
        normal = (
            f'<p class="hint">Tasks passed by every agent, one attempt each: {esc(names)}.</p>'
            + agent_charts(shared, shared=True)
            + table(head, agent_rows(shared), cls="sortable", empty="No trials were found.")
        )
    else:
        normal = '<p class="empty">No task was passed by every agent.</p>'
    # One note for both tabs, on one line where the page is wide enough.
    note = (
        '<p class="hint hint-line">Totals, not an efficiency ranking. Cost is the API list-price '
        "equivalent where token usage is recorded; a dash means that no trial records the value.</p>"
    )
    return note + tabs(
        [
            ("general", "General", "All attempts, failures included", general),
            ("normalized", "Normalized", "Only the tasks that every agent passed", normal),
        ],
        "Agent view",
    )


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
        timed_calls = sum(t["summary"].get("provider_timed", 0) for t in group)
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
            + covered_td(
                duration(sum(timed)) if timed else None,
                sum(timed) if timed else -1,
                timed_calls,
                calls,
                "calls",
            )
            + covered_td(
                money(sum(costs)) if costs else None,
                sum(costs) if costs else -1,
                len(costs),
                len(group),
            )
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
            + td(money(f["cost"]), -1 if f["cost"] is None else f["cost"])
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


def index(
    trials: list[dict],
    generated: str,
    *,
    head: str | None = None,
    title: str = "Terminal-Bench 4.0",
    description: str | None = None,
) -> str:
    """The trial overview; head replaces the default heading and description the lead."""
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
            ("Calls", number(calls), f"{duration(sum(timed)) or 'no time'} recorded"),
            ("Cost", money(sum(costs)) if costs else None, f"{len(costs)} of {len(trials)} trials"),
            ("Tool calls", number(tool_calls), None),
            (
                "Tool errors",
                number(tool_errors),
                f"{percent(tool_errors, marked_calls)} of the calls that mark errors",
            ),
        ]
    )
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
        th("Calls"),
        th("Recorded call time"),
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
    if head is None:
        head = f'<section class="head">\n<h1>Benchmark runs</h1>\n<p class="lead">{esc(lead)}</p>\n</section>'
    body = f"""{head}
{top}
<section id="agents" class="block" aria-label="Agents">
{agent_view(trials)}
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
    return page(title, body, depth=0, description=description or lead)


# Trial page


def offset(at: float | None, base: float | None) -> str | None:
    if at is None or base is None:
        return None
    seconds = max(0, round((at - base) / 1000))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def gantt_time(ms: float) -> str:
    """An epoch time in milliseconds as a Gantt date. Pele reads and writes it in one zone, so it stays UTC."""
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")


def run_timeline(trial: dict) -> str:
    """The phase bar of the run and the activity strip of its events: the signature object."""
    phases = [p for p in trial["phases"] if p["start"] is not None and p["end"] is not None]
    parts = []
    if phases:
        start, end = min(p["start"] for p in phases), max(p["end"] for p in phases)
        rows, legend = [], []
        for number_, p in enumerate(phases, 1):
            name = PHASE_NAMES[p["name"]]
            rows.append(f"  {name} :p{number_}, {gantt_time(p['start'])}, {gantt_time(p['end'])}")
            legend.append(
                f'<li>{esc(name)} <span class="mono">{esc(duration(p["end"] - p["start"]))}</span></li>'
            )
        axis = "%H:%M" if end - start >= 1_800_000 else "%H:%M:%S"
        chart = "gantt\n  dateFormat YYYY-MM-DD HH:mm:ss\n  axisFormat " + axis + "\n"
        chart += "\n".join(rows)
        parts.append(
            diagram(chart, "Run phases in UTC") + f'<ul class="legend">{"".join(legend)}</ul>'
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
        if e["error"] or any(c["error"] for c in e["commands"]):
            rows["error"][column] += 1
    labels = {"call": "Calls", "tool": "Tools", "error": "Errors"}
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


def usage(trial: dict) -> tuple[int | None, int | None, int | None]:
    """Input, cached and output tokens from the first source that has them.

    Prefer run totals, then Harbor totals, then calls. Calls can omit side work.
    """
    if (run_result(trial["events"]) or {}).get("usage_complete") is False:
        return None, None, None
    s = trial["summary"]
    used = (run_result(trial["events"]) or {}).get("tokens")
    if used:
        return used["input"], used["cache"], used["output"]
    harbor = trial["harbor"]
    # Harbor writes 0 for a count that the agent did not report, so all zeros mean none.
    if any(harbor.get(key) for key in ("input", "cache", "output")):
        return harbor["input"], harbor["cache"], harbor["output"]
    if any(e["tokens"] for e in trial["events"] if e["kind"] == "call"):
        return s["input"], s["cache"], s["output"]
    return None, None, None


def tokens_note(trial: dict) -> str | None:
    """All tokens of the run in one short phrase, or None when none are recorded."""
    tokens_in, _, tokens_out = usage(trial)
    if tokens_in is None and tokens_out is None:
        return None
    return f"{number((tokens_in or 0) + (tokens_out or 0))} tokens"


def provider_list(trial: dict) -> str:
    """The same call facts for every harness. A value that the harness lacks is not recorded."""
    s, calls = trial["summary"], [e for e in trial["events"] if e["kind"] == "call"]
    times = [e["ms"] for e in calls if e["ms"] is not None]
    tokens_in, cached, tokens_out = usage(trial)
    items = [
        ("Calls", number(s["provider_calls"])),
        ("Recorded call time", duration(s["provider_ms"])),
        ("Mean time", duration(statistics.mean(times)) if times else None),
        ("Longest call", duration(max(times)) if times else None),
        ("Failed calls", number(s["provider_errors"])),
        ("Cost", money(trial["facts"]["cost"])),
        ("Input tokens", number(tokens_in)),
        ("Cached tokens", number(cached)),
        ("Output tokens", number(tokens_out)),
    ]
    return (
        '<dl class="facts">'
        + "".join(f"<div><dt>{esc(k)}</dt><dd>{shown(v)}</dd></div>" for k, v in items)
        + "</dl>"
        + f'<p class="hint">Call timing coverage: {s["provider_timed"]} / {s["provider_calls"]}. '
        "Recorded time is a partial sum when any call lacks timing. Token counts use run totals when available.</p>"
    )


def body_block(label: str, text: str | None, *, error: bool = False, closed: bool = False) -> str:
    if text is None or text == "":
        return ""
    is_open = " open" if error or not closed and len(text) <= OPEN_LIMIT else ""
    cls = ' class="is-error"' if error else ""
    return (
        f'<details{is_open}{cls}><summary>{esc(label)} <span class="size">'
        f"{len(text):,} characters</span></summary><pre>{esc_text(text)}</pre></details>"
    )


def fact(label: str, value: str, title: str, cls: str = "") -> str:
    """One labelled value in an event header, for example "Took 4.8 s"."""
    return (
        f'<span class="{f"fact {cls}".strip()}" title="{esc(title)}">'
        f'<span class="fact-k">{label}</span> '
        f'<span class="mono">{esc(value)}</span></span>'
    )


BOLD = re.compile(r"\*\*([^\n]+?)\*\*")


def rich_text(text: str) -> str:
    """Escaped text with Markdown **bold** as bold, as model messages and summaries write it."""
    return BOLD.sub(r"<strong>\1</strong>", esc_text(text))


def command_step(c: dict) -> str:
    """One command that a tool call ran, inside the row of that tool call."""
    meta = []
    if c["ms"] is not None:
        source = "derived from neighbouring events" if c["ms_source"] == "derived" else "recorded"
        meta.append(
            fact("Took", duration(c["ms"]), f"How long this command ran, {source}", "duration")
        )
    if c["error"]:
        meta.append('<span class="chip chip-error">Error</span>')
    return (
        f'<li class="ev-step{" ev-error" if c["error"] else ""}"><div class="ev-head">'
        f'<span class="ev-kind mono" title="{KINDS[c["kind"]]}">{esc(c["name"] or KINDS[c["kind"]])}</span>'
        f'<span class="ev-meta">{"".join(meta)}</span></div>'
        + body_block("Command", c["input"])
        + body_block("Output", c["output"], error=bool(c["error"]))
        + "</li>"
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
        meta.append(fact("Started", when, "Time since the agent started (minutes:seconds)"))
    if e["ms"] is not None:
        source = "derived from neighbouring events" if e["ms_source"] == "derived" else "recorded"
        meta.append(
            fact("Took", duration(e["ms"]), f"How long this step ran, {source}", "duration")
        )
    if e["tokens"]:
        t = e["tokens"]
        meta.append(
            fact(
                "Tokens",
                f"{t['input']:,} in · {t['cache']:,} cached · {t['output']:,} out",
                "Input, cached and output tokens",
            )
        )
    if e["cost"] is not None:
        meta.append(fact("Cost", money(e["cost"]), "Cost of this call"))
    if e["error"]:
        meta.append('<span class="chip chip-error">Error</span>')
    # One label: the tool or record name when there is one, else the kind.
    label = esc(e["name"] or KINDS[kind])
    mono = " mono" if e["name"] and kind in ("tool", "command") else ""
    content = []
    if e["text"]:
        content.append(f'<pre class="ev-text">{rich_text(e["text"])}</pre>')
    if e["commands"]:
        # The commands come first; the script that started them and its raw result stay closed.
        content.append(
            f'<ol class="ev-steps">{"".join(command_step(c) for c in e["commands"])}</ol>'
        )
        content.append(body_block("Script", e["input"], closed=True))
        content.append(
            body_block("Script output", e["output"], error=bool(e["error"]), closed=True)
        )
    else:
        content.append(body_block("Input", e["input"]))
        content.append(body_block("Output", e["output"], error=bool(e["error"])))
    inner = "".join(content)
    failed = bool(e["error"]) or any(c["error"] for c in e["commands"])
    cls = f"ev ev-{kind}" + (" ev-error" if failed else "")
    return (
        f'<li id="e-{index}" class="{cls}" data-group="{group}" '
        f'data-error="{"1" if failed else "0"}">'
        f'<div class="ev-head"><a class="ev-kind{mono}" href="#e-{index}" title="{KINDS[kind]}">{label}</a>'
        f'<span class="ev-meta">{"".join(meta)}</span></div>'
        + (f'<section class="ev-body">{inner}</section>' if inner else "")
        + "</li>"
    )


VERIFY_KINDS = {"suite": "Suite", "test": "Test", "log": "Log"}
VERIFY_HINT = (
    '<p class="hint">What the verifier ran after the agent finished: the task tests, '
    "their output and the logs of task services.</p>"
)
STATUS_CHIPS = {"passed": "chip-passed", "failed": "chip-error"}


def status_chip(status: str | None) -> str:
    if not status:
        return ""
    return f'<span class="chip {STATUS_CHIPS.get(status, "chip-failed")}">{esc(status.capitalize())}</span>'


def verification_item(index: int, e: dict) -> str:
    failed = e["status"] == "failed"
    meta = [status_chip(e["status"])]
    if e["ms"] is not None:
        meta.append(fact("Took", duration(e["ms"]), "How long this ran, recorded", "duration"))
    if e["kind"] == "suite":
        inner = f'<pre class="ev-text">{esc_text(e["text"])}</pre>' if e["text"] else ""
    else:
        label = "Output" if e["kind"] == "log" else "Failure" if failed else "Details"
        inner = body_block(label, e["text"], error=failed)
    return (
        f'<li id="v-{index}" class="ev ev-{e["kind"]}{" ev-error" if failed else ""}">'
        f'<div class="ev-head"><a class="ev-kind{"" if e["kind"] == "suite" else " mono"}" '
        f'href="#v-{index}" title="{VERIFY_KINDS[e["kind"]]}">{esc(e["name"])}</a>'
        f'<span class="ev-meta">{"".join(meta)}</span></div>'
        + (f'<section class="ev-body">{inner}</section>' if inner else "")
        + "</li>"
    )


def passed_tests(tests: list[dict]) -> str:
    """Tests without a failure, in one closed list."""
    rows = "".join(
        f'<li><span class="mono">{esc(t["name"])}</span> {status_chip(t["status"])}'
        + (
            f' <span class="mono duration">{esc(duration(t["ms"]))}</span>'
            if t["ms"] is not None
            else ""
        )
        + "</li>"
        for t in tests
    )
    return (
        f'<li class="ev ev-tests"><details><summary>{len(tests):,} tests without a failure</summary>'
        f'<ul class="test-names">{rows}</ul></details></li>'
    )


def verification_list(entries: list[dict]) -> str:
    """Runs, their failed tests and logs; the other tests of a run collapse into one list."""
    if not entries:
        return '<p class="empty">No verifier output was recorded.</p>'
    items, quiet = [], []
    for index, e in enumerate(entries):
        if e["kind"] == "test" and e["status"] != "failed":
            quiet.append(e)
            continue
        if quiet and e["kind"] != "test":
            items.append(passed_tests(quiet))
            quiet = []
        items.append(verification_item(index, e))
    if quiet:
        items.append(passed_tests(quiet))
    return f'<ol class="events" data-verification>{"".join(items)}</ol>'


def tabs(panels: list[tuple[str, str, str, str]], label: str) -> str:
    """Tab cards with a label and a short note, and their panels; the first tab starts selected.

    Without JavaScript all panels show."""
    buttons = "".join(
        f'<button type="button" role="tab" id="tab-{key}" aria-controls="{key}" '
        f'aria-selected="{"true" if i == 0 else "false"}" data-tab="{key}">'
        f'<span class="tab-name">{esc(name)}</span><small>{esc(note)}</small></button>'
        for i, (key, name, note, _) in enumerate(panels)
    )
    return f'<div class="tabs" role="tablist" aria-label="{esc(label)}">{buttons}</div>' + "".join(
        f'<div id="{key}" role="tabpanel" aria-labelledby="tab-{key}" data-tab-panel="{key}">{html}</div>'
        for key, _, _, html in panels
    )


def error_list(events: list[dict]) -> str:
    items = []
    # A failed command links to the row of the tool call that ran it.
    failed = (
        (index, x) for index, e in enumerate(events) for x in (e, *e["commands"]) if x["error"]
    )
    for index, e in failed:
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
                "Calls",
                number(s["provider_calls"]),
                f"{duration(s['provider_ms'])} in calls" if s["provider_ms"] is not None else None,
            ),
            ("Cost", money(f["cost"]), tokens_note(trial)),
            (
                "Tool calls",
                number(s["tool_calls"]),
                f"{duration(s['tool_ms'])} in tools" if s["tool_ms"] is not None else None,
            ),
            ("Tool errors", number(s["tool_errors"]), percent(s["tool_errors"], s["tool_calls"])),
        ]
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
    implementation = (
        f'<nav class="chips" aria-label="Transcript filter">{chips}</nav>'
        '<p class="hint">Started: minutes and seconds after the agent started. '
        "Took: how long that step ran.</p>"
        f'<p class="count" aria-live="polite" data-event-count>{len(events):,} events</p>'
        '<p class="empty" data-empty hidden>No events of this kind.</p>'
        '<ol class="events" data-events>'
        + (transcript or '<li class="empty">No transcript was found.</li>')
        + "</ol>"
    )
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
<h2>Calls</h2>
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
<section id="transcript" class="block">
<h2>Transcript</h2>
{
        tabs(
            [
                ("implementation", "Implementation", "The agent's transcript", implementation),
                (
                    "verification",
                    "Verification",
                    "Tests and logs after the run",
                    VERIFY_HINT + verification_list(trial["verification"]),
                ),
            ],
            "Trial phase",
        )
    }
</section>"""
    description = (
        f"The {trial['agent']} trial of {task_name(trial['task'])}: times, costs, "
        "tool errors, the transcript and the verifier results."
    )
    return page(
        f"{task_name(trial['task'])}, {trial['agent']}",
        body,
        depth=1,
        description=description,
    )


# Build


def write_trial(trial_dir: Path, jobs: Path, out: Path) -> dict:
    """Load one trial, write its page and return it without the events."""
    from ctb.report.trials import load

    trial = load(trial_dir, jobs)
    trial["facts"] = facts(trial)
    (out / "trial" / f"{trial['id']}.html").write_text(trial_page(trial), encoding="utf-8")
    trial["events"] = []
    return trial


def build(jobs: Path, out: Path, workers: int | None = None) -> list[dict]:
    """Write the whole site to `out`. A second run rewrites every page and removes stale ones."""
    from ctb.report.trials import find

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

"""Render the benchmark as a portrait research report."""

import json
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics import renderPDF
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph
from svglib.svglib import svg2rlg

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results/opus-comparison-branded"
WIDTH, HEIGHT = A4
INK = colors.HexColor("#262626")
GRAY = colors.HexColor("#3f3f3f")
CREAM = colors.HexColor("#faf3eb")
AMBER = colors.HexColor("#ffc420")
RULE = colors.HexColor("#d6cbbd")


def render(data_path=None, output_path=None):
    for name, file in (
        ("Inter", "Inter-Regular.ttf"),
        ("InterBold", "Inter-Bold.ttf"),
        ("InterBlack", "Inter-Black.ttf"),
        ("Mono", "JetBrainsMono.ttf"),
    ):
        pdfmetrics.registerFont(TTFont(name, str(OUT / "fonts" / file)))
    data = json.loads((data_path or ROOT / "results/opus-comparison/data.json").read_text())
    groups = sorted(data["groups"], key=lambda g: (-g["passed"], g["agent"], g["model"]))
    path = output_path or OUT / "blockether-benchmark.pdf"
    canvas = Canvas(str(path), pagesize=A4, invariant=1)
    canvas.setTitle("Blockether | Coding agent benchmark")
    canvas.setAuthor("Blockether")
    number = 0
    cursor = 0

    def text(value, x, y, size=12, font="Inter", color=INK):
        canvas.setFillColor(color)
        canvas.setFont(font, size)
        canvas.drawString(x, y, str(value))

    def paragraph(value, size=12):
        nonlocal cursor
        style = ParagraphStyle(
            "body",
            fontName="Inter",
            fontSize=size,
            leading=size * 1.55,
            textColor=INK,
            alignment=TA_JUSTIFY,
        )
        item = Paragraph(escape(value), style)
        _, height = item.wrap(WIDTH - 96, HEIGHT)
        if cursor - height < 66:
            raise ValueError(f"Text exceeds its page: {value[:60]}")
        item.drawOn(canvas, 48, cursor - height)
        cursor -= height + 17

    def heading(value):
        nonlocal cursor
        cursor -= 9
        text(value, 48, cursor, 17, "InterBold")
        cursor -= 27

    def page(section, title, subtitle=None):
        nonlocal number, cursor
        if number:
            canvas.showPage()
        number += 1
        canvas.setFillColor(CREAM)
        canvas.rect(0, 0, WIDTH, HEIGHT, fill=1, stroke=0)
        logo = svg2rlg(str(OUT / "brand-lockup.svg"))
        logo.scale(0.85, 0.85)
        renderPDF.draw(logo, canvas, 48, HEIGHT - 65)
        canvas.setFillColor(INK)
        canvas.setFont("Mono", 9)
        canvas.drawRightString(WIDTH - 48, HEIGHT - 65, "18 October 2026")
        text(title, 48, HEIGHT - 151, 28, "InterBlack")
        canvas.setFillColor(AMBER)
        canvas.rect(48, HEIGHT - 172, 48, 4, fill=1, stroke=0)
        cursor = HEIGHT - 197
        if subtitle:
            paragraph(subtitle, 12)
        canvas.setStrokeColor(RULE)
        canvas.line(48, 45, WIDTH - 48, 45)
        text(section.upper(), 48, 27, 9, "Mono")
        text(f"{number:02}", WIDTH - 65, 27, 9, "Mono")

    def model(group):
        return "Opus 5.5" if "anthropic" in group["model"] else "GPT 6.1 sol"

    def pair(group):
        return f"{group['agent']} / {model(group)}"

    def bars(key, maximum, ticks, suffix=""):
        nonlocal cursor
        left, width, top = 48, 420, 566
        for tick in ticks:
            x = left + width * tick / maximum
            canvas.setStrokeColor(RULE)
            canvas.line(x, 145, x, top + 3)
            text(f"{tick}{suffix}", x - 3, 122, 10, "Mono")
        for i, group in enumerate(groups):
            y = top - i * (380 / max(len(groups) - 1, 1))
            value = group["passed"] / 12 * 100 if key == "rate" else group[key]
            text(pair(group), 48, y + 17, 12, "InterBold")
            canvas.setFillColor(AMBER if "anthropic" in group["model"] else GRAY)
            canvas.rect(left, y - 16, width * value / maximum, 22, fill=1, stroke=0)
            label = (
                f"{value:.1f}%"
                if key == "rate"
                else f"${value:.2f}"
                if key == "cost_usd"
                else f"{value:.1f}"
            )
            text(label, 480, y - 10, 10, "Mono")
        cursor = 100

    page(
        "01 / Objective",
        "Coding agent benchmark",
        f"{len(groups)} harness/model pairs. Twelve curated tasks. {len(data['rows'])} scored attempts.",
    )
    heading("Objective")
    paragraph(
        "Measure task completion, execution time and cost across the recorded coding agent configurations. Compare tools for the selected workload.",
    )
    heading("Task selection")
    paragraph(
        "The CPU-only subset contains six easier tasks with expert estimates of 0.75–2 hours and six harder tasks estimated at 4–8 hours. Every pair receives the same tasks."
    )
    paragraph(
        "Twelve tasks cover security, routing geometry, music constraints, build artifacts, cryptanalysis, payments, storage, live systems, graph performance and UI behavior.",
    )
    heading("Study scope")
    paragraph(
        "The unit of comparison is the configured harness/model pair. The dataset is curated, with one attempt per pair and task. Scores describe completion on these twelve tasks."
    )

    page("01 / Objective", "Methodology")
    heading("Execution and verification")
    paragraph(
        "Harbor starts each task environment and installs the pinned harness. The runner supplies the task and model, requests medium reasoning effort, and executes one trial at a time. The agent timeout multiplier is 0.25; an eight-hour task receives a two-hour agent limit."
    )
    paragraph(
        "A separate verifier checks artifacts and behavior after execution. Reward 1 means resolved. Each pair has twelve scored attempts. Runtime failures and provider refusals count as unresolved."
    )
    heading("Configuration and accounting")
    paragraph(
        "OpenCode on Opus uses a 128,000-token output cap. Other configurations retain their recorded defaults. Cost uses agent-reported values where available, otherwise pinned API prices. Time covers agent execution, excluding setup and verification."
    )
    heading("Evidence and exclusions")
    paragraph(
        "GPT and Claude Code results use sealed r3. The later pi and OpenCode Opus trials have hash-locked evidence from a dirty checkout, outside the clean-commit seal. Two OpenCode setup failures are retained and replaced: html-js-filter scored 0 and photonic-waveguide-routing scored 1. The earlier 32k Opus smoke is excluded."
    )

    page(
        "02 / Results",
        "Task resolution",
        "Resolved tasks / 12 (%). Amber: Opus 5.5. Graphite: GPT 6.1 sol.",
    )
    bars("rate", 100, [0, 25, 50, 75, 100], "%")
    paragraph(
        "Codex / GPT resolves 8 tasks; OpenCode / Opus resolves 5; pi / Opus resolves 4. The other pairs resolve 3 each.",
        10,
    )

    page(
        "02 / Results",
        "Cost ($)",
        "Total cost ($) across twelve scored attempts per pair.",
    )
    bars("cost_usd", 40, [0, 10, 20, 30, 40])
    paragraph(
        "Pi / GPT has the lowest measured cost at $2.82. Codex / GPT resolves the most tasks at $5.46.",
        10,
    )

    page(
        "02 / Results",
        "Execution time",
        "Total agent execution time (minutes) across twelve scored attempts per pair.",
    )
    time_max = max(250, int(max(g["agent_minutes"] for g in groups) / 50 + 1) * 50)
    bars("agent_minutes", time_max, list(range(0, time_max + 1, 50)))
    paragraph("Totals exclude environment setup, verifier execution and excluded attempts.", 10)

    page(
        "02 / Results",
        "Cost and resolution",
        "Resolution rate (%) against cost ($). Rings mark non-dominated pairs.",
    )
    left, bottom, width, height = 90, 360, 400, 225
    for tick in [0, 10, 20, 30, 40]:
        x = left + width * tick / 40
        canvas.setStrokeColor(RULE)
        canvas.line(x, bottom, x, bottom + height)
        text(f"${tick}", x - 10, bottom - 25, 10, "Mono")
    for tick in [0, 25, 50, 75, 100]:
        y = bottom + height * tick / 100
        canvas.setStrokeColor(RULE)
        canvas.line(left, y, left + width, y)
        text(f"{tick}%", 48, y - 4, 10, "Mono")
    for i, group in enumerate(groups, 1):
        x = left + width * group["cost_usd"] / 40
        y = bottom + height * group["passed"] / 12
        canvas.setFillColor(AMBER if "anthropic" in group["model"] else GRAY)
        canvas.circle(x, y, 4, fill=1, stroke=0)
        frontier = not any(
            other["cost_usd"] <= group["cost_usd"]
            and other["passed"] >= group["passed"]
            and (other["cost_usd"] < group["cost_usd"] or other["passed"] > group["passed"])
            for other in groups
        )
        if frontier:
            canvas.setStrokeColor(INK)
            canvas.circle(x, y, 8, fill=0, stroke=1)
        dx, dy = (-12, -22) if group["agent"] == "pi" and "openai" in group["model"] else (10, 9)
        text(f"P{i}", x + dx, y + dy, 10, "Mono")
        text(f"P{i}  {pair(group)}", 48, 290 - (i - 1) * 23, 11)
    cursor = 128
    paragraph(
        "The observed frontier contains pi / GPT and Codex / GPT. A non-dominated pair has no alternative that costs no more and resolves at least as many tasks, with one strict improvement.",
        10,
    )

    page(
        "02 / Results",
        "Results ledger",
        f"P1–P{len(groups)} identify the same configurations in the charts and task matrix.",
    )
    for i, group in enumerate(groups, 1):
        y = 580 - (i - 1) * (355 / max(len(groups) - 1, 1))
        text(f"P{i}  {pair(group)}", 48, y, 13, "InterBold")
        text(f"{group['passed']}/12 passed", 48, y - 25, 10, "Mono")
        text(f"{group['agent_minutes']:.1f} min", 180, y - 25, 10, "Mono")
        text(f"${group['cost_usd']:.2f}", 320, y - 25, 10, "Mono")
        text(f"${group['cost_per_pass_usd']:.2f}/pass", 420, y - 25, 10, "Mono")
        canvas.setStrokeColor(RULE)
        canvas.line(48, y - 40, WIDTH - 48, y - 40)
    cursor = 125
    paragraph(
        "Harness versions: Codex 0.163.0; Claude Code 2.1.302; OpenCode 1.19.3; pi 1.3.0. Opus authentication plugins: OpenCode 1.8.6; pi 3.4.3.",
        10,
    )

    page(
        "02 / Results",
        "Task-level outcomes",
        "1 = resolved. 0 = unresolved. * = runtime exception or provider refusal.",
    )
    for i in range(len(groups)):
        text(f"P{i + 1}", 310 + i * 32, 583, 10, "Mono")
    for i, task in enumerate(sorted({row["task"] for row in data["rows"]})):
        y = 547 - i * 31
        text(task.split("/")[-1], 48, y, 10)
        for j, group in enumerate(groups):
            row = next(
                r
                for r in data["rows"]
                if (r["agent"], r["model"], r["task"]) == (group["agent"], group["model"], task)
            )
            text(
                ("1" if row["reward"] == 1 else "0") + ("*" if row["error"] else ""),
                310 + j * 32,
                y,
                10,
                "Mono",
            )
        canvas.setStrokeColor(RULE)
        canvas.line(48, y - 11, WIDTH - 48, y - 11)
    cursor = 160
    paragraph(
        "Provider refusals occurred for Claude Code on interleaved-vigenere and OpenCode Opus on kv-live-surgery. OpenCode GPT hung on vf2-speedup-networkx; its cause remains unresolved.",
        11,
    )

    page("03 / Interpretation", "Interpretation")
    heading("Completion and cost")
    paragraph(
        "Codex / GPT leads completion with 8/12 resolved tasks at $5.46. Pi / GPT has the lowest total cost at $2.82 and resolves 3/12. These pairs define the observed cost–resolution frontier."
    )
    heading("Within-harness differences")
    paragraph(
        "OpenCode resolves 5/12 tasks with Opus and 3/12 with GPT. Pi resolves 4/12 with Opus and 3/12 with GPT. The higher Opus scores come with higher costs. These differences include model, plugin and budget effects."
    )
    heading("Task sensitivity")
    paragraph(
        "One task changes the resolution rate by 8.3 percentage points. No configuration resolves bun-sourcemap-leak or music-harmony. Repeated runs can distinguish stable failure patterns from run-to-run variation."
    )
    heading("Use of the results")
    paragraph(
        "For this workload, Codex / GPT offers the highest completion rate, while pi / GPT offers the lowest measured cost. Tool selection can use these two reference points alongside the task-level results."
    )

    page("04 / References", "References")
    heading("Study records")
    paragraph(
        "Task selection: tasks.toml. Scored outcomes and aggregate metrics: results/opus-comparison/data.json. Evidence hashes: results/opus-comparison/manifest.json."
    )
    paragraph(
        "Presentation assets and PDF hashes: results/opus-comparison-branded/manifest.json. The report uses the same 72 scored attempts as the source comparison."
    )
    heading("Evaluation methodology")
    paragraph(
        "Anthropic — SWE-bench engineering report. Reference for evaluating a model together with its harness."
    )
    paragraph("https://www.anthropic.com/engineering/swe-bench-sonnet", 10)
    paragraph(
        "Terminal-Bench — leaderboard presentation of task resolution and uncertainty. The fetched page at the link below displayed Terminal-Bench 4.0."
    )
    paragraph("https://www.tbench.ai/leaderboard/terminal-bench/2.0", 10)
    heading("Brand assets")
    paragraph(
        "Blockether — typeface, symbol and wordmark geometry from https://blockether.com/. The source font and measured geometry are retained with the presentation assets."
    )

    page("05 / Further work", "Further work")
    heading("01 / Repeat the trials")
    paragraph(
        "Run multiple attempts for each pair and task. Report variation in completion, cost and time, then estimate uncertainty from the repeated measurements."
    )
    heading("02 / Match the budgets")
    paragraph(
        "Align output caps, reasoning settings and time limits where the harnesses permit. Record remaining configuration differences alongside each result."
    )
    heading("03 / Expand task coverage")
    paragraph(
        "Add tasks across difficulty levels and domains. Define the selection procedure before execution and retain task-level outcomes for each configuration."
    )
    heading("04 / Resolve failure mechanisms")
    paragraph(
        "Reproduce the OpenCode GPT hang on vf2-speedup-networkx. Inspect verifier artifacts for the shared failures on bun-sourcemap-leak and music-harmony."
    )
    heading("05 / Standardize the next run")
    paragraph(
        "Run all configurations from a clean, pinned checkout in one campaign. Use a common cost ledger and apply the standard evidence seal to every trial."
    )
    if any(g["agent"] == "dirge" for g in groups):
        page("06 / Dirge evidence", "Dirge: sealed results")
        heading("Primary series: 3 of 12 resolved")
        paragraph(
            "Dirge 0.25.7 uses the pinned f36b2890 source with token accounting patches. "
            "The GPT series uses clean harness commit 4e65f21 and its own immutable seal. "
            "The original twelve attempts remain in all primary charts and totals."
        )
        heading("Failures and time limits")
        paragraph(
            "Five completed solutions failed verification. Payments and session-window tasks reached the two-hour limit. "
            "The bun-sourcemap and React tasks failed at startup because system CA certificates were absent."
        )
        heading("CA retries: separate evidence")
        paragraph(
            "After certificate installation, both startup failures were retried. Both agents completed execution, "
            "but both solutions scored zero. These retries have a dirty run record and are not canonically sealed. "
            "Their evidence hashes are retained separately; they do not replace primary scores."
        )
        heading("Accounting limits")
        paragraph(
            "Dirge stores native JSONL transcripts rather than ATIF trajectories. Zero ATIF step counts do not mean no tool use. "
            "Startup failures have no model usage. Timeout trials may lack final usage, so aggregate cost can undercount them."
        )
    canvas.save()
    return path


if __name__ == "__main__":
    print(render())

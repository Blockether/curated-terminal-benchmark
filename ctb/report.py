"""Build a deterministic comparison and an evidence manifest, without bypassing round seals."""

import hashlib
import html
import json
from pathlib import Path

from ctb.metrics import round_metrics, totals, trial_metrics

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = "results/opus-comparison"


def encode(value):
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def chart(groups, key, title, unit):
    width, height = 940, 100 + 48 * len(groups)
    maximum = max(g[key] or 0 for g in groups) or 1
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" role="img">',
        f"<title>{html.escape(title)}</title>",
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="20" y="30" font-family="sans-serif" font-size="20">{html.escape(title)}</text>',
    ]
    for i, group in enumerate(groups):
        y = 65 + i * 48
        label = group["agent"] + " / " + group["model"].split("/")[-1]
        value = group[key] or 0
        parts += [
            f'<text x="20" y="{y + 19}" font-family="sans-serif" font-size="13">{html.escape(label)}</text>',
            f'<rect x="380" y="{y}" width="{450 * value / maximum:.2f}" height="26" fill="#2563eb"/>',
            f'<text x="845" y="{y + 19}" font-family="sans-serif" font-size="13">{value} {unit}</text>',
        ]
    return "\n".join(parts + ["</svg>"]) + "\n"


def build(root=ROOT, *, include_dirge=False):
    files = {}

    def evidence(path):
        files[str(path.relative_to(root))] = digest(path.read_bytes())

    sealed_path = root / "results/r3.json"
    evidence(sealed_path)
    sealed = json.loads(sealed_path.read_text())
    rows = round_metrics(root, sealed)["rows"]
    for row, original in zip(rows, sealed["rows"]):
        row["source"] = original["result"]
        row["provenance"] = "sealed-r3"
    excluded = []
    for agent in ("pi", "opencode"):
        for task in sorted(
            p.name.split("__", 2)[-1]
            for p in (root / "jobs/opus-baseline-128k").glob(f"{agent}__anthropic__*")
        ):
            round_name = "opus-baseline-128k"
            if agent == "opencode" and task in ("html-js-filter", "photonic-waveguide-routing"):
                old = sorted(
                    (root / "jobs" / round_name / f"{agent}__anthropic__{task}").glob(
                        "*/result.json"
                    )
                )
                assert len(old) == 1
                evidence(old[0])
                excluded.append(
                    {
                        "result": str(old[0].relative_to(root)),
                        "reason": "Invalid runner configuration: missing limit.context; replaced, not scored.",
                    }
                )
                round_name = "opus-128k-config-recovery"
            candidates = sorted(
                (root / "jobs" / round_name / f"{agent}__anthropic__{task}").glob("*/result.json")
            )
            assert len(candidates) == 1, (agent, task)
            path = candidates[0]
            result = json.loads(path.read_text())
            reward = result["verifier_result"]["rewards"]["reward"]
            assert float((path.parent / "verifier/reward.txt").read_text()) == reward
            rows.append(
                {
                    "agent": agent,
                    "model": "anthropic/claude-opus-5-5",
                    "agent_version": "1.3.0" if agent == "pi" else "1.19.3",
                    "task": "terminal-bench/" + task,
                    "reward": reward,
                    "error": (result.get("exception_info") or {}).get("exception_type"),
                    "source": str(path.relative_to(root)),
                    "provenance": "dirty-run-evidence",
                    **trial_metrics(path),
                }
            )
    assert len(rows) == 72
    supplements = []
    if include_dirge:
        dirge_path = root / "results/dirge-gpt-usage-4e65f21.json"
        evidence(dirge_path)
        dirge_seal = json.loads(dirge_path.read_text())
        dirge_rows = round_metrics(root, dirge_seal)["rows"]
        assert len(dirge_rows) == 12
        for row, original in zip(dirge_rows, dirge_seal["rows"]):
            row["source"] = original["result"]
            row["provenance"] = "sealed-dirge-gpt-usage-4e65f21"
        rows.extend(dirge_rows)
        retry_root = root / "jobs/dirge-gpt-ca-retry"
        evidence(retry_root / "ctb-runs.json")
        for result in sorted(retry_root.glob("*/*/result.json")):
            evidence(result)
            supplements.append(
                {
                    "source": str(result.relative_to(root)),
                    "provenance": "dirty-run-evidence; excluded from primary scores",
                    **trial_metrics(result),
                }
            )
            for artifact in (result.parent / "agent").glob("dirge*"):
                if artifact.is_file():
                    evidence(artifact)
    rows.sort(key=lambda r: (r["agent"], r["model"], r["task"]))
    for row in rows:
        path = root / row["source"]
        evidence(path)
        for rel in (
            "trial.log",
            "agent/trajectory.json",
            "agent/dirge.jsonl",
            "agent/dirge-stderr.log",
            "verifier/reward.txt",
            "verifier/test-stdout.txt",
        ):
            artifact = path.parent / rel
            if artifact.is_file():
                evidence(artifact)
    for name in ("opus-baseline-128k", "opus-128k-config-recovery"):
        evidence(root / "jobs" / name / "ctb-runs.json")
    for path in sorted((root / "ctb").rglob("*.py")):
        evidence(path)
    groups = totals(rows)
    assert len(groups) == (7 if include_dirge else 6) and all(g["trials"] == 12 for g in groups)
    data = {
        "schema_version": 1,
        "status": "evidence-locked; new Opus rounds are not canonically sealed",
        "selection": "All r3 trials, plus 24 Opus trials. Replace only two pre-execution configuration failures."
        + (
            " Add all 12 sealed Dirge trials; keep CA retries outside primary scores."
            if include_dirge
            else ""
        ),
        "r3_digest": sealed["digest"],
        "rows": rows,
        "groups": groups,
        "excluded": excluded,
        "supplements": supplements,
    }
    lines = [
        "# Coding harness comparison",
        "",
        "## Status and scope",
        "",
        f"This report contains {len(rows)} scored trials: {len(groups)} harness/model pairs across the same 12 tasks.",
        "The r3 results are sealed. The new Opus runs used a dirty checkout and cannot pass the standard seal check.",
        "Dirge uses the sealed 12-trial series when included. CA retries are separate, unsealed evidence and do not replace primary scores.",
        "The SHA-256 manifest locks the available evidence and report bytes. It does not certify a clean historical checkout.",
        "The source hashes describe the current generator and adapters, not a reconstructed historical checkout.",
        "",
        "## Results",
        "",
        "| Harness | Model | Version | Passed | Agent minutes | Median minutes | API-equivalent USD | USD per pass |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for g in groups:
        lines.append(
            f"| {g['agent']} | {g['model'].split('/')[-1]} | {', '.join(g['agent_versions'])} | {g['passed']}/12 | {g['agent_minutes']} | {g['median_agent_minutes']} | {g['cost_usd']:.2f} | {g['cost_per_pass_usd']:.2f} |"
        )
    lines += [
        "",
        "![Passed tasks](passes.svg)",
        "",
        "![API-equivalent cost](cost.svg)",
        "",
        "![Agent execution time](time.svg)",
        "",
        "## Task outcomes",
        "",
        "Columns use the pair IDs listed below. PASS means reward 1; FAIL means reward 0.",
        "",
    ]
    for i, g in enumerate(groups):
        lines.append(f"- P{i + 1}: {g['agent']} / {g['model'].split('/')[-1]}")
    lines += [
        "",
        "| Task | " + " | ".join(f"P{i + 1}" for i in range(len(groups))) + " |",
        "|---|" + "---|" * len(groups),
    ]
    for task in sorted({r["task"] for r in rows}):
        cells = []
        for g in groups:
            r = next(
                r
                for r in rows
                if (r["agent"], r["model"], r["task"]) == (g["agent"], g["model"], task)
            )
            cells.append(("PASS" if r["reward"] == 1 else "FAIL") + ("*" if r["error"] else ""))
        lines.append("| " + task.split("/")[-1] + " | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Exceptions and exclusions",
        "",
        "An asterisk marks a recorded exception. These outcomes remain in the denominator.",
        "",
    ]
    for r in rows:
        if r["error"]:
            lines.append(
                f"- {r['agent']} / {r['model'].split('/')[-1]} / {r['task'].split('/')[-1]}: {r['error']}."
            )
    lines += [
        "",
        "Two new Opus OpenCode attempts failed before model execution because the runner omitted limit.context.",
        "Their replacements are selected by task name, not by reward. The originals remain in the evidence manifest.",
        "The earlier 32k Opus smoke is not part of this comparison. No result files were overwritten.",
        "",
        "## Configuration and interpretation",
        "",
        "All pairs use medium reasoning effort. New OpenCode Opus trials use a 128,000-token output cap and a 1,000,000-token context limit.",
        "GPT and r3 Claude Code retain their recorded configurations. These are not identical output-budget comparisons.",
        "Claude plugins: @gotgenes/pi-anthropic-auth@3.4.3 and @ex-machina/opencode-anthropic-auth@1.8.6.",
        "Each task has a separate verifier container. Scoring checks artifacts and behavior, not the agent's completion claim.",
        "Costs use the existing metrics implementation: reported agent cost when present, otherwise the pinned API price estimate.",
        "Costs are API equivalents, not subscription invoices. Reported costs can use different provider accounting conventions.",
        "Times cover agent execution, excluding setup and verification. Excluded attempts are not included in scored totals.",
        "There is one trial per pair and task. Do not infer statistical superiority from these small differences.",
        "OpenCode's r3 hang has unresolved transport-versus-harness attribution. Provider refusals are not environment failures.",
        "",
        "## Reproduction",
        "",
        "Run `python -m ctb.report` from the repository with its environment.",
        "The generator verifies sealed r3 result hashes and each new reward file before rendering.",
        "Outputs use sorted inputs, fixed SVG geometry and no clock, random IDs, external fonts or network requests.",
        "SHA256SUMS covers data.json, REPORT.md and all three SVG files. manifest.json also records evidence hashes.",
        "The artifact lock proves byte integrity, not that the trials can be rerun with identical model outputs.",
        "",
    ]
    outputs = {
        "data.json": encode(data),
        "REPORT.md": "\n".join(lines),
        "passes.svg": chart(groups, "passed", "Passed tasks out of 12", ""),
        "cost.svg": chart(groups, "cost_usd", "API-equivalent cost", "USD"),
        "time.svg": chart(groups, "agent_minutes", "Agent execution time", "min"),
    }
    manifest = {
        "schema_version": 1,
        "status": data["status"],
        "evidence": files,
        "artifacts": {name: digest(text.encode()) for name, text in sorted(outputs.items())},
    }
    outputs["manifest.json"] = encode(manifest)
    outputs["SHA256SUMS"] = "".join(
        f"{digest(text.encode())}  {name}\n" for name, text in sorted(outputs.items())
    )
    return outputs


def main():
    outputs = build()
    directory = ROOT / OUTPUT
    directory.mkdir(exist_ok=True)
    for name, text in outputs.items():
        path = directory / name
        if path.exists() and path.read_text() != text:
            raise SystemExit(f"Refusing to overwrite changed artifact: {path}")
    for name, text in outputs.items():
        (directory / name).write_text(text)
    print(f"Verified and wrote {len(outputs)} deterministic artifacts in {OUTPUT}")


if __name__ == "__main__":
    main()

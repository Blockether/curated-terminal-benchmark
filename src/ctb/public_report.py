"""Build a public, closed-schema comparison. Never load transcripts or raw trials."""

import argparse
import hashlib
import html
import json
import re
from pathlib import Path

SELECTION = {
    "vis-solo": "gpt-vis-solo-053-6e1a71a",
    "codex": "gpt-codex-dirge-c30ea7d",
    "dirge": "gpt-codex-dirge-c30ea7d",
    "opencode": "gpt-opencode-03-c30ea7d",
    "kmet": "gpt-sequential-c30ea7d",
    "samizdat": "gpt-sequential-c30ea7d",
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
    require(type(data["schema"]) is int and data["schema"] == 1)
    keys(data["sources"], SELECTION)
    for agent, source in data["sources"].items():
        keys(source, ("round", "seal_sha256", "harness_commit"))
        require(source["round"] == SELECTION[agent])
        sha(source["seal_sha256"])
        sha(source["harness_commit"], 40)
    require(type(data["rows"]) is list and len(data["rows"]) == 72)
    seen = set()
    for row in data["rows"]:
        keys(row, ("agent", "task", "passed", "error", "result_sha256"))
        require(type(row["agent"]) is str and row["agent"] in SELECTION)
        require(type(row["task"]) is str and row["task"] in TASKS)
        require(type(row["passed"]) is bool)
        require(row["error"] is None or type(row["error"]) is str)
        require(row["error"] in ERRORS)
        sha(row["result_sha256"])
        pair = row["agent"], row["task"]
        require(pair not in seen)
        seen.add(pair)
    require(seen == {(a, t) for a in SELECTION for t in TASKS})
    return data


def from_seals(root):
    data = {"schema": 1, "sources": {}, "rows": []}
    for agent, name in SELECTION.items():
        seal = read(root / "results" / (name + ".json"))
        require(seal["digest"] == digest(seal) and seal["round"] == name)
        runs = seal["runs"]
        require(bool(runs) and all(r["dirty"] is False for r in runs))
        commits = {r["commit"] for r in runs}
        require(len(commits) == 1)
        data["sources"][agent] = {
            "round": name,
            "seal_sha256": seal["digest"],
            "harness_commit": next(iter(commits)),
        }
        for row in seal["rows"]:
            if row["agent"] != agent:
                continue
            require(row["model"] == "openai/gpt-6.1-sol")
            require(type(row["reward"]) in (int, float, type(None)))
            require(row["reward"] in (None, 0, 1))
            task = row["task"]
            require(task.startswith("terminal-bench/"))
            data["rows"].append(
                {
                    "agent": agent,
                    "task": task.removeprefix("terminal-bench/"),
                    "passed": row["reward"] == 1,
                    "error": row["error"],
                    "result_sha256": row["result_sha256"],
                }
            )
    return validate(data)


def encoded(data):
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()


def render(data):
    validate(data)
    ranking = sorted(
        SELECTION, key=lambda a: (-sum(r["passed"] for r in data["rows"] if r["agent"] == a), a)
    )
    summary = []
    for agent in ranking:
        rows = [r for r in data["rows"] if r["agent"] == agent]
        count = sum(r["passed"] for r in rows)
        errors = sum(r["error"] is not None for r in rows)
        summary.append(
            f"<tr><th>{agent}</th><td>{count}/12</td><td>{count / 12:.1%}</td>"
            f"<td>{errors}</td></tr>"
        )
    matrix = []
    lookup = {(r["agent"], r["task"]): r for r in data["rows"]}
    for task in TASKS:
        cells = []
        for agent in ranking:
            row = lookup[agent, task]
            label = "PASS" if row["passed"] else "FAIL"
            if row["error"]:
                label += " *"
            cells.append(f'<td class="{label.split()[0].lower()}">{label}</td>')
        matrix.append(f"<tr><th>{task}</th>{''.join(cells)}</tr>")
    sources = "".join(
        f"<tr><th>{a}</th><td>{s['round']}</td><td>{s['harness_commit']}</td>"
        f"<td>{s['seal_sha256']}</td></tr>"
        for a, s in data["sources"].items()
    )
    return (
        """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Vis vs terminal agents</title><style>
body{font:16px system-ui,sans-serif;background:#111827;color:#e5e7eb;margin:0;padding:4vw}
main{max-width:1280px;margin:auto}h1{font-size:2.6rem}a{color:#93c5fd}
table{border-collapse:collapse;width:100%;margin:24px 0}th,td{padding:12px;text-align:left;border-bottom:1px solid #374151}
.pass{color:#86efac}.fail{color:#fca5a5}.scroll{overflow:auto}p{line-height:1.6}.note{border-left:4px solid #fbbf24;padding:16px;background:#1f2937}
.hashes{font-size:12px} @media print{body{background:white;color:black}.scroll{overflow:visible}th,td{padding:5px}.hashes{word-break:break-all}}
</style></head><body><main><h1>Vis vs terminal agents</h1>
<p>Vis 0.5.3 solo · GPT-6.1-sol · medium effort · 12 tasks per agent · 2 hours per task.</p>
<p class="note">Latest complete round per agent. The source results are sealed, not certified as fair or error-free.
Vis used a newer harness with verifier upload, photonic rendering and health-check fixes.
Other agents used c30ea7d. Recovery trials and older rounds are not substituted.
This comparison does not use one identical harness revision.</p>
<h2>Results</h2><table><thead><tr><th>Agent</th><th>Passed</th><th>Rate</th><th>Exceptions</th></tr></thead><tbody>"""
        + "".join(summary)
        + "</tbody></table><h2>Tasks</h2><p>* The trial recorded an exception. A PASS can coexist with an exception.</p><div class=scroll><table><thead><tr><th>Task</th>"
        + "".join(f"<th>{html.escape(a)}</th>" for a in ranking)
        + "</tr></thead><tbody>"
        + "".join(matrix)
        + "</tbody></table></div>"
        + "<h2>Sealed sources</h2><div class=scroll><table class=hashes><thead><tr><th>Agent</th><th>Round</th><th>Harness commit</th><th>Seal SHA-256</th></tr></thead><tbody>"
        + sources
        + "</tbody></table></div><p><a href=comparison.json>Download public result data</a></p>"
        + "<p>Only result fields and hashes are published. Transcripts, prompts, logs, credentials and task artifacts stay local. No analytics or external assets are loaded.</p></main></body></html>\n"
    ).encode()


def expected(data):
    return {"index.html": render(data), "comparison.json": encoded(data)}


def check_output(out, data):
    require(not any(p.is_symlink() for p in (out, *out.parents)))
    files = list(out.rglob("*"))
    require({p.relative_to(out).as_posix() for p in files} == set(expected(data)))
    for name, raw in expected(data).items():
        path = out / name
        require(path.is_file() and not path.is_symlink() and path.read_bytes() == raw)


def build(root, out, check=False):
    data = validate(read(root / "public/comparison.json"))
    require(data == from_seals(root))
    require(not any(p.is_symlink() for p in (out, *out.parents)))
    if not check:
        # Refuse a reused or contaminated output rather than upload stale content.
        require(not out.exists())
        out.mkdir(parents=True)
        for name, raw in expected(data).items():
            (out / name).write_bytes(raw)
    check_output(out, data)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        build(args.root, args.out, args.check)
    except (ValueError, KeyError, TypeError, OSError):
        raise SystemExit("Public report refused. No artifact may be uploaded.") from None
    print("Public report checked: 72 trials; two allowlisted files.")


if __name__ == "__main__":
    main()

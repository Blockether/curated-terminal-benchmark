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

SELECTION = {
    "codex": "gpt-bench-b2a4d4a",
    "kmet": "gpt-kmet-2-16bbdc8",
    "opencode": "gpt-bench-b2a4d4a",
    "pi": "gpt-bench-b2a4d4a",
    "vis-solo": "gpt-vis-solo-057-43a8048",
    "wisp": "gpt-wisp-ab49003",
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
        keys(source, ("round", "seal_sha256", "harness_commit", "agent_version"))
        require(source["round"] == SELECTION[agent])
        version = source["agent_version"]
        require(type(version) is str and re.fullmatch("[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}", version))
        sha(source["seal_sha256"])
        sha(source["harness_commit"], 40)
    require(type(data["rows"]) is list and len(data["rows"]) == len(SELECTION) * len(TASKS))
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
        versions = {r["agent_version"] for r in seal["rows"] if r["agent"] == agent}
        require(len(versions) == 1)
        data["sources"][agent] = {
            "round": name,
            "seal_sha256": seal["digest"],
            "harness_commit": next(iter(commits)),
            "agent_version": next(iter(versions)),
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


# Only these tool labels can enter the public metrics, never arbitrary log text.
TOOLS = frozenset(
    [
        "__no_call__",
        "__parse_error__",
        "__provider_error__",
        "apply_patch",
        "ask_human",
        "bash",
        "bash_output",
        "done",
        "edit",
        "edit_file",
        "exec",
        "execution",
        "fetch_turn",
        "glob",
        "grep",
        "kill_shell",
        "list_dir",
        "load_image",
        "message",
        "papercut",
        "patch",
        "plan",
        "python3",
        "python_execution",
        "read",
        "read_digest",
        "read_file",
        "read_minified",
        "repo_overview",
        "request_user_input_async",
        "resource",
        "run_code",
        "shell",
        "skill",
        "split",
        "task",
        "tool_search",
        "write",
        "write_file",
        "write_todo_list",
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
        for key in ("provider_calls", "provider_timed", "tool_calls", "provider_errors"):
            require(m[key] is not None)
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
        require(
            m["tool_errors"] is None
            or m["tool_errors"] == sum(t["errors"] for t in m["tools"].values())
        )
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
    for agent, name in SELECTION.items():
        seal = read(root / "results" / (name + ".json"))
        for row in seal["rows"]:
            if row["agent"] != agent:
                continue
            path = root / row["result"]
            require(path.resolve().is_relative_to((root / "jobs").resolve()))
            require(hashlib.sha256(path.read_bytes()).hexdigest() == row["result_sha256"])
            trial = load(path.parent, root / "jobs")
            facts, summary = layout.facts(trial), trial["summary"]
            runs = [r for r in seal["runs"] if agent in r["agents"].split(",")]
            identities = {setup_id(r) for r in runs}
            require(len(identities) == 1 and None not in identities)
            metrics[row["result_sha256"]] = {
                "task_checksum": row["task_checksum"],
                "setup": next(iter(identities)),
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
    for agent, name in SELECTION.items():
        for row in read(root / "results" / (name + ".json"))["rows"]:
            if row["agent"] == agent:
                m = metrics[row["result_sha256"]]
                require(m["task_checksum"] == row["task_checksum"])
                runs = [
                    r
                    for r in read(root / "results" / (name + ".json"))["runs"]
                    if agent in r["agents"].split(",")
                ]
                require({setup_id(r) for r in runs} == {m["setup"]})
                require(m["reward"] == row["reward"] and m["started_at"] == row["started_at"])
    return metrics


def public_trials(data, metrics):
    validate(data)
    validate_metrics(metrics, data)
    trials = []
    for r in data["rows"]:
        m = metrics[r["result_sha256"]]
        trials.append(
            {
                **r,
                "id": r["result_sha256"],
                "task_checksum": m["task_checksum"],
                "setup": m["setup"],
                "model": "openai/gpt-6.1-sol",
                "round": data["sources"][r["agent"]]["round"],
                "reward": m["reward"],
                "started_at": m["started_at"],
                "version": data["sources"][r["agent"]]["agent_version"],
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
    "wisp": ("Wisp", False, "https://github.com/xificurC/wisp"),
    "dirge": ("Dirge", True, "https://github.com/dirge-code/dirge"),
    "samizdat": ("Samizdat", True, "https://github.com/samizdat-code/samizdat"),
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
EMULATION = (
    "Run under Rosetta emulation on a MacBook Pro M4 Max (Podman VM, 12 CPUs, 24 GiB). "
    "Times include the emulation overhead."
)
FONT_FILES = ("inter-latin.woff2", "jetbrains-mono-latin.woff2")


def agent_names():
    """The display names of the selected agents, in the order of AGENTS."""
    chosen = {name.split("-")[0] for name in SELECTION}
    return [label for key, (label, _, _) in AGENTS.items() if key in chosen]


def description():
    names = agent_names()
    return (
        f"{', '.join(names[:-1])} and {names[-1]} on {len(TASKS)} CPU-only Terminal-Bench 4.0 "
        "tasks with GPT-6.1-sol: pass rates, cost, tokens, run time and sanitized transcripts."
    )


def head(data):
    """The page head: one lead line, then the tested versions, the paused agents and the sources."""
    # A sealed version can carry the program name ("vis-agent 0.5.7"); the list shows the name.
    versions = {a.split("-")[0]: s["agent_version"].split()[-1] for a, s in data["sources"].items()}

    def agent(key):
        label, _, url = AGENTS[key]
        version = (
            f' <span class="version">{layout.esc(versions[key])}</span>' if key in versions else ""
        )
        return f'<li><a href="{url}">{label}</a>{version}</li>'

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
    intro = (
        f"Our agent Vis vs. {len(versions) - 1} other coding agents on {len(TASKS)} CPU-only "
        "Terminal-Bench 4.0 tasks, all on GPT-6.1-sol with medium reasoning."
    )
    return (
        f'<section class="head">\n<h1>{SITE_NAME}</h1>\n<p class="lead">{intro}</p>\n'
        f'<dl class="head-facts">{facts}</dl>\n'
        f'<p class="head-note">{EMULATION}</p>\n</section>'
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
    except (ValueError, KeyError, TypeError, OSError):
        raise SystemExit("Public report refused. No artifact may be uploaded.") from None
    print(
        f"Public report checked: {len(SELECTION) * len(TASKS)} trials; exact report assets and metrics."
    )


if __name__ == "__main__":
    main()

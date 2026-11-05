"""Export scrubbed text events locally; check the exact public site in CI."""

import argparse
import base64
import gzip
import hashlib
import io
import json
import re
import subprocess
import urllib.parse
import zlib
from pathlib import Path

from ctb import public_report as report

STATIC = Path(__file__).with_name("publication_static")
KINDS = {"user", "assistant", "reasoning", "call", "tool", "command", "result"}
FIELDS = ("name", "text", "input", "output")
REDACTED = "[REDACTED]"
MAX_GZIP = 2 * 1024**2
MAX_EXPANDED = 16 * 1024**2
MAX_TOTAL = 128 * 1024**2
# Free text is untrusted. These rules deliberately remove some benign examples too.
RULES = tuple(
    re.compile(p)
    for p in (
        r"(?im)\b(?:set-cookie|cookie|authorization|proxy-authorization)\s*:\s*[^\r\n]+",
        r"(?i)\b[A-Za-z0-9_.-]*(?:token|secret|password|passwd|credential|api[_-]?key|access[_-]?key|session[_-]?key|private[_-]?key)[A-Za-z0-9_.-]*[\\\"']*\s*[:=]\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}]+)",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?",
        r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|AKIA[A-Z0-9]{16})\b",
        r"(?i)\b(?:Bearer|Basic)\s+(?!\[REDACTED\])[A-Za-z0-9+/_.=\-]{8,}",
        r"(?i)(?:access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|client[_-]?secret|password|authorization|cookie|encrypted_content|reasoning_content)[\"']?\s*[:=]\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}]+)",
        r"(?i)https?://[^\s/:]+:[^\s/@]+@[^\s]+",
        r"(?i)/(?:Users|home)/[^/\s\"']+",
    )
)


def secret_variants(known):
    values = set()
    for value in known:
        if len(value) < 8:
            continue
        values.update(
            (
                value,
                json.dumps(value)[1:-1],
                urllib.parse.quote(value, safe=""),
                base64.b64encode(value.encode()).decode(),
                base64.urlsafe_b64encode(value.encode()).decode(),
            )
        )
    return sorted(values, key=len, reverse=True)


def scrub(text, known=()):
    if text is None:
        return None
    report.require(type(text) is str)
    for value in known:
        text = text.replace(value, REDACTED)
    for rule in RULES:
        text = rule.sub(REDACTED, text)
    return text


def validate_payload(data, result_hash, known=()):
    report.keys(data, ("schema", "result_sha256", "events"))
    report.require(type(data["schema"]) is int and data["schema"] == 1)
    report.require(data["result_sha256"] == result_hash)
    report.require(type(data["events"]) is list and 0 < len(data["events"]) <= 20_000)
    for event in data["events"]:
        report.keys(event, ("kind", *FIELDS))
        report.require(type(event["kind"]) is str and event["kind"] in KINDS)
        for key in FIELDS:
            value = event[key]
            report.require(value is None or type(value) is str and len(value) <= 1_000_000)
            report.require(scrub(value, known) == value)
    return data


def decode(raw):
    def pairs(items):
        out = {}
        for k, v in items:
            report.require(k not in out)
            out[k] = v
        return out

    return json.loads(raw, object_pairs_hook=pairs)


def pack(raw):
    stream = io.BytesIO()
    with gzip.GzipFile(fileobj=stream, mode="wb", filename="", compresslevel=9, mtime=0) as archive:
        archive.write(raw)
    return stream.getvalue()


def unpack(raw):
    report.require(0 < len(raw) <= MAX_GZIP)
    report.require(raw[:10] == b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff")
    decompressor = zlib.decompressobj(wbits=31)
    expanded = decompressor.decompress(raw, MAX_EXPANDED + 1)
    report.require(len(expanded) <= MAX_EXPANDED and decompressor.eof)
    report.require(not decompressor.unused_data and not decompressor.unconsumed_tail)
    return expanded


def content_hash(raw):
    return hashlib.sha256(raw).hexdigest()


def manifest_check(manifest, comparison):
    report.keys(manifest, ("schema", "transcripts"))
    report.require(type(manifest["schema"]) is int and manifest["schema"] == 1)
    expected = {r["result_sha256"] for r in comparison["rows"]}
    report.keys(manifest["transcripts"], expected)
    for record in manifest["transcripts"].values():
        report.keys(record, ("sha256", "bytes", "expanded_bytes", "events"))
        report.sha(record["sha256"])
        for key, maximum in (
            ("bytes", MAX_GZIP),
            ("expanded_bytes", MAX_EXPANDED),
            ("events", 20_000),
        ):
            report.require(type(record[key]) is int and 0 < record[key] <= maximum)
    report.require(sum(r["expanded_bytes"] for r in manifest["transcripts"].values()) <= MAX_TOTAL)


def static_files(comparison, manifest):
    index = report.render(comparison).decode()
    links = "".join(
        f'<li><a href="transcript.html?id={r["result_sha256"]}">{r["agent"]}: {r["task"]}</a></li>'
        for r in comparison["rows"]
    )
    index = index.replace(
        "</main>",
        "<h2>Sanitized transcripts</h2><p>Text only. "
        "Credential-like values and local user paths are removed. "
        "This is a normalized view, not a complete raw log. "
        "One gzip file is fetched when you open a trial.</p><ul>" + links + "</ul></main>",
    )
    index = index.replace(
        "Transcripts, prompts, logs, credentials and task artifacts stay local.",
        "Raw logs, credentials and task artifacts stay local. Sanitized transcripts are linked below.",
    )
    return {
        "index.html": index.encode(),
        "comparison.json": report.encoded(comparison),
        "transcripts.json": report.encoded(manifest),
        "transcript.html": (STATIC / "transcript.html").read_bytes(),
        "viewer.js": (STATIC / "viewer.js").read_bytes(),
    }


def checked_inputs(root):
    comparison = report.validate(report.read(root / "public/comparison.json"))
    report.require(comparison == report.from_seals(root))
    manifest = report.read(root / "public/transcript-hashes.json")
    manifest_check(manifest, comparison)
    return comparison, manifest


def check(root, out, known=()):
    comparison, manifest = checked_inputs(root)
    report.require(not any(p.is_symlink() for p in (out, *out.parents)))
    fixed = static_files(comparison, manifest)
    wanted = (
        set(fixed) | {"transcripts"} | {f"transcripts/{h}.json.gz" for h in manifest["transcripts"]}
    )
    paths = list(out.rglob("*"))
    report.require({p.relative_to(out).as_posix() for p in paths} == wanted)
    report.require(not any(p.is_symlink() for p in paths))
    for name, raw in fixed.items():
        report.require((out / name).is_file() and (out / name).read_bytes() == raw)
    for result_hash, row in manifest["transcripts"].items():
        path = out / "transcripts" / (result_hash + ".json.gz")
        report.require(path.is_file() and path.stat().st_size == row["bytes"])
        raw = path.read_bytes()
        report.require(content_hash(raw) == row["sha256"])
        expanded = unpack(raw)
        report.require(len(expanded) == row["expanded_bytes"])
        data = validate_payload(decode(expanded), result_hash, known)
        report.require(len(data["events"]) == row["events"])
        # No gzip filename, timestamp or trailing payload can hide data outside the JSON.
        report.require(expanded == report.encoded(data))


def export(root, out):
    # Dependencies and credential files are used locally, never by the CI checker.
    from ctb.privacy import secret_values
    from ctb.report import transcripts

    comparison = report.validate(report.read(root / "public/comparison.json"))
    report.require(comparison == report.from_seals(root))
    known = secret_variants(secret_values())
    report.require(not out.exists() and not any(p.is_symlink() for p in out.parents))
    (out / "transcripts").mkdir(parents=True)
    manifest = {"schema": 1, "transcripts": {}}
    seals = {
        r: report.read(root / "results" / (r + ".json")) for r in set(report.SELECTION.values())
    }
    # Do not clip text before sanitization; a clipped credential can escape a full-token scan.
    transcripts.MAX_TEXT = 1_000_000_000
    for row in comparison["rows"]:
        name = report.SELECTION[row["agent"]]
        source = next(s for s in seals[name]["rows"] if s["result_sha256"] == row["result_sha256"])
        result_path = root / source["result"]
        report.require(content_hash(result_path.read_bytes()) == row["result_sha256"])
        events = transcripts.READERS[transcripts.harness(row["agent"])](
            result_path.parent / "agent", "openai/gpt-6.1-sol"
        )
        if not events:
            result = report.read(result_path)
            report.require(
                result.get("agent_execution") is None
                and result.get("agent_setup") is None
                and bool(result.get("exception_info"))
            )
            events = [
                {
                    "kind": "result",
                    "text": "The agent did not start. The original trial failed during environment setup.",
                }
            ]
        payload = {
            "schema": 1,
            "result_sha256": row["result_sha256"],
            "events": [
                {"kind": e["kind"], **{k: scrub(e.get(k), known) for k in FIELDS}} for e in events
            ],
        }
        validate_payload(payload, row["result_sha256"], known)
        expanded = report.encoded(payload)
        report.require(len(expanded) <= MAX_EXPANDED)
        raw = pack(expanded)
        report.require(len(raw) <= MAX_GZIP)
        (out / "transcripts" / (row["result_sha256"] + ".json.gz")).write_bytes(raw)
        manifest["transcripts"][row["result_sha256"]] = {
            "sha256": content_hash(raw),
            "bytes": len(raw),
            "expanded_bytes": len(expanded),
            "events": len(events),
        }
    manifest_check(manifest, comparison)
    # The caller stages this hash-only manifest on main after the local audit.
    (root / "public/transcript-hashes.json").write_bytes(report.encoded(manifest))
    for name, raw in static_files(comparison, manifest).items():
        (out / name).write_bytes(raw)
    check(root, out, known)


def stage(root, out):
    """Check only the intended public files, including their staged bytes and modes."""
    check(root, out)
    branch = out.parent
    names = {p.relative_to(branch).as_posix() for p in out.rglob("*") if p.is_file()}
    workflow = ".github/workflows/pages.yml"
    names.add(workflow)
    require_workflow = (root / workflow).read_bytes()
    report.require((branch / workflow).read_bytes() == require_workflow)
    records = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=branch)
    entries = {}
    for raw in records.split(b"\0"):
        if not raw:
            continue
        meta, path = raw.split(b"\t", 1)
        mode, _, number = meta.split()
        report.require(mode == b"100644" and number == b"0")
        entries[path.decode()] = subprocess.check_output(
            ["git", "show", ":" + path.decode()], cwd=branch
        )
    report.require(set(entries) == names)
    for name, raw in entries.items():
        report.require((branch / name).read_bytes() == raw)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("command", choices=("export", "check", "stage"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "export":
            export(args.root, args.out)
        elif args.command == "stage":
            stage(args.root, args.out)
        else:
            check(args.root, args.out)
    except (
        ValueError,
        KeyError,
        TypeError,
        OSError,
        EOFError,
        zlib.error,
        subprocess.CalledProcessError,
    ):
        raise SystemExit(
            "Publication refused. Inspect locally; do not upload this directory."
        ) from None
    print("Publication checked: 72 sanitized transcripts and exact site files.")


if __name__ == "__main__":
    main()

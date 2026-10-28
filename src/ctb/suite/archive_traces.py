"""Recompress finished Harbor traces losslessly, keeping interrupted gzip files."""

import gzip
import hashlib
import json
import subprocess
from pathlib import Path
from typing import BinaryIO

from ctb.suite import SUITE

JOBS = SUITE / "jobs"
CHUNK = 1024 * 1024


def open_trace(path: Path) -> BinaryIO:
    """The raw JSONL bytes of a gzip or a plain trace."""
    return gzip.open(path, "rb") if path.suffix == ".gz" else path.open("rb")


def trace_digest(path: Path) -> tuple[str, int]:
    """Read a gzip trace to its checksum, so incomplete streams cannot be archived.

    A plain JSONL trace has no checksum: its bytes are archived as they are.
    """
    digest = hashlib.sha256()
    size = 0
    with open_trace(path) as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def compress_trace(source: Path, target: Path) -> None:
    """Pipe raw JSONL into long-window zstd without materializing it."""
    with target.open("wb") as output:
        process = subprocess.Popen(
            ["zstd", "-T2", "-19", "--long=27", "-q", "-c"],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.DEVNULL,
        )
        try:
            with open_trace(source) as stream:
                for chunk in iter(lambda: stream.read(CHUNK), b""):
                    process.stdin.write(chunk)
            process.stdin.close()
            if process.wait() != 0:
                raise RuntimeError("zstd compression failed; the trace is preserved")
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise


def zstd_digest(path: Path) -> tuple[str, int]:
    """Verify the recompressed JSONL by reading the complete zstd stream."""
    process = subprocess.Popen(
        ["zstd", "-dc", "--", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    digest = hashlib.sha256()
    size = 0
    with process.stdout as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    if process.wait() != 0:
        raise RuntimeError("zstd verification failed; the trace is preserved")
    return digest.hexdigest(), size


def archive_trace(source: Path) -> dict:
    """Delete a trace only after its complete raw stream matches the zstd archive."""
    expected = trace_digest(source)
    target = source.with_name(source.name.removesuffix(".gz") + ".zst")
    temporary = target.with_name(target.name + ".tmp")
    if temporary.exists():
        raise FileExistsError(f"Remove or inspect the unfinished archive: {temporary}")
    created = not target.exists()
    try:
        if created:
            compress_trace(source, temporary)
        candidate = temporary if created else target
        if zstd_digest(candidate) != expected:
            raise RuntimeError("Trace digests differ; the trace is preserved")
        if created:
            temporary.replace(target)
    finally:
        if created and temporary.exists():
            temporary.unlink()
    manifest = {
        "trace": target.name,
        "raw_sha256": expected[0],
        "raw_bytes": expected[1],
        "original_format": "gzip" if source.suffix == ".gz" else "jsonl",
        "original_bytes": source.stat().st_size,
        "archived_zstd_bytes": target.stat().st_size,
        "compression": "zstd -T2 -19 --long=27",
    }
    info = source.with_name("trace-archive.json")
    pending = info.with_name(info.name + ".tmp")
    pending.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    pending.replace(info)
    source.unlink()
    return manifest


def main() -> None:
    archived = 0
    incomplete = 0
    for path in sorted(JOBS.glob("*/*/result.json")):
        if not json.loads(path.read_text(encoding="utf-8")).get("finished_at"):
            continue
        names = ("agent/vis-trace.jsonl.gz", "agent/vis-trace.jsonl")
        source = next((path.parent / n for n in names if (path.parent / n).is_file()), None)
        if source is None:
            continue
        try:
            manifest = archive_trace(source)
        except (EOFError, gzip.BadGzipFile):
            incomplete += 1
            print(f"Preserved incomplete gzip: {path.parent.parent.name}/{path.parent.name}")
            continue
        archived += 1
        print(
            f"Archived {path.parent.parent.name}/{path.parent.name}: "
            f"{manifest['original_bytes']} -> {manifest['archived_zstd_bytes']} bytes"
        )
    print(f"Archived {archived}; preserved incomplete gzip: {incomplete}")


if __name__ == "__main__":
    main()

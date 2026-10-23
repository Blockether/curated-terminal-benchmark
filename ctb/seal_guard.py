"""Check staged seals and scan their evidence before a local commit."""

import bz2
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from ctb.privacy import secret_values


class UnsafeSeal(ValueError):
    """The seal or its evidence cannot pass the audit."""


PATTERNS = {
    "JWT": rb"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}",
    "provider key": rb"(?<![A-Za-z0-9_-])(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16})",
    "private key": rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "bearer": rb"(?i)bearer[ \t]+[A-Za-z0-9_./+~-]{24,}",
    "credential assignment": rb"(?i)[\"']?(?:access_token|refresh_token|api_key|client_secret|password)[\"']?\s*[:=]\s*[\"'](?!\[REDACTED\])[^\"'\r\n]{16,}[\"']",
}
RULES = {name: re.compile(pattern) for name, pattern in PATTERNS.items()}
MARKERS = {
    "JWT": (b"eyj",),
    "provider key": (b"sk-", b"ghp_", b"gho_", b"ghu_", b"ghs_", b"ghr_", b"github_pat_", b"akia"),
    "private key": (b"private key",),
    "bearer": (b"bearer",),
    "credential assignment": (
        b"access_token",
        b"refresh_token",
        b"api_key",
        b"client_secret",
        b"password",
    ),
}
MAX_EXPANDED_BYTES = 16 * 1024**3


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root)


def safe_path(root, name):
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise UnsafeSeal("Evidence path escapes the checkout")
    target = root / path
    if any(p.is_symlink() for p in [target, *target.parents] if p != root.parent):
        raise UnsafeSeal("Evidence contains a symbolic link")
    if not target.is_file() or not target.resolve().is_relative_to(root.resolve()):
        raise UnsafeSeal("Evidence file is missing or outside the checkout")
    return target


def digest(body):
    return hashlib.sha256(
        json.dumps({k: v for k, v in body.items() if k != "digest"}, sort_keys=True).encode()
    ).hexdigest()


def verify_digest(body):
    if not isinstance(body, dict) or body.get("digest") != digest(body):
        raise UnsafeSeal("Seal digest does not match")


def scan_stream(stream, known, budget):
    tail = b""
    overlap = max(16384, max(map(len, known), default=0))
    while block := stream.read(1024 * 1024):
        previous = budget[0]
        budget[0] += len(block)
        if budget[0] // (128 * 1024**2) != previous // (128 * 1024**2):
            print(f"Seal audit: scanned {budget[0] // 1024**2} MiB including archives.", flush=True)
        if budget[0] > MAX_EXPANDED_BYTES:
            raise UnsafeSeal("Scan size limit exceeded; audit incomplete")
        data = tail + block
        if any(value in data for value in known):
            raise UnsafeSeal("Known credential found")
        lower = data.lower()
        for name, rule in RULES.items():
            if any(marker in lower for marker in MARKERS[name]) and rule.search(data):
                raise UnsafeSeal(f"Potential {name} found")
        tail = data[-overlap:]


def scan_file(path, known, budget):
    with path.open("rb") as stream:
        scan_stream(stream, known, budget)
    if path.name.endswith((".tgz", ".tar.gz", ".tar", ".tar.bz2")):
        with tarfile.open(path, "r|*") as archive:
            for member in archive:
                if member.isfile():
                    with archive.extractfile(member) as stream:
                        scan_stream(stream, known, budget)
                elif not member.isdir():
                    raise UnsafeSeal("Unsupported archive member; audit incomplete")
    elif path.suffix in {".zip", ".mxl"}:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                with archive.open(member) as stream:
                    scan_stream(stream, known, budget)
    elif path.suffix in {".gz", ".bz2"}:
        opener = gzip.open if path.suffix == ".gz" else bz2.open
        with opener(path, "rb") as stream:
            scan_stream(stream, known, budget)


def verify_files(root, manifest, known, budget):
    verify_digest(manifest)
    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise UnsafeSeal("Transcript manifest has no files")
    names = set()
    for index, row in enumerate(records):
        if index % 100 == 0:
            print(f"Seal audit: checking file {index + 1}/{len(records)}.", flush=True)
        name = row["path"]
        if name in names:
            raise UnsafeSeal("Duplicate evidence path")
        names.add(name)
        if row.get("type") == "symlink":
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise UnsafeSeal("Link path escapes the checkout")
            path = root / relative
            if not path.is_symlink() or any(p.is_symlink() for p in path.parents):
                raise UnsafeSeal("Invalid link evidence")
            target = os.readlink(path).encode()
            if target.decode() != row["target"] or len(target) != row["bytes"]:
                raise UnsafeSeal("Link target changed")
            if hashlib.sha256(target).hexdigest() != row["sha256"]:
                raise UnsafeSeal("Link hash changed")
            scan_stream(io.BytesIO(target), known, budget)
            continue
        path = safe_path(root, name)
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != row["sha256"] or path.stat().st_size != row["bytes"]:
            raise UnsafeSeal(f"Evidence hash or size changed: {name}")
        try:
            scan_file(path, known, budget)
        except UnsafeSeal as error:
            raise UnsafeSeal(f"{name}: {error}") from None
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != actual:
                raise UnsafeSeal("Evidence changed during the scan")
    return names


def validate(root, staged, known):
    """Validate staged JSON bodies, not unstaged versions of the seals."""
    seals = []
    manifests = []
    for name, raw in staged.items():
        if not name.endswith(".json"):
            continue
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            if name.startswith("results/") or "seal" in Path(name).name:
                raise UnsafeSeal("Invalid JSON in a seal candidate") from None
            continue
        if not isinstance(body, dict):
            continue
        if "digest" in body or "rows" in body and "sealed_at" in body:
            verify_digest(body)
            if "files" in body:
                manifests.append(body)
            elif "rows" in body:
                seals.append(body)
            else:
                raise UnsafeSeal("Unknown seal schema")
    if not seals and not manifests:
        return 0
    if seals and not manifests:
        raise UnsafeSeal("Stage a full transcript manifest together with the result seal")
    budget = [0]
    covered = set()
    for manifest in manifests:
        covered |= verify_files(root, manifest, known, budget)
        if not manifest.get("seals"):
            raise UnsafeSeal("Manifest must reference result seals")
        for reference in manifest["seals"]:
            name = f"results/{reference['round']}.json"
            if name not in covered:
                raise UnsafeSeal("Referenced seal is absent from the manifest")
            raw = safe_path(root, name).read_bytes()
            if name in staged and staged[name] != raw:
                raise UnsafeSeal("Staged seal differs from the audited file")
            linked = json.loads(raw)
            verify_digest(linked)
            if linked["digest"] != reference["digest"]:
                raise UnsafeSeal("Referenced seal digest changed")
            seals.append(linked)
    # A manifest must include every file under each trial, including hidden files.
    result_names = {name for name in covered if name.endswith("/result.json")}
    for seal in seals:
        if not seal.get("rows") or not seal.get("runs"):
            raise UnsafeSeal("Result seal is empty")
        if any(run.get("dirty", True) for run in seal["runs"]):
            raise UnsafeSeal("Result seal contains a dirty run")
        for row in seal["rows"]:
            result_names.add(row["result"])
            if row["result"] not in covered:
                raise UnsafeSeal("Result is absent from transcript manifest")
            path = safe_path(root, row["result"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["result_sha256"]:
                raise UnsafeSeal("Result hash differs from its seal")
    for name in result_names:
        path = safe_path(root, name)
        body = json.loads(path.read_bytes())
        if "trial_name" not in body:
            continue
        agent = path.parent / "agent"
        if not agent.is_dir() or not any(p.is_file() for p in agent.rglob("*")):
            raise UnsafeSeal("Trial has no agent evidence")
        for child in path.parent.rglob("*"):
            if child.is_symlink():
                if child.relative_to(root).as_posix() not in covered:
                    raise UnsafeSeal("Trial evidence contains an unrecorded symbolic link")
                continue
            if child.is_file() and child.relative_to(root).as_posix() not in covered:
                raise UnsafeSeal("Transcript manifest omits a trial file")
    return len(covered)


def main():
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
    names = git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    staged = {
        name: git(root, "show", f":{name}")
        for raw in names.split(b"\0")
        if raw and (name := raw.decode()).endswith(".json")
    }
    if not staged:
        return 0
    try:
        count = validate(root, staged, {s.encode() for s in secret_values()})
    except (
        UnsafeSeal,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        tarfile.TarError,
        zipfile.BadZipFile,
        EOFError,
    ):
        # Exception details can contain credentials or an attacker-controlled path.
        print(
            "Seal audit FAILED. Check hashes, coverage, and credentials locally.", file=sys.stderr
        )
        return 1
    if count:
        print(f"Seal audit passed: {count} evidence files checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

import hashlib
import io
import json
import tarfile

import pytest

from ctb import seal_guard as guard


def signed(body):
    body["digest"] = guard.digest(body)
    return body


@pytest.fixture
def evidence(tmp_path):
    trial = tmp_path / "jobs/r/a/t"
    (trial / "agent").mkdir(parents=True)
    (trial / "agent/transcript.txt").write_text("A harmless tool response.")
    result = trial / "result.json"
    result.write_text(json.dumps({"trial_name": "t"}))
    seal = signed(
        {
            "round": "r",
            "sealed_at": "now",
            "runs": [{"dirty": False}],
            "rows": [
                {
                    "result": "jobs/r/a/t/result.json",
                    "result_sha256": hashlib.sha256(result.read_bytes()).hexdigest(),
                }
            ],
        }
    )
    (tmp_path / "results").mkdir()
    (tmp_path / "results/r.json").write_text(json.dumps(seal))
    files = [
        {
            "path": p.relative_to(tmp_path).as_posix(),
            "bytes": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in sorted(tmp_path.rglob("*"))
        if p.is_file()
    ]
    manifest = signed({"files": files, "seals": [{"round": "r", "digest": seal["digest"]}]})
    return tmp_path, seal, manifest


def stage(seal, manifest):
    return {
        "results/r.json": json.dumps(seal).encode(),
        "results/transcripts-seal.json": json.dumps(manifest).encode(),
    }


def test_normal_commit_does_not_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "scan_file", lambda *a: pytest.fail("Unexpected scan"))
    assert guard.validate(tmp_path, {"package.json": b'{"name":"app"}'}, set()) == 0


def test_complete_seal_passes(evidence):
    root, seal, manifest = evidence
    assert guard.validate(root, stage(seal, manifest), set()) == 3


def test_result_seal_requires_manifest(evidence):
    root, seal, _ = evidence
    with pytest.raises(guard.UnsafeSeal, match="Stage a full"):
        guard.validate(root, {"results/r.json": json.dumps(seal).encode()}, set())


@pytest.mark.parametrize("change", ["hash", "missing", "extra", "symlink"])
def test_changed_or_incomplete_evidence_fails(evidence, change):
    root, seal, manifest = evidence
    transcript = root / "jobs/r/a/t/agent/transcript.txt"
    if change == "hash":
        transcript.write_text("Changed")
    elif change == "missing":
        transcript.unlink()
    elif change == "extra":
        (transcript.parent / "hidden.txt").write_text("Omitted evidence")
    else:
        (transcript.parent / "link").symlink_to(transcript)
    with pytest.raises(guard.UnsafeSeal):
        guard.validate(root, stage(seal, manifest), set())


def test_staged_seal_cannot_differ_from_audited_file(evidence):
    root, seal, manifest = evidence
    seal["sealed_at"] = "later"
    signed(seal)
    with pytest.raises(guard.UnsafeSeal, match="Staged seal differs"):
        guard.validate(root, stage(seal, manifest), set())


def test_bad_manifest_digest_fails(evidence):
    root, seal, manifest = evidence
    manifest["digest"] = "wrong"
    with pytest.raises(guard.UnsafeSeal):
        guard.validate(root, stage(seal, manifest), set())


@pytest.mark.parametrize(
    "payload",
    [
        b"Bearer " + b"A" * 40,
        b"sk-proj-" + b"A" * 40,
        b"-----BEGIN PRIVATE KEY-----",
        b'"access_token": "' + b"A" * 40 + b'"',
        b"eyJ" + b"A" * 20 + b"." + b"B" * 30 + b"." + b"C" * 20,
    ],
)
def test_secret_patterns_fail_without_echoing_values(payload):
    with pytest.raises(guard.UnsafeSeal) as error:
        guard.scan_stream(io.BytesIO(payload), set(), [0])
    assert payload.decode() not in str(error.value)


def test_known_secret_across_chunk_boundary():
    secret = b"unique-test-credential"
    payload = b" " * (1024 * 1024 - 5) + secret
    with pytest.raises(guard.UnsafeSeal, match="Known credential"):
        guard.scan_stream(io.BytesIO(payload), {secret}, [0])


def test_archive_contents_are_scanned(tmp_path):
    path = tmp_path / "evidence.tgz"
    value = b"Bearer " + b"B" * 50
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("nested.txt")
        info.size = len(value)
        archive.addfile(info, io.BytesIO(value))
    with pytest.raises(guard.UnsafeSeal):
        guard.scan_file(path, set(), [0])


def test_corrupt_archive_fails(tmp_path):
    path = tmp_path / "broken.tgz"
    path.write_bytes(b"not an archive")
    with pytest.raises(tarfile.TarError):
        guard.scan_file(path, set(), [0])


def test_size_limit_fails_closed(monkeypatch):
    monkeypatch.setattr(guard, "MAX_EXPANDED_BYTES", 2)
    with pytest.raises(guard.UnsafeSeal, match="limit"):
        guard.scan_stream(io.BytesIO(b"long"), set(), [0])


def test_outside_path_is_refused(tmp_path):
    with pytest.raises(guard.UnsafeSeal):
        guard.safe_path(tmp_path, "../outside")


def test_real_git_hook_uses_index_and_blocks_changed_evidence(evidence, monkeypatch):
    import os
    import shutil
    import subprocess
    from pathlib import Path

    root, seal, manifest = evidence
    source = Path(guard.__file__).resolve().parents[1]
    (root / ".githooks").mkdir()
    shutil.copy2(source / ".githooks/pre-commit", root / ".githooks/pre-commit")
    (root / ".venv").symlink_to(source / ".venv", target_is_directory=True)
    env = dict(os.environ, PYTHONPATH=str(source))
    monkeypatch.setenv("PYTHONPATH", str(source))

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, check=False)

    assert git("init", "-q").returncode == 0
    assert git("config", "core.hooksPath", ".githooks").returncode == 0
    assert git("config", "user.name", "Test").returncode == 0
    assert git("config", "user.email", "test@example.com").returncode == 0
    (root / "code.txt").write_text("normal commit")
    assert git("add", "code.txt").returncode == 0
    assert git("commit", "-qm", "normal code").returncode == 0
    for name, raw in stage(seal, manifest).items():
        (root / name).write_bytes(raw)
    assert git("add", "results").returncode == 0
    transcript = root / "jobs/r/a/t/agent/transcript.txt"
    original = transcript.read_bytes()
    transcript.write_bytes(b"changed after staging")
    blocked = git("commit", "-qm", "invalid seal")
    assert blocked.returncode != 0
    assert b"Seal audit FAILED" in blocked.stderr
    transcript.write_bytes(original)
    assert git("commit", "-qm", "verified seal").returncode == 0


def test_explicit_link_metadata_is_checked_without_following(evidence):
    root, seal, manifest = evidence
    link = root / "jobs/r/a/t/agent/python"
    link.symlink_to("/container-only/python")
    target = b"/container-only/python"
    manifest["files"].append(
        {
            "path": link.relative_to(root).as_posix(),
            "type": "symlink",
            "target": target.decode(),
            "bytes": len(target),
            "sha256": hashlib.sha256(target).hexdigest(),
        }
    )
    signed(manifest)
    assert guard.validate(root, stage(seal, manifest), set()) == 4
    link.unlink()
    link.symlink_to("/different/python")
    with pytest.raises(guard.UnsafeSeal):
        guard.validate(root, stage(seal, manifest), set())

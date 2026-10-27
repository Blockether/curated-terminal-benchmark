"""Reproducible Dirge builds with the pinned streaming parser fix."""

import io
import tarfile

import pytest

from ctb import cli
from ctb.agents.dirge import Dirge


def test_prepare_dirge_rig_uses_the_locked_checksum(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "Cargo.lock").write_text(
        '[[package]]\nname = "rig-core"\nversion = "0.41.0"\nchecksum = "locked-digest"\n'
    )
    monkeypatch.setattr(cli, "ARTIFACTS", tmp_path)
    calls = []

    def download(url, path, sha256):
        calls.append((url, sha256))
        content = b"pinned crate source"
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo("rig-core-0.41.0/src/lib.rs")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        return path

    monkeypatch.setattr(cli, "download", download)
    cli.prepare_dirge_rig(source)
    assert calls == [
        ("https://static.crates.io/crates/rig-core/rig-core-0.41.0.crate", "locked-digest")
    ]
    assert (source / "vendor/rig-core-0.41.0/src/lib.rs").read_text() == "pinned crate source"


def test_prepare_dirge_rig_rejects_another_version(tmp_path):
    (tmp_path / "Cargo.lock").write_text(
        '[[package]]\nname = "rig-core"\nversion = "0.42.0"\nchecksum = "other"\n'
    )
    with pytest.raises(RuntimeError, match="0.41.0"):
        cli.prepare_dirge_rig(tmp_path)


def test_dirge_build_runs_the_native_regression_before_packaging():
    assert "cargo test" in cli.DIRGE_BUILD
    assert "codex_completed_response_keeps_usage" in cli.DIRGE_BUILD
    assert "codex_body_gets_gpt_settings" in cli.DIRGE_BUILD
    assert cli.DIRGE_BUILD.index("cargo test") < cli.DIRGE_BUILD.index("cargo build")
    assert Dirge.ARCHIVE.name != "dirge-x86_64-unknown-linux-musl.tar.gz"


def test_fetch_dirge_prepares_and_patches_before_build(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ARTIFACTS", tmp_path)
    source = tmp_path / "source"
    calls = []
    monkeypatch.setattr(cli, "checkout", lambda *args: source)
    monkeypatch.setattr(cli, "prepare_dirge_rig", lambda path: calls.append(("prepare", path)))
    monkeypatch.setattr(
        cli, "apply_patches", lambda path, *names: calls.append(("patch", path, names))
    )
    monkeypatch.setattr(cli, "sh", lambda command: calls.append(("build", command[-1])))
    monkeypatch.setattr(cli, "pack", lambda *args: calls.append(("pack", args)))
    archive = cli.fetch_dirge()
    assert archive.name == Dirge.ARCHIVE.name
    assert calls[:2] == [
        ("prepare", source),
        (
            "patch",
            source,
            ("dirge-usage.patch", "dirge-rig-stream.patch", "dirge-codex-settings.patch"),
        ),
    ]
    assert calls[2] == ("build", cli.DIRGE_BUILD)
    assert calls[3] == (
        "pack",
        (source / "target-linux/x86_64-unknown-linux-musl/release/dirge", archive, "dirge"),
    )

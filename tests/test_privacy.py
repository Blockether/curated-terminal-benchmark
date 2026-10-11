import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from harbor.cli.job_plugins import attach_job_plugin

from ctb import cli, privacy
from ctb.matrix import AGENTS, ROOT, Trial

# Fake credentials: the tests plant them and check that no trial file keeps them.
TOKEN = "sk-ant-oat01-" + "f" * 40
PROXY_KEY = "fake-proxy-key-0123456789"


def test_harbor_jobs_run_with_telemetry_off(tmp_path):
    env = cli.host_env(Trial(AGENTS["kmet"], "openai", "t"), tmp_path)
    assert env["HARBOR_TELEMETRY"] == "off"


def test_python_with_the_repository_on_its_path_turns_telemetry_off():
    env = {k: v for k, v in os.environ.items() if k != "HARBOR_TELEMETRY"}
    probe = "import os; print(os.environ.get('HARBOR_TELEMETRY'))"
    out = subprocess.run(
        [sys.executable, "-c", probe],
        env=dict(env, PYTHONPATH=str(ROOT)),
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "off"


def test_harbor_runs_get_the_redactor_and_pass_the_check():
    command = cli.harbor_command(Trial(AGENTS["codex"], "openai", "t"), Path("/j"), 0.25)
    assert command[command.index("--plugin") + 1] == privacy.REDACTOR
    assert privacy.check_command(command) is command


@pytest.mark.parametrize("flag", [*sorted(privacy.HUB_FLAGS), "--public=true"])
def test_hub_flags_are_refused(flag):
    with pytest.raises(ValueError, match="Harbor Hub"):
        privacy.check_command(["harbor", "run", "-p", "dataset", flag])


@pytest.mark.parametrize("command", [["harbor", "upload", "jobs/r1"], ["harbor", "publish"]])
def test_only_harbor_run_is_allowed(command):
    with pytest.raises(ValueError, match="harbor run"):
        privacy.check_command(command)


def test_secrets_come_from_sensitive_names_and_credential_files(tmp_path):
    codex = tmp_path / "auth.json"
    codex.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": TOKEN, "account_id": "acct-0123456789"},
                "last_refresh": "2026-10-11T16:13:54.000000Z",
            }
        )
    )
    env = {
        "CTB_PROXY_KEY": PROXY_KEY,
        "CODEX_AUTH_JSON_PATH": str(codex),
        "CLAUDE_FORCE_OAUTH": "1",
        "MAX_TOKENS": "128000000",
        "PATH": "/usr/bin:/bin",
    }
    assert privacy.secret_values(env, files=[]) == {TOKEN, PROXY_KEY}
    assert privacy.secret_values({}, files=[codex]) == {TOKEN}


def test_redaction_keeps_binary_files_and_reports_changes(tmp_path):
    text = tmp_path / "agent" / "log.txt"
    text.parent.mkdir()
    text.write_text(f"token={TOKEN}\n")
    binary = tmp_path / "agent" / "image.png"
    binary.write_bytes(b"\x89PNG\0" + TOKEN.encode())
    assert privacy.redact_tree(tmp_path, {TOKEN}) == [text]
    assert text.read_text() == "token=[REDACTED]\n"
    assert binary.read_bytes() == b"\x89PNG\0" + TOKEN.encode()
    assert privacy.redact_tree(tmp_path, set()) == []


def test_redactor_removes_planted_credentials_from_trajectories(tmp_path, monkeypatch):
    auth = tmp_path / "anthropic-auth.json"
    auth.write_text(json.dumps({"access_token": TOKEN, "expires_at_ms": 1}))
    monkeypatch.setattr(privacy, "CREDENTIAL_FILES", (auth,))
    monkeypatch.setenv("CTB_PROXY_KEY", PROXY_KEY)
    job_dir = tmp_path / "job"
    trajectory = job_dir / "trial__1" / "agent" / "trajectory.json"
    trajectory.parent.mkdir(parents=True)
    step = {"source": "agent", "message": f"key {PROXY_KEY} and token {TOKEN}"}
    trajectory.write_text(json.dumps({"steps": [step]}))
    hooks = []
    job = SimpleNamespace(job_dir=job_dir, on_trial_ended=hooks.append)
    # Harbor's own loader checks the plugin type and starts it.
    plugin = asyncio.run(attach_job_plugin(job, privacy.REDACTOR))
    event = SimpleNamespace(config=SimpleNamespace(trials_dir=job_dir, trial_name="trial__1"))
    asyncio.run(hooks[0](event))
    text = trajectory.read_text()
    assert TOKEN not in text and PROXY_KEY not in text
    assert json.loads(text)["steps"][0]["message"] == "key [REDACTED] and token [REDACTED]"
    (job_dir / "job.log").write_text(f"done {TOKEN}")
    asyncio.run(plugin.on_job_end(None))
    assert (job_dir / "job.log").read_text() == "done [REDACTED]"


def test_round_directories_are_private(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "trials", lambda *_: [])
    monkeypatch.setattr(cli, "record_run", lambda *_: None)
    args = argparse.Namespace(
        round="r", agents="codex", models="openai", tasks="all", parallel=1, timeout_multiplier=1.0
    )
    cli.run(args)
    for path in (tmp_path / "jobs" / "r", tmp_path / "runs" / "r"):
        assert path.stat().st_mode & 0o777 == 0o700

import argparse
import hashlib
import json

import pytest

from ctb import cli, credentials, matrix


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(cli, "ARTIFACTS", tmp_path / "artifacts")
    return tmp_path


def run_args(**extra):
    base = {
        "round": "r",
        "agents": "dirge",
        "models": "anthropic",
        "tasks": "html-js-filter",
        "parallel": 1,
        "timeout_multiplier": 0.25,
    }
    return argparse.Namespace(**{**base, **extra})


def test_host_env_defers_a_claude_trial_with_a_short_token(monkeypatch, tmp_path):
    def short(_min_valid_s):
        raise credentials.ExpiringToken("short")

    monkeypatch.setattr(credentials, "anthropic", short)
    with pytest.raises(credentials.ExpiringToken):
        cli.host_env(matrix.Trial(matrix.AGENTS["dirge"], "anthropic", "t"), tmp_path)


def test_run_retries_deferred_trials_after_a_token_refresh(root, monkeypatch, capsys):
    answers = iter(["defer dirge: short", "done dirge: reward=1.0"])
    refreshes = []
    monkeypatch.setattr(cli, "run_trial", lambda *a: next(answers))
    monkeypatch.setattr(credentials, "refresh_anthropic", refreshes.append)
    cli.run(run_args())
    out = capsys.readouterr().out
    assert "1 trials wait for a Claude token refresh" in out and "done dirge" in out
    assert len(refreshes) == 1
    record = json.loads((root / "jobs/r/ctb-runs.json").read_text())
    assert record[0]["pins"]["dirge"] == cli.DIRGE_COMMIT and record[0]["parallel"] == 1


def write_result(root, job, reward):
    trial_dir = root / "jobs/r" / job / "t__1"
    trial_dir.mkdir(parents=True)
    result = {
        "verifier_result": {"rewards": {"reward": reward}},
        "agent_result": {"n_input_tokens": 10, "n_cache_tokens": 4, "n_output_tokens": 2},
        "agent_info": {"version": "1.0"},
        "task_checksum": "abc",
    }
    (trial_dir / "result.json").write_text(json.dumps(result))


def write_runs(root, dirty):
    (root / "jobs/r").mkdir(parents=True, exist_ok=True)
    (root / "jobs/r/ctb-runs.json").write_text(json.dumps([{"commit": "c", "dirty": dirty}]))


def test_seal_writes_rows_hashes_and_a_digest_once(root):
    write_runs(root, dirty=False)
    write_result(root, "dirge__anthropic__html-js-filter", 1.0)
    cli.seal(run_args())
    sealed = json.loads((root / "results/r.json").read_text())
    row = sealed["rows"][0]
    assert (row["agent"], row["reward"], row["cache"], row["agent_version"]) == (
        "dirge",
        1.0,
        4,
        "1.0",
    )
    result = root / row["result"]
    assert row["result_sha256"] == hashlib.sha256(result.read_bytes()).hexdigest()
    digest = sealed.pop("digest")
    assert digest == hashlib.sha256(json.dumps(sealed, sort_keys=True).encode()).hexdigest()
    with pytest.raises(SystemExit, match="already sealed"):
        cli.seal(run_args())


def test_seal_refuses_missing_trials_and_dirty_runs(root):
    write_runs(root, dirty=False)
    with pytest.raises(SystemExit, match="1 trials have no result"):
        cli.seal(run_args())
    write_runs(root, dirty=True)
    write_result(root, "dirge__anthropic__html-js-filter", 0.0)
    with pytest.raises(SystemExit, match="clean harness commit"):
        cli.seal(run_args())

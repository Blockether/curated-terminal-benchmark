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
        "models": "openai",
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
    agent, family, task = job.split("__")
    trial_dir = root / "jobs/r" / job / "t__1"
    trial_dir.mkdir(parents=True)
    result = {
        "task_name": f"terminal-bench/{task}",
        "trial_name": "t__1",
        "started_at": "2026-10-11T10:00:00Z",
        "finished_at": "2026-10-11T10:05:00Z",
        "config": {"agent": {"name": agent, "model_name": cli.MODELS[family]}},
        "verifier_result": {"rewards": {"reward": reward}},
        "agent_result": {"n_input_tokens": 10, "n_cache_tokens": 4, "n_output_tokens": 2},
        "agent_info": {"name": agent, "version": "1.0"},
        "task_checksum": "abc",
    }
    (trial_dir / "result.json").write_text(json.dumps(result))


def write_runs(root, dirty):
    (root / "jobs/r").mkdir(parents=True, exist_ok=True)
    (root / "jobs/r/ctb-runs.json").write_text(json.dumps([{"commit": "c", "dirty": dirty}]))


def test_seal_writes_rows_hashes_and_a_digest_once(root):
    write_runs(root, dirty=False)
    write_result(root, "dirge__openai__html-js-filter", 1.0)
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
    write_result(root, "dirge__openai__html-js-filter", 0.0)
    with pytest.raises(SystemExit, match="clean harness commit"):
        cli.seal(run_args())


def test_rows_read_the_trials_of_a_round_and_skip_job_results(root):
    write_result(root, "dirge__openai__html-js-filter", 1.0)
    (root / "jobs/r/dirge__openai__html-js-filter/result.json").write_text('{"stats": {}}')
    assert cli.rows(root / "jobs/r") == [
        {
            "agent": "dirge",
            "model": "openai/gpt-6.1-sol",
            "task": "terminal-bench/html-js-filter",
            "reward": 1.0,
            "error": None,
            "input": 10,
            "output": 2,
        }
    ]
    assert cli.rows(root / "jobs/missing") == []


def test_summary_counts_the_level_of_a_dataset_task_name(root, capsys):
    write_result(root, "dirge__openai__html-js-filter", 1.0)
    cli.summary(argparse.Namespace(round="r", json=None))
    columns = capsys.readouterr().out.splitlines()[1].split()
    assert columns[:3] == ["dirge", "openai/gpt-6.1-sol", "1/1"]
    assert sorted(columns[3:5]) == ["0/0", "1/1"]


def test_run_trial_needs_podman(root, monkeypatch):
    monkeypatch.setattr(cli, "podman_ready", lambda env=None: False)
    trial = matrix.Trial(matrix.AGENTS["codex"], "openai", "t")
    with pytest.raises(cli.PodmanDown, match="not reachable"):
        cli.run_trial(trial, root / "jobs", root, 0.25)


def test_run_trial_drops_a_job_when_podman_stops_during_it(root, monkeypatch):
    probes = iter([True, False])
    monkeypatch.setattr(cli, "podman_ready", lambda env=None: next(probes))
    monkeypatch.setattr(cli, "host_env", lambda *a: {})
    monkeypatch.setattr(cli, "harbor_command", lambda *a: ["true"])
    monkeypatch.setattr(cli, "hard_limit", lambda *a: 60)
    trial = matrix.Trial(matrix.AGENTS["codex"], "openai", "t")
    (root / "jobs" / trial.job / "t__1").mkdir(parents=True)
    with pytest.raises(cli.PodmanDown, match="stopped during"):
        cli.run_trial(trial, root / "jobs", root, 0.25)
    assert not (root / "jobs" / trial.job).exists()


def test_run_stops_with_a_message_when_podman_is_down(root, monkeypatch):
    def down(*_):
        raise cli.PodmanDown("Podman is not reachable")

    monkeypatch.setattr(cli, "run_trial", down)
    with pytest.raises(SystemExit, match="not reachable"):
        cli.run(run_args())


def test_hard_limit_adds_every_task_timeout_and_a_margin(tmp_path, monkeypatch):
    task = tmp_path / "t"
    task.mkdir()
    (task / "task.toml").write_text(
        "[agent]\ntimeout_sec = 28800.0\n"
        "[verifier]\ntimeout_sec = 300.0\n"
        "[verifier.environment]\nbuild_timeout_sec = 600.0\n"
        "[environment]\nbuild_timeout_sec = 600.0\n"
    )
    monkeypatch.setattr(cli, "DATASET", tmp_path)
    setup = cli.AGENT_SETUP_TIMEOUT_S * cli.SETUP_MULTIPLIER
    assert cli.hard_limit("t", 0.25) == 7200 + 300 + 600 + 600 + setup + cli.HARD_LIMIT_MARGIN_S


def test_run_trial_stops_a_trial_that_harbor_does_not_stop(root, monkeypatch):
    # The baseline round of 2026-10-11 ran samizdat 50 minutes past its 2 h agent timeout.
    removed = []
    monkeypatch.setattr(cli, "podman_ready", lambda env=None: True)
    monkeypatch.setattr(cli, "host_env", lambda *a: {})
    monkeypatch.setattr(cli, "harbor_command", lambda *a: ["sh", "-c", "sleep 30 & wait"])
    monkeypatch.setattr(cli, "hard_limit", lambda *a: 0.5)
    monkeypatch.setattr(cli, "remove_containers", lambda job, env: removed.append(job.name))
    trial = matrix.Trial(matrix.AGENTS["samizdat"], "openai", "t")
    line = cli.run_trial(trial, root / "jobs", root, 0.25)
    assert line.startswith(f"done {trial.job}: exit=timeout reward=None")
    assert removed == [trial.job]
    error = cli.trial_result(root / "jobs", trial)["exception_info"]
    assert error["exception_type"] == "HarnessTimeoutError"
    found = cli.harbor_trial(root / "jobs", trial)
    assert (found.agent, found.model, found.reward, found.error) == (
        "samizdat",
        cli.MODELS["openai"],
        None,
        "HarnessTimeoutError",
    )


def test_run_trial_keeps_a_result_that_harbor_wrote_while_stopping(root, monkeypatch):
    trial = matrix.Trial(matrix.AGENTS["samizdat"], "openai", "t")
    own = root / "jobs" / trial.job / "t__1"
    write = f"mkdir -p {own} && echo '{{\"verifier_result\": null}}' > {own}/result.json"
    monkeypatch.setattr(cli, "podman_ready", lambda env=None: True)
    monkeypatch.setattr(cli, "host_env", lambda *a: {})
    monkeypatch.setattr(cli, "harbor_command", lambda *a: ["sh", "-c", f"{write}; sleep 30"])
    monkeypatch.setattr(cli, "hard_limit", lambda *a: 1)
    monkeypatch.setattr(cli, "remove_containers", lambda job, env: None)
    cli.run_trial(trial, root / "jobs", root, 0.25)
    assert not (root / "jobs" / trial.job / cli.TIMEOUT_DIR).exists()

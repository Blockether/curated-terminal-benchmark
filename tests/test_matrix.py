import pytest

from ctb import matrix
from ctb.agents.base import split_model


def test_only_supported_model_pairs_become_trials():
    jobs = [
        t.job
        for t in matrix.trials(["claude-code", "codex", "vis-team"], ["anthropic", "openai"], ["x"])
    ]
    assert jobs == [
        "claude-code__anthropic__x",
        "codex__openai__x",
        "vis-team__anthropic__x",
        "vis-team__openai__x",
    ]


def test_kmet_runs_only_on_the_gpt_model():
    assert [t.job for t in matrix.trials(["kmet"], ["anthropic", "openai"], ["x"])] == [
        "kmet__openai__x"
    ]


def test_task_selectors():
    tasks = matrix.load_tasks()
    assert len(matrix.select_tasks("easy", tasks)) == 6
    assert len(matrix.select_tasks("hard", tasks)) == 6
    assert matrix.select_tasks("music-harmony", tasks) == ["music-harmony"]
    with pytest.raises(ValueError, match="Unknown"):
        matrix.select_tasks("nope", tasks)


def test_curated_tasks_exist_and_need_no_gpu():
    import tomllib

    from ctb.cli import DATASET

    if not DATASET.is_dir():
        pytest.skip("dataset not present")
    for task in matrix.load_tasks():
        info = tomllib.loads((DATASET / task["name"] / "task.toml").read_text())
        env = info.get("environment", {})
        assert not env.get("gpus") and (env.get("memory_mb") or 0) <= 8192, task["name"]


def test_only_the_two_benchmark_models_are_accepted():
    assert split_model("openai/gpt-6.1-sol") == ("openai", "gpt-6.1-sol")
    with pytest.raises(ValueError):
        split_model("anthropic/claude-sonnet-4-5")

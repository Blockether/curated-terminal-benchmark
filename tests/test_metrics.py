import hashlib
import json

import pytest

from ctb.metrics import list_price, round_metrics, totals, trajectory_counts, trial_metrics


def write_trial(root, name, *, cost, tools, model="openai/gpt-6.1-sol", reward=1.0):
    trial = root / "jobs" / "r" / name / "t1"
    (trial / "agent").mkdir(parents=True)
    timing = {"started_at": "2026-10-16T10:00:00Z", "finished_at": "2026-10-16T10:02:00Z"}
    result = {
        "config": {"agent": {"model_name": model}},
        "agent_result": {
            "n_input_tokens": 1000,
            "n_cache_tokens": 800,
            "n_output_tokens": 100,
            "cost_usd": cost,
        },
        "started_at": "2026-10-16T09:59:00Z",
        "finished_at": "2026-10-16T10:04:00Z",
        "agent_execution": timing,
        "verifier": {"started_at": "2026-10-16T10:02:00Z", "finished_at": "2026-10-16T10:03:30Z"},
    }
    (trial / "result.json").write_text(json.dumps(result))
    steps = [{"source": "user"}] + [
        {"source": "agent", "llm_call_count": 1, "tool_calls": [{"function_name": t}]}
        for t in tools
    ]
    (trial / "agent" / "trajectory.json").write_text(json.dumps({"steps": steps}))
    path = trial / "result.json"
    return {
        "agent": name.split("__")[0],
        "model": model,
        "task": "terminal-bench/x",
        "agent_version": "1.0.0",
        "reward": reward,
        "error": None,
        "result": str(path.relative_to(root)),
        "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def test_a_trial_without_an_agent_cost_gets_the_api_list_price(tmp_path):
    write_trial(tmp_path, "opencode__openai__x", cost=None, tools=["bash", "read"])
    row = trial_metrics(tmp_path / "jobs/r/opencode__openai__x/t1/result.json")
    assert row["cost_usd"] == round(200 * 2e-06 + 800 * 1e-07 + 100 * 1e-05, 4)
    assert row["cost_source"] == "list price"
    assert row["seconds"] == {
        "environment_setup": None,
        "agent_setup": None,
        "agent_execution": 120.0,
        "verifier": 90.0,
        "total": 300.0,
    }
    assert (row["steps"], row["llm_calls"], row["tool_calls"]) == (2, 2, 2)


def test_subagent_tools_of_each_agent_count_as_subagents(tmp_path):
    write_trial(tmp_path, "claude-code__anthropic__x", cost=1.5, tools=["Task", "Agent", "Bash"])
    row = trial_metrics(tmp_path / "jobs/r/claude-code__anthropic__x/t1/result.json")
    assert row["subagents"] == 2
    assert row["cost_usd"] == 1.5 and row["cost_source"] == "agent"


def test_vis_subagents_are_the_distinct_children_of_council_run(tmp_path):
    a, b = "1" * 8 + "-0000-0000-0000-" + "0" * 12, "2" * 8 + "-0000-0000-0000-" + "0" * 12

    def step(code, records):
        call = {"function_name": "python_execution", "arguments": {"code": code}}
        observation = {"results": [{"content": repr(records)}]}
        return {"source": "agent", "tool_calls": [call], "observation": observation}

    steps = [
        step("rs = await council.run([t1, t2])", [{"session_id": a}, {"session_id": b}]),
        step("await council.wait(session_ids=ids)", [{"session_id": a}]),
        step("await council.resume(ids[0], t)", [{"session_id": a}]),
    ]
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps({"steps": steps}))
    assert trajectory_counts(path)["subagents"] == 2


def test_round_metrics_refuses_a_result_that_changed_after_the_seal(tmp_path):
    row = write_trial(tmp_path, "pi__openai__x", cost=0.5, tools=["bash"])
    sealed = {"round": "r", "digest": "d", "rows": [row]}
    metrics = round_metrics(tmp_path, sealed)
    assert metrics["rows"][0]["agent_version"] == "1.0.0"
    assert metrics["agents"][0]["agent_versions"] == ["1.0.0"]
    assert metrics["agents"][0]["cost_per_pass_usd"] == 0.5
    (tmp_path / row["result"]).write_text("{}")
    with pytest.raises(ValueError, match="changed after the seal"):
        round_metrics(tmp_path, sealed)


def test_totals_leave_the_cost_open_when_a_trial_has_no_price():
    row = {
        "agent": "a",
        "model": "m",
        "reward": 0.0,
        "seconds": {"agent_execution": 60.0, "total": 90.0},
        "cost_usd": None,
        "input": 1,
        "cache": 0,
        "output": 1,
        "steps": 1,
        "tool_calls": 1,
        "subagents": 0,
    }
    (total,) = totals([row, {**row, "reward": 1.0, "cost_usd": 2.0}])
    assert total["passed"] == 1 and total["cost_usd"] is None and total["agent_minutes"] == 2.0


def test_an_unknown_model_has_no_list_price():
    assert list_price("other/model", 10, 0, 10) is None

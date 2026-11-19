import json
import re

from ctb.report import render
from ctb.report.trials import find, load


def trial(root, rnd, task):
    path = root / rnd / f"codex__openai__{task}" / f"{task}__abc"
    (path / "agent").mkdir(parents=True)
    result = {"trial_name": f"{task}__abc", "task_name": task, "agent_info": {"name": "codex"}}
    (path / "result.json").write_text(json.dumps(result))


def test_rounds_of_one_run_share_one_run_entry(tmp_path):
    for rnd in ("gpt-run/r1", "gpt-run/r2", "single"):
        trial(tmp_path, rnd, "html-js-filter")
    trials = [load(t, tmp_path) for t in find(tmp_path)]
    assert sorted((t["round"], t["run"]) for t in trials if t["run"]) == [
        ("gpt-run", 1),
        ("gpt-run", 2),
    ]
    assert len({t["id"] for t in trials}) == 3
    single = next(t for t in trials if t["round"] == "single")
    assert single["run"] is None and single["run_name"] == "single"


def fake_trial(name, run, agent="a", task="t", passed=True, errors=0, started="2026-01-01"):
    return {
        "round": name,
        "run_name": name,
        "run": run,
        "id": f"{name}-{run}-{agent}-{task}",
        "agent": agent,
        "task": task,
        "model": "openai/gpt",
        "passed": passed,
        "reward": 1.0 if passed else 0.0,
        "started_at": started,
        "facts": {"outcome": "passed" if passed else "failed", "run_ms": 1000, "cost": 0.5},
        "summary": {
            "provider_calls": 2,
            "provider_ms": 500,
            "tool_calls": 4,
            "tool_errors": errors,
            "tools": {"bash": {"calls": 4, "errors": errors}},
        },
    }


def test_page_starts_on_the_mean_of_the_newest_run_with_rounds():
    trials = [
        fake_trial("gpt", 1, started="2026-02-01"),
        fake_trial("gpt", 2, started="2026-02-02"),
        fake_trial("smoke", None, started="2026-03-01"),
    ]
    html = render.scoped(trials, lambda chosen, kind, _: f"{kind}:{len(chosen)}", "gpt")
    blocks = re.findall(
        r'data-run-block="([^"]*)" data-view-block="([^"]*)"( hidden)?>([^<]*)', html
    )
    assert blocks == [
        ("all", "", " hidden", "all:3"),
        ("gpt", "mean", "", "mean:2"),
        ("gpt", "r1", " hidden", "round:1"),
        ("gpt", "r2", " hidden", "round:1"),
        ("smoke", "", " hidden", "single:1"),
    ]
    assert render.default_run(trials) == "gpt"


def test_mean_views_average_over_rounds_and_link_each_round():
    trials = [
        fake_trial("gpt", 1, errors=2),
        fake_trial("gpt", 2, passed=False, errors=0),
    ]
    [errors] = render.tool_error_mean_rows(trials)
    cells = re.findall(r"<td[^>]*>([^<]*)</td>", errors)
    # Calls, errors per round, error rate, trials with errors per round.
    assert cells[2:] == ["4.0 ± 0.0", "1.0 ± 1.4", "25%", "0.5 ± 0.7"]
    [pair] = render.pair_rows(trials)
    assert ">1/2<" in pair and 'data-outcome="failed passed"' in pair
    assert re.findall(r'href="trial/([^"]+)\.html"', pair) == ["gpt-1-a-t", "gpt-2-a-t"]


def test_local_head_names_versions_model_and_machine_from_the_run(tmp_path):
    from ctb.public_report import NATIVE, local_head

    root = tmp_path / "gpt" / "r1"
    root.mkdir(parents=True)
    record = {"pins": {"effort": "medium"}, "engine": {"arch": "arm64", "emulated": False}}
    (root / "ctb-runs.json").write_text(json.dumps([record]))
    trials = []
    for agent, version in (("codex", "codex-cli 0.166.0"), ("pi", "1.5.0")):
        trial = fake_trial("gpt", 1, agent=agent)
        trial |= {"version": version, "model": "openai/gpt-6.1-sol"}
        trial["path"] = str(root / f"{agent}__job" / "t__abc")
        trials.append(trial)
    head = local_head(trials)
    assert "2 coding agents on 1 CPU-only Terminal-Bench 4.0 tasks" in head
    assert "GPT-6.1-sol with medium reasoning" in head and NATIVE in head
    assert "(0.166.0)</span>" in head and "Rosetta" not in head


def test_only_runs_with_rounds_are_reported():
    trials = [fake_trial("gpt", 1), fake_trial("gpt", 2), fake_trial("smoke", None)]
    assert [t["round"] for t in render.reported(trials)] == ["gpt", "gpt"]
    assert render.scopes(render.reported(trials))[0][:2] == ("all", "mean")


def test_a_round_still_running_stays_out_of_the_mean():
    trials = [fake_trial("gpt", r, task=task) for r in (1, 2) for task in ("t", "u")]
    trials.append(fake_trial("gpt", 3, task="t"))
    assert render.per_round(trials, lambda t: int(t["passed"])) == [2, 2]
    assert render.rounds_text(trials) == "2 (+1 incomplete)"


def test_agents_with_different_rounds_have_no_incomplete_rounds():
    # Agent a has five rounds and agent b one: round 1 has more trials, but no round is incomplete.
    trials = [fake_trial("gpt", r, task=task) for r in range(1, 6) for task in ("t", "u")]
    trials += [fake_trial("gpt", 1, agent="b", task=task, passed=False) for task in ("t", "u")]
    html = render.scope_readings(trials, "mean")
    assert "incomplete" not in html
    assert "1 to 5 per agent" in html
    # Passes per round: 2 for a and 0 for b, the sum of the agent means.
    assert 'Passed per round</p><p class="reading">2.0<' in html
    # A round of b without a cost stays out of the mean of b: 0.5 per trial, two tasks each.
    trials[-1]["facts"]["cost"] = None
    trials.append(fake_trial("gpt", 2, agent="b", task="t"))
    trials.append(
        {**fake_trial("gpt", 2, agent="b", task="u"), "facts": {"run_ms": 1, "cost": 0.5}}
    )
    html = render.scope_readings(trials, "mean")
    assert 'Cost per round</p><p class="reading">$2.00<' in html
    assert "1 agent round without a value left out" in html


def test_charts_keep_agents_with_values_and_count_the_trials_left_out(monkeypatch):
    def fake(agent, tokens, cost):
        return {"agent": agent, "passed": True, "tokens": tokens, "facts": {"cost": cost}}

    monkeypatch.setattr(render, "usage", lambda t: t["tokens"])
    shown = []
    monkeypatch.setattr(
        render,
        "bar_chart",
        lambda names, values, axis, caption, **_: shown.append((names, values, caption)) or "",
    )
    render.agent_charts(
        [
            fake("a", (2000, 1000, 500), 1.5),
            fake("a", (None, None, None), 0.5),
            fake("b", (4000, 0, 1000), None),
        ]
    )
    by_caption = {
        caption.split(" by agent")[0]: (names, values, caption) for names, values, caption in shown
    }
    names, values, caption = by_caption["Total cost"]
    assert (names, values) == ('"a"', [2.0]) and caption.endswith(
        "1 trials without a recorded value left out"
    )
    names, values, caption = by_caption["Input tokens"]
    assert (names, values) == ('"a", "b"', [2.0, 4.0]) and "1 trials without" in caption
    assert by_caption["Cache hit rate"][1] == [50, 0]

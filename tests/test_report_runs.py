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


def test_each_run_gets_one_block_with_a_tab_for_each_round():
    def fake(name, run):
        return {"round": name, "run_name": name, "run": run}

    shown = []
    original = render.round_view
    render.round_view = lambda trials, prefix="": shown.append((prefix, len(trials))) or ""
    render.agent_view, original_agent = (lambda trials, prefix="": ""), render.agent_view
    try:
        html = render.run_views([fake("a", 1), fake("a", 2), fake("b", None)])
    finally:
        render.round_view, render.agent_view = original, original_agent
    assert re.findall(r'data-run-block="([^"]*)"', html) == ["", "a", "b"]
    assert shown == [("run0-", 2), ("run1-", 1)]

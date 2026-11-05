from ctb.report.comparison import cohorts


def trial(agent, task, passed=True, cost=2, ms=1000, checksum="same", setup="setup"):
    return {
        "agent": agent,
        "task": task,
        "model": "model",
        "task_checksum": checksum,
        "setup": setup,
        "passed": passed,
        "facts": {"cost": cost, "agent_ms": ms},
    }


def test_matched_tasks_do_not_reward_fast_failures():
    data = [
        trial("a", "x"),
        trial("b", "x", cost=4),
        trial("a", "y"),
        trial("b", "y", passed=False, cost=0.01),
        trial("a", "extra"),
    ]
    group = cohorts(data)[0]
    assert group["tasks"] == ["x", "y"]
    assert group["solved"] == ["x"]
    assert group["rows"]["a"]["passed"] == 2
    assert group["rows"]["b"]["passed"] == 1
    assert group["rows"]["b"]["cost"] == 4
    assert group["excluded"] == ["extra"]


def test_missing_measurements_use_the_same_denominator():
    group = cohorts(
        [trial("a", "x", cost=None), trial("b", "x"), trial("a", "y"), trial("b", "y", cost=6)]
    )[0]
    assert group["metric_tasks"]["cost"] == ["y"]
    assert group["rows"]["a"]["cost"] == 2
    assert group["rows"]["b"]["cost"] == 6


def test_duplicate_trials_are_not_selected_by_best_result():
    group = cohorts([trial("a", "x"), trial("a", "x", False), trial("b", "x")])[0]
    assert group["tasks"] == []
    assert group["ambiguous"] == ["x"]
    assert group["rows"]["a"]["cost"] is None


def test_task_content_and_setup_must_match():
    assert cohorts([]) == []
    groups = cohorts([trial("a", "x"), trial("b", "x", setup="other")])
    assert len(groups) == 2
    assert all(not g["tasks"] for g in groups)
    group = cohorts([trial("a", "x"), trial("b", "x", checksum="changed")])[0]
    assert group["tasks"] == []
    assert group["mismatched"] == ["x"]


def test_unknown_provenance_and_extreme_ratios():
    from ctb.report.comparison import setup_id

    assert (
        setup_id(
            {"commit": "c", "pins": {}, "parallel": 1, "timeout_multiplier": 0.25, "dirty": False}
        )
        is None
    )
    data = [trial("a", "x"), trial("b", "x")]
    for item in data:
        item["model"] = None
    assert cohorts(data)[0]["tasks"] == []
    group = cohorts([trial("a", "x", cost=1e-300), trial("b", "x", cost=1e300)])[0]
    assert group["rows"]["b"]["cost_ratio"] is None


def test_pairwise_overlap_without_all_agent_overlap():
    from ctb.report.render import matched_comparisons

    html = matched_comparisons([trial("a", "x"), trial("b", "x"), trial("b", "y"), trial("c", "y")])
    assert "a vs b" in html and "b vs c" in html
    assert "1 shared tasks; 1 jointly solved" in html


def test_selection_requires_every_selected_agent_in_the_same_setup():
    from ctb.report.comparison import selected_cohorts

    data = [trial("a", "x"), trial("b", "x"), trial("c", "x", setup="other")]
    assert selected_cohorts(data, []) == []
    assert selected_cohorts(data, ["a"]) == []
    assert selected_cohorts(data, ["a", "missing"]) == []
    assert selected_cohorts(data, ["a", "c"]) == []
    assert selected_cohorts(data, ["a", "b", "c"]) == []
    group = selected_cohorts(data, ["b", "a", "a"])[0]
    assert group["agents"] == ["a", "b"]
    assert group["tasks"] == ["x"]
    assert group["rows"]["a"]["passed"] == 1
    data.append(trial("d", "x", passed=False))
    group = selected_cohorts(data, ["a", "b", "d"])[0]
    assert group["tasks"] == ["x"] and group["solved"] == []


def test_observed_comparison_keeps_setup_differences_and_failures_visible():
    from ctb.report.comparison import selected_cohorts

    data = [
        trial("a", "x", cost=4),
        trial("b", "x", False, cost=1, setup="other"),
        trial("a", "changed"),
        trial("b", "changed", checksum="different", setup="other"),
    ]
    assert selected_cohorts(data, ["a", "b"]) == []
    group = selected_cohorts(data, ["a", "b"], strict=False, basis="attempted")[0]
    assert group["tasks"] == ["x"] and group["solved"] == []
    assert group["setups"] == ["other", "setup"] and not group["strict"]
    assert group["rows"]["b"]["passed"] == 0
    assert group["rows"]["a"]["cost"] == 4 and group["rows"]["b"]["cost"] == 1
    assert group["mismatched"] == ["changed"]
    assert selected_cohorts(data, ["a", "missing"], strict=False) == []


def test_attempt_metrics_use_complete_paired_tasks_not_zero_imputation():
    from ctb.report.comparison import selected_cohorts

    data = [
        trial("a", "x", cost=None),
        trial("b", "x", False),
        trial("a", "y", cost=6),
        trial("b", "y", False, cost=2),
    ]
    for t in data:
        t["facts"]["run_ms"] = 5000
    group = selected_cohorts(data, ["a", "b"], basis="attempted")[0]
    assert group["metric_tasks"]["cost"] == ["y"]
    assert group["metric_tasks"]["run_ms"] == ["x", "y"]
    assert group["rows"]["a"]["cost"] == 6
    assert group["rows"]["b"]["run_ms"] == 5000
    assert group["rows"]["b"]["passed"] == 0
    solved = selected_cohorts(data, ["a", "b"])[0]
    assert solved["metric_tasks"]["cost"] == []
    assert solved["rows"]["a"]["cost"] is None

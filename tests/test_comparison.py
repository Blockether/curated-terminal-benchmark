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

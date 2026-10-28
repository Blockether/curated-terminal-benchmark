import json

from ctb.report import render
from ctb.report.transcripts import event
from ctb.report.trials import clean_version, summarize


def write_vis_trial(jobs, name="t1", task="terminal-bench/demo", model="zai/glm-5.3-flash"):
    """A Harbor trial of Vis whose trace holds only the run result."""
    trial = jobs / "r1" / f"vis-native__{name}" / f"demo__{name}"
    (trial / "agent").mkdir(parents=True)
    timing = {"started_at": "2026-10-16T10:00:00Z", "finished_at": "2026-10-16T10:02:00Z"}
    result = {
        "trial_name": f"demo__{name}",
        "task_name": task,
        "started_at": "2026-10-16T09:59:00Z",
        "finished_at": "2026-10-16T10:04:00Z",
        "config": {"agent": {"model_name": model}},
        "agent_info": {
            "name": "vis-native",
            "version": "\x1b[4m>>>> banner <<<<\n\x1b[0mvis-agent 0.2.29",
        },
        "agent_result": {},
        "agent_execution": timing,
        "verifier": {"started_at": "2026-10-16T10:02:00Z", "finished_at": "2026-10-16T10:03:00Z"},
        "verifier_result": {"rewards": {"reward": 1.0}},
    }
    (trial / "result.json").write_text(json.dumps(result))
    record = {
        "event": "result",
        "payload": {
            "content": [{"markdown": "Done <b>here</b>."}],
            "status": "success",
            "duration-ms": 90_000,
            "tokens": {"input": 100, "cached": 40, "output": 7},
            "cost": {"total_cost": 0.25},
        },
    }
    (trial / "agent" / "vis-trace.jsonl").write_text(json.dumps(record) + "\n")
    return trial


def test_formats_values_for_a_person():
    assert render.duration(None) is None
    assert render.duration(850) == "850 ms"
    assert render.duration(12_340) == "12.3 s"
    assert render.duration(245_000) == "4 min 05 s"
    assert render.duration(3_720_000) == "1 h 02 min"
    assert render.money(0.01234) == "$0.0123"
    assert render.money(1234.5) == "$1,234.50"
    assert render.percent(1, 200) == "<1%"
    assert render.percent(None, 10) is None
    assert render.percent(3, 0) is None
    assert render.stamp("2026-10-16T10:00:00Z") == "2026-10-16 10:00 UTC"
    assert render.task_name("terminal-bench/demo") == "demo"


def test_clean_version_keeps_the_last_line_without_ansi():
    assert clean_version("\x1b[4m>>>> banner <<<<\n\n\x1b[0mvis-agent 0.2.29") == "vis-agent 0.2.29"
    assert clean_version(None) is None
    assert clean_version("  \n") is None


def test_summarize_totals_only_what_is_recorded():
    events = [
        event(
            "call",
            ms=1000.0,
            cost=0.5,
            cost_source="recorded",
            tokens={"input": 10, "cache": 4, "output": 2},
        ),
        event("call"),
        event("tool", name="bash", ms=200.0, error=True),
        event("tool", name="bash", error=False),
        event("tool", name="read", ms=50.0, error=False),
    ]
    s = summarize(events)
    assert (s["provider_calls"], s["provider_timed"], s["provider_ms"]) == (2, 1, 1000.0)
    assert (s["provider_priced"], s["provider_cost"], s["input"]) == (1, 0.5, 10)
    assert s["tools"]["bash"] == {"calls": 2, "errors": 1, "ms": 200.0, "timed": 1}
    assert (s["tool_calls"], s["tool_errors"], s["tool_ms"]) == (3, 1, 250.0)


def test_summarize_marks_tool_errors_unknown_when_the_harness_does_not_record_them():
    s = summarize([event("tool", name="bash"), event("call")])
    assert s["tool_errors"] is None
    assert summarize([])["tool_errors"] == 0


def test_build_writes_the_index_and_one_page_per_trial(tmp_path):
    jobs, out = tmp_path / "jobs", tmp_path / "report"
    write_vis_trial(jobs, task="terminal-bench/<script>x</script>")
    trials = render.build(jobs, out, workers=1)
    assert len(trials) == 1
    trial = trials[0]
    assert trial["version"] == "vis-agent 0.2.29"
    assert trial["facts"]["cost"] == 0.25
    assert trial["facts"]["cost_source"] == "agent run total"
    index = (out / "index.html").read_text()
    page = (out / "trial" / f"{trial['id']}.html").read_text()
    assert "<script>x</script>" not in index and "<script>x</script>" not in page
    assert "&lt;script&gt;x&lt;/script&gt;" in index
    assert f'href="trial/{trial["id"]}.html"' in index
    assert "Run totals that the agent wrote" in page and "$0.2500" in page
    assert "Done &lt;b&gt;here&lt;/b&gt;." in page
    assert "not recorded" in page
    for name in render.ASSETS:
        assert (out / name).is_file()


def test_build_twice_gives_the_same_pages_and_drops_stale_ones(tmp_path):
    jobs, out = tmp_path / "jobs", tmp_path / "report"
    write_vis_trial(jobs)
    first = render.build(jobs, out, workers=1)[0]
    page = out / "trial" / f"{first['id']}.html"
    before = page.read_text()
    (out / "trial" / "gone.html").write_text("old")
    render.build(jobs, out, workers=1)
    assert page.read_text() == before
    assert not (out / "trial" / "gone.html").exists()


def test_trial_page_opens_error_output_and_links_each_error():
    events = [
        event("user", text="Fix it."),
        event("tool", name="bash", input="ls", output="x" * (render.OPEN_LIMIT + 10), error=True),
        event("tool", name="read", input="a", output="y" * (render.OPEN_LIMIT + 10), error=False),
    ]
    trial = {
        "id": "t",
        "round": "r",
        "agent": "pi",
        "harness": "pi",
        "version": "1",
        "model": "m",
        "task": "terminal-bench/demo",
        "reward": 0.0,
        "passed": False,
        "error": None,
        "error_message": None,
        "started_at": None,
        "path": "p",
        "harbor": {
            "seconds": {"total": None, "agent_execution": None},
            "cost_usd": None,
            "cost_source": None,
        },
        "phases": [],
        "events": events,
        "notes": [],
        "summary": summarize(events),
    }
    trial["facts"] = render.facts(trial)
    page = render.trial_page(trial)
    assert '<a class="mono" href="#e-1">bash</a>' in page
    assert '<details open class="is-error">' in page
    # The two short inputs and the error output open; the long output without an error stays closed.
    assert page.count("<details open") == 3
    assert "<details><summary>Output" in page
    assert "Harbor recorded no phase times" in page


def test_diagrams_hold_mermaid_source_that_pele_draws(tmp_path):
    jobs, out = tmp_path / "jobs", tmp_path / "report"
    write_vis_trial(jobs, name="t1")
    write_vis_trial(jobs, name="t2", task="terminal-bench/other")
    trials = render.build(jobs, out, workers=1)
    index = (out / "index.html").read_text()
    page = (out / "trial" / f"{trials[0]['id']}.html").read_text()
    assert index.count('<pre class="mermaid"') == 2
    assert 'x-axis ["vis-native"]' in index and "bar [100]" in index
    assert "pele.global.js" in index and (out / "pele.global.js").is_file()
    # Phase times are absolute UTC dates, so the chart does not move with the reader's zone.
    assert "dateFormat YYYY-MM-DD HH:mm:ss" in page
    assert "Agent execution :p1, 2026-10-16 10:00:00, 2026-10-16 10:02:00" in page
    assert "Verifier :p2, 2026-10-16 10:02:00, 2026-10-16 10:03:00" in page
    assert render.mermaid_text('a "b": c#d;') == "a  b   c d"

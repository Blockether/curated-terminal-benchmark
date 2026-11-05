import json
import re

from ctb.report import render
from ctb.report.transcripts import event, read_dirge, read_opencode
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
    # Every harness gets the same call facts; run totals fill tokens that no call records.
    calls = page[page.index('id="provider"') : page.index('id="tools"')]
    labels = re.findall(r"<dt>([^<]+)</dt>", calls)
    assert labels == [
        "Calls",
        "Recorded call time",
        "Mean time",
        "Longest call",
        "Failed calls",
        "Cost",
        "Input tokens",
        "Cached tokens",
        "Output tokens",
    ]
    assert "$0.2500" in calls and ">100<" in calls and ">40<" in calls and ">7<" in calls
    assert "107 tokens" in page
    assert "Recording notes" not in page and "Vis writes" not in page
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


def test_tokens_come_from_the_run_totals_or_harbor_when_no_call_records_them(tmp_path):
    # Dirge writes usage only in its final result record, not in each assistant message.
    lines = [
        {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Hi"}]}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 249_392,
            "total_cost_usd": 0.0,
            "usage": {
                "input_tokens": 448_724,
                "cached_input_tokens": 238_080,
                "output_tokens": 7_620,
            },
        },
    ]
    (tmp_path / "dirge.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    events = read_dirge(tmp_path, "openai/gpt-5.5")
    result = events[-1]
    assert result["kind"] == "result" and result["ms"] == 249_392 and result["cost"] is None
    harbor = {"input": 0, "cache": 0, "output": 0}
    trial = {"events": events, "summary": summarize(events), "harbor": harbor}
    assert render.usage(trial) == (448_724, 238_080, 7_620)
    plain = [e for e in events if e["kind"] != "result"]
    trial = {"events": plain, "summary": summarize(plain), "harbor": harbor}
    assert render.usage(trial) == (None, None, None)
    trial["harbor"] = {"input": 30, "cache": 10, "output": 5}
    assert render.usage(trial) == (30, 10, 5)


def test_opencode_2_last_step_takes_its_usage_from_the_session_export(tmp_path):
    # OpenCode 2 ends a run without the step_finish of its last step.
    lines = [
        {"type": "step_start", "timestamp": 1000, "part": {"messageID": "msg_1"}},
        {
            "type": "step_finish",
            "timestamp": 3000,
            "part": {"reason": "tool-calls", "cost": 0, "tokens": {"input": 10, "output": 2}},
        },
        {"type": "step_start", "timestamp": 4000, "part": {"messageID": "msg_2"}},
        {"type": "text", "timestamp": 4500, "part": {"text": "done"}},
    ]
    (tmp_path / "opencode.txt").write_text("\n".join(json.dumps(line) for line in lines))
    used = {"input": 7, "output": 3, "reasoning": 1, "cache": {"read": 90}}
    exported = {
        "messages": [
            {"type": "assistant", "id": "msg_1", "tokens": {"input": 10, "output": 2}},
            {"type": "assistant", "id": "msg_2", "time": {"completed": 5000}, "tokens": used},
        ]
    }
    (tmp_path / "opencode").mkdir()
    (tmp_path / "opencode" / "session.json").write_text(json.dumps(exported))
    calls = [e for e in read_opencode(tmp_path, "openai/gpt-6.1-sol") if e["kind"] == "call"]
    assert [c["tokens"] for c in calls] == [
        {"input": 10, "cache": 0, "output": 2},
        {"input": 97, "cache": 90, "output": 4},
    ]
    assert [c["ms"] for c in calls] == [2000, 1000]
    # Without the export, the last call keeps no usage.
    (tmp_path / "opencode" / "session.json").unlink()
    last = [e for e in read_opencode(tmp_path, "openai/gpt-6.1-sol") if e["kind"] == "call"][-1]
    assert last["tokens"] is None


def test_run_usage_includes_side_calls_missing_from_turn_events():
    calls = [event("call", tokens={"input": 100, "cache": 20, "output": 10})]
    run = event("result", tokens={"input": 140, "cache": 30, "output": 15})
    trial = {
        "events": calls + [run],
        "summary": summarize(calls + [run]),
        "harbor": {"input": 140, "cache": 30, "output": 15},
    }
    assert render.usage(trial) == (140, 30, 15)
    trial["events"] = calls
    assert render.usage(trial) == (140, 30, 15)
    trial["harbor"] = {"input": 0, "cache": 0, "output": 0}
    assert render.usage(trial) == (100, 20, 10)
    trial["events"] = []
    assert render.usage(trial) == (None, None, None)


def test_dirge_calls_without_ids_are_not_deduplicated(tmp_path, monkeypatch):
    records = [{"type": "assistant", "message": {"content": []}} for _ in range(1000)]
    (tmp_path / "dirge.jsonl").write_text("\n".join(json.dumps(r) for r in records))
    assert (
        len([e for e in read_dirge(tmp_path, "openai/gpt-6.1-sol") if e["kind"] == "call"]) == 1000
    )
    # Streaming decoders can reuse storage; object identity is not a message identity.
    from ctb.report import transcripts

    original = transcripts.jsonl
    record = {"type": "assistant", "message": {"content": []}}
    monkeypatch.setattr(transcripts, "jsonl", lambda path: (record for _ in range(1000)))
    assert (
        len([e for e in read_dirge(tmp_path, "openai/gpt-6.1-sol") if e["kind"] == "call"]) == 1000
    )
    monkeypatch.setattr(transcripts, "jsonl", original)
    records = [{"type": "assistant", "message": {"id": "same", "content": []}} for _ in range(3)]
    (tmp_path / "dirge.jsonl").write_text("\n".join(json.dumps(r) for r in records))
    assert len([e for e in read_dirge(tmp_path, "openai/gpt-6.1-sol") if e["kind"] == "call"]) == 1


def test_partial_call_prices_do_not_become_a_run_total():
    events = [event("call", cost=1.0), event("call")]
    trial = {
        "events": events,
        "summary": summarize(events),
        "harbor": {
            "cost_usd": None,
            "cost_source": None,
            "seconds": {"total": 2, "agent_execution": 1},
        },
        "error": None,
        "passed": False,
    }
    assert render.facts(trial)["cost"] is None
    events[1]["cost"] = 2.0
    trial["summary"] = summarize(events)
    assert render.facts(trial)["cost"] == 3.0


def test_opencode_session_totals_include_reasoning_and_incomplete_export(tmp_path):
    from ctb.agents.opencode import session_usage
    from ctb.metrics import list_price

    used = {"input": 100, "output": 10, "reasoning": 30, "cache": {"read": 40}}
    assert session_usage([{"tokens": used}])[:3] == (140, 40, 40)
    export = tmp_path / "opencode" / "session.json"
    export.parent.mkdir()
    export.write_text(
        json.dumps(
            {
                "messages": [
                    {"id": "m1", "type": "assistant", "tokens": used, "time": {"completed": 100}}
                ]
            }
        )
    )
    events = read_opencode(tmp_path, "openai/gpt-6.1-sol")
    trial = {
        "events": events,
        "summary": summarize(events),
        "model": "openai/gpt-6.1-sol",
        "error": None,
        "passed": True,
        "harbor": {
            "seconds": {"total": 1, "agent_execution": 1},
            "cost_usd": 0.01,
            "cost_source": "list price",
            "input": 140,
            "cache": 40,
            "output": 10,
        },
    }
    assert render.usage(trial) == (140, 40, 40)
    assert render.facts(trial)["cost"] == list_price(trial["model"], 140, 40, 40)
    trial["harbor"].update(cost_usd=0.5, cost_source="agent")
    assert render.facts(trial)["cost"] == 0.5
    export.write_text("invalid")
    trial["events"] = read_opencode(tmp_path, trial["model"])
    assert render.usage(trial) == (None, None, None)
    assert render.facts(trial)["cost"] == 0.5
    trial["harbor"]["cost_source"] = "list price"
    assert render.facts(trial)["cost"] is None


def test_opencode_rejects_stale_or_empty_usage_export(tmp_path):
    path = tmp_path / "opencode" / "session.json"
    path.parent.mkdir()
    message = {
        "id": "old",
        "type": "assistant",
        "time": {"completed": 1},
        "tokens": {"input": 100, "output": 20},
    }
    path.write_text(json.dumps({"messages": [message]}))
    stream = tmp_path / "opencode.txt"
    stream.write_text(json.dumps({"type": "step_start", "part": {"messageID": "new"}}))
    assert render.run_result(read_opencode(tmp_path, "m"))["usage_complete"] is False
    stream.write_text("")
    for used in ({}, {"input": True, "output": 2}, {"input": 1, "output": -1}):
        message["tokens"] = used
        path.write_text(json.dumps({"messages": [message]}))
        assert render.run_result(read_opencode(tmp_path, "m"))["usage_complete"] is False

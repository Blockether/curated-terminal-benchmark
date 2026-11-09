import json
import re

from ctb.report import render
from ctb.report.transcripts import event, read_dirge, read_opencode, read_verification, read_wisp
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
        "verification": [],
        "notes": [],
        "summary": summarize(events),
    }
    trial["facts"] = render.facts(trial)
    page = render.trial_page(trial)
    assert '<a class="mono" href="#e-1">bash</a>' in page
    assert "No verifier output was recorded." in page
    # Each row has one label: the tool name, with the kind only as its title.
    assert '<a class="ev-kind mono" href="#e-1" title="Tool">bash</a>' in page
    assert '<a class="ev-kind" href="#e-0" title="Prompt">Prompt</a>' in page
    assert '<details open class="is-error">' in page
    # The two short inputs and the error output open; the long output without an error stays closed.
    assert page.count("<details open") == 3
    assert "<details><summary>Output" in page
    assert "Harbor recorded no phase times" in page


def test_verification_lists_test_runs_verifier_output_and_service_logs(tmp_path):
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    test = {"name": "test_ok", "status": "passed", "duration": 5}
    failed = {"name": "test_order", "status": "failed", "duration": 7, "message": "expected 1"}
    run = {"summary": {"start": 100, "stop": 160}, "tests": [test, failed]}
    (verifier / "ctrf.json").write_text(json.dumps({"results": run}))
    (verifier / "ctrf_2.json").write_text(json.dumps({"results": run}))
    (verifier / "test-stdout.txt").write_text("2 tests ran")
    artifacts = tmp_path / "artifacts"
    (artifacts / "app").mkdir(parents=True)
    (artifacts / "load.log").write_text("p99 3.2 s")
    (artifacts / "app" / "out.txt").write_text("agent file")
    manifest = [
        {
            "service": "loadgen",
            "source": "/var/log/load.log",
            "destination": "artifacts/load.log",
            "type": "file",
            "status": "ok",
        },
        {
            "service": None,
            "source": "/app/out.txt",
            "destination": "artifacts/app/out.txt",
            "type": "file",
            "status": "ok",
        },
    ]
    (artifacts / "manifest.json").write_text(json.dumps(manifest))
    entries = read_verification(tmp_path)
    assert [(e["kind"], e["name"], e["status"]) for e in entries] == [
        ("suite", "Test run", "failed"),
        ("test", "test_ok", "passed"),
        ("test", "test_order", "failed"),
        ("suite", "Repeat run 2", "failed"),
        ("test", "test_order", "failed"),
        ("log", "Verifier output", None),
        ("log", "loadgen: /var/log/load.log", None),
    ]
    assert entries[0]["ms"] == 60 and entries[0]["text"] == "2 tests: 1 passed, 1 failed"
    assert entries[2]["text"] == "expected 1"
    assert entries[-1]["text"] == "p99 3.2 s"
    assert read_verification(tmp_path / "missing") == []


def test_trial_page_puts_verification_in_its_own_tab(tmp_path):
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
        "events": [event("user", text="Fix it.")],
        "notes": [],
        "verification": [
            {
                "kind": "suite",
                "name": "Test run",
                "status": "failed",
                "ms": 60,
                "text": "1 tests: 0 passed, 1 failed",
            },
            {
                "kind": "test",
                "name": "test_<order>",
                "status": "failed",
                "ms": 7,
                "text": "expected <1>",
            },
        ],
    }
    trial["summary"] = summarize(trial["events"])
    trial["facts"] = render.facts(trial)
    page = render.trial_page(trial)
    assert 'role="tablist"' in page
    assert 'id="implementation"' in page and 'id="verification"' in page
    assert "test_&lt;order&gt;" in page and "expected &lt;1&gt;" in page
    assert 'title="Suite">Test run</a>' in page and ">Suite<" not in page
    assert (
        page.index('id="implementation"') < page.index("Fix it.") < page.index('id="verification"')
    )


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
    # OpenCode 2 ends a run without the step_finish of its last step. It writes step_start only
    # when output starts to stream, so the call time comes from the export: created to streamed.
    lines = [
        {"type": "step_start", "timestamp": 2900, "part": {"messageID": "msg_1"}},
        # The tool starts before the stream ends; its overlap is tool time, not model time.
        {
            "type": "tool_use",
            "timestamp": 3000,
            "part": {"tool": "read", "state": {"time": {"start": 2920, "end": 2990}}},
        },
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
            {
                "type": "assistant",
                "id": "msg_1",
                "time": {"created": 500, "streamed": 2950, "completed": 3000},
                "tokens": {"input": 10, "output": 2},
            },
            {
                "type": "assistant",
                "id": "msg_2",
                "time": {"created": 3100, "streamed": 4900, "completed": 5000},
                "tokens": used,
            },
        ]
    }
    (tmp_path / "opencode").mkdir()
    (tmp_path / "opencode" / "session.json").write_text(json.dumps(exported))
    calls = [e for e in read_opencode(tmp_path, "openai/gpt-6.1-sol") if e["kind"] == "call"]
    assert [c["tokens"] for c in calls] == [
        {"input": 10, "cache": 0, "output": 2},
        {"input": 97, "cache": 90, "output": 4},
    ]
    assert [(c["at"], c["ms"], c["ms_source"]) for c in calls] == [
        (500, 2420, "recorded"),
        (3100, 1800, "recorded"),
    ]
    # Without the export, the last call keeps no usage and no call has a time.
    (tmp_path / "opencode" / "session.json").unlink()
    calls = [e for e in read_opencode(tmp_path, "openai/gpt-6.1-sol") if e["kind"] == "call"]
    assert calls[-1]["tokens"] is None
    assert [c["ms"] for c in calls] == [None, None]


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


def test_dirge_immediate_tools_survive_an_interrupted_turn(tmp_path):
    records = [
        {
            "type": "ctb_tool_call",
            "id": "a",
            "name": "bash",
            "input": {"command": "sleep 5"},
            "at_ms": 1000,
        },
        {"type": "ctb_tool_call", "id": "b", "name": "read", "input": {"path": "x"}, "at_ms": 1001},
        {"type": "ctb_tool_result", "id": "b", "output": "file contents", "duration_ms": 0},
    ]
    path = tmp_path / "dirge.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + '\n{"type":')
    events = read_dirge(tmp_path, "openai/gpt-6.1-sol")
    tools = [e for e in events if e["kind"] == "tool"]
    assert len(tools) == 2
    assert tools[0]["name"] == "bash" and tools[0]["at"] == 1000
    assert tools[0]["output"] is None and tools[0]["ms"] is None
    assert tools[1]["output"] == "file contents"
    assert tools[1]["ms"] == 0 and tools[1]["ms_source"] == "recorded"
    assert not [e for e in events if e["kind"] == "call"]


def test_dirge_immediate_tools_do_not_duplicate_turn_blocks(tmp_path):
    records = [
        {
            "type": "ctb_tool_call",
            "id": "a",
            "name": "bash",
            "input": {"command": "echo ok"},
            "at_ms": 1000,
        },
        {"type": "ctb_tool_result", "id": "a", "output": "ok", "duration_ms": 25},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "a", "name": "bash", "input": {"command": "echo ok"}}
                ]
            },
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "content": "ok"}]},
        },
    ]
    path = tmp_path / "dirge.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records))
    for _ in range(2):
        events = read_dirge(tmp_path, "openai/gpt-6.1-sol")
        tools = [e for e in events if e["kind"] == "tool"]
        assert len(tools) == 1
        assert tools[0]["ms"] == 25 and tools[0]["output"] == "ok"
        assert len([e for e in events if e["kind"] == "call"]) == 1
    path.write_text("\n".join(json.dumps(r) for r in records[2:]))
    legacy = [e for e in read_dirge(tmp_path, "openai/gpt-6.1-sol") if e["kind"] == "tool"]
    assert len(legacy) == 1 and legacy[0]["output"] == "ok" and legacy[0]["ms"] is None
    path.write_text("")
    assert read_dirge(tmp_path, "openai/gpt-6.1-sol") == []


def test_dirge_unpaired_result_is_not_a_tool_call(tmp_path):
    records = [
        {"type": "ctb_tool_call", "id": "1", "name": "read", "input": {}},
        {"type": "ctb_tool_result", "id": None, "output": "unpaired", "duration_ms": None},
    ]
    (tmp_path / "dirge.jsonl").write_text("\n".join(json.dumps(r) for r in records))
    events = read_dirge(tmp_path, "openai/gpt-6.1-sol")
    tools = [e for e in events if e["kind"] == "tool"]
    assert len(tools) == 1 and tools[0]["output"] is None and tools[0]["ms"] is None
    assert events[-1]["name"] == "Unpaired tool result"
    assert events[-1]["text"] == "unpaired"


def test_wisp_transcript_pairs_tools_and_takes_times_from_the_log(tmp_path):
    session = tmp_path / "wisp-sessions" / "ctb"
    session.mkdir(parents=True)
    usage = {
        "prompt_tokens": 120,
        "completion_tokens": 9,
        "prompt_tokens_details": {"cached_tokens": 64},
    }
    records = [
        {"type": "system", "content": "harness"},
        {"type": "user", "content": "Fix it."},
        {
            "type": "assistant",
            "output": [
                {"type": "text", "text": "Looking."},
                {"type": "tool_call", "id": "c1", "name": "bash", "arguments": '{"command":"ls"}'},
                {
                    "type": "tool_call",
                    "id": "c2",
                    "name": "python3",
                    "arguments": '{"command":"x"}',
                },
            ],
            "finish_reason": "tool_calls",
            "usage": [usage],
        },
        {"type": "tool", "call_id": "c1", "name": "bash", "content": "app.py", "exit": 0},
        {"type": "tool", "call_id": "c2", "name": "python3", "content": "boom", "exit": 1},
        {"type": "assistant", "output": [{"type": "text", "text": "Done."}], "usage": [usage]},
    ]
    (session / "wisp.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    (session / "wisp.log").write_text(
        "1000 request model=gpt-6.1-sol round=1\n"
        "1250 http_done url=x lines=6 payloads=2 ms=240 status=200\n"
        "1260 tool end name=bash call_id=c1 exit=0 ms=60 bytes=6\n"
        "1300 tool end name=python3 call_id=c2 exit=1 ms=35 bytes=4\n"
        "1400 request model=gpt-6.1-sol round=2\n"
        "1500 http_done url=x lines=6 payloads=2 ms=90 status=200\n"
    )
    events = read_wisp(tmp_path, "openai/gpt-6.1-sol")
    assert [e["kind"] for e in events] == [
        "user",
        "call",
        "assistant",
        "tool",
        "tool",
        "call",
        "assistant",
    ]
    calls = [e for e in events if e["kind"] == "call"]
    assert [(c["at"], c["ms"]) for c in calls] == [(1000, 240), (1400, 90)]
    assert calls[0]["tokens"] == {"input": 120, "cache": 64, "output": 9}
    tools = [e for e in events if e["kind"] == "tool"]
    assert [(t["name"], t["output"], t["error"], t["ms"]) for t in tools] == [
        ("bash", "app.py", False, 60),
        ("python3", "boom", True, 35),
    ]
    assert read_wisp(tmp_path / "missing", "openai/gpt-6.1-sol") == []

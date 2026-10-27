import json

from ctb import edit_stats, edits, edits_grade


def write_task(root, name="rename"):
    task = root / name
    (task / "environment" / "workspace" / "src").mkdir(parents=True)
    (task / "environment" / "workspace" / "src" / "a.py").write_text("def old():\n    pass\n")
    (task / "tests").mkdir()
    checks = {
        "want": {"src/a.py": ["def new"]},
        "wantnot": {"src/a.py": ["def old"]},
        "want_tools": ["patch"],
    }
    (task / "tests" / "checks.json").write_text(json.dumps(checks))
    (task / "instruction.md").write_text(
        "Rename it. Locate the references, read their numbered source and edit them with patch."
    )
    return task


def test_build_writes_a_container_task_with_file_checks_only(tmp_path, monkeypatch):
    source = tmp_path / "source"
    write_task(source)
    write_task(source, "find-usages")
    monkeypatch.setattr(edits, "DATASET", tmp_path / "out")
    monkeypatch.setattr(edits, "TASKS", tmp_path / "tasks.toml")
    assert edits.build(source) == ["rename"]
    task = tmp_path / "out" / "rename"
    assert json.loads((task / "tests" / "checks.json").read_text()) == {
        "want": {"src/a.py": ["def new"]},
        "wantnot": {"src/a.py": ["def old"]},
    }
    assert "patch" not in (task / "instruction.md").read_text()
    assert "COPY workspace/ /app/" in (task / "environment" / "Dockerfile").read_text()
    assert 'name = "vis-edits/rename"' in (task / "task.toml").read_text()
    assert 'name = "rename"' in (tmp_path / "tasks.toml").read_text()


def test_build_keeps_the_commands_that_run_before_the_grade(tmp_path, monkeypatch):
    source = tmp_path / "source"
    task = write_task(source)
    (task / "tests" / "test.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'cd "$VIS_HARBOR_WORKSPACE"\n'
        '"$VIS_HARBOR_PYTHON" check.py > .check-result.txt 2>&1 || true\n'
        'exec "$VIS_HARBOR_PYTHON" -m blockether.vis.evals.grade "$VIS_HARBOR_TESTS/checks.json"\n'
    )
    monkeypatch.setattr(edits, "DATASET", tmp_path / "out")
    monkeypatch.setattr(edits, "TASKS", tmp_path / "tasks.toml")
    edits.build(source)
    script = (tmp_path / "out" / "rename" / "tests" / "test.sh").read_text().splitlines()
    assert script[2:] == [
        "cd /app",
        "python3 check.py > .check-result.txt 2>&1 || true",
        "exec python3 /tests/grade.py /tests/checks.json /app /logs/verifier",
    ]


def test_the_grader_rewards_only_a_full_pass(tmp_path):
    checks = {"want": {"a.py": ["def new"]}, "wantnot": {"a.py": ["def old"]}}
    (tmp_path / "checks.json").write_text(json.dumps(checks))
    work, logs = tmp_path / "work", tmp_path / "logs"
    work.mkdir()
    (work / "a.py").write_text("def new():\n    pass\ndef old():\n    pass\n")
    edits_grade.main([str(tmp_path / "checks.json"), str(work), str(logs)])
    assert (logs / "reward.txt").read_text() == "0"
    (work / "a.py").write_text("def new():\n    pass\n")
    edits_grade.main([str(tmp_path / "checks.json"), str(work), str(logs)])
    assert (logs / "reward.txt").read_text() == "1"


FILES = ["client.py", "service.py"]


def trajectory(*calls):
    steps = [
        {"source": "agent", "tool_calls": [{"function_name": n, "arguments": a}]} for n, a in calls
    ]
    return {"steps": [{"source": "user", "message": "task"}, *steps]}


def test_edit_tools_of_each_agent_are_edits_on_their_files():
    run = trajectory(
        ("Edit", {"file_path": "/app/client.py", "old_string": "a", "new_string": "b"}),
        ("edit", {"path": "service.py", "edits": [{"oldText": "a"}, {"oldText": "b"}]}),
        ("apply_patch", {"patchText": "*** Begin Patch\n*** Update File: client.py\n@@\n-a\n+b\n"}),
        ("bash", {"command": "sed -i 's/a/b/' service.py"}),
    )
    actions = edit_stats.calls(run, FILES)
    assert [(a.kind, a.files, a.units) for a in actions] == [
        ("edit", ("client.py",), 1),
        ("edit", ("service.py",), 2),
        ("edit", ("client.py",), 1),
        ("edit", ("service.py",), 1),
    ]
    profile = edit_stats.edit_profile(actions)
    assert (profile["max_edits_per_file"], profile["re_edits"]) == (2, 2)


def test_codex_exec_scripts_and_vis_blocks_split_into_their_calls():
    script = (
        'text(await tools.exec_command({cmd:"cat client.py"}));'
        'text(await tools.apply_patch("*** Begin Patch\\n*** Update File: client.py\\n"));'
        'text(await tools.exec_command({cmd:"cat client.py"}))'
    )
    code = 'c = ws/"client.py"; s = ws/"service.py"\nr1 = patch(c, [])\nr2 = patch(s, [])\nprint(cat(c))'
    actions = edit_stats.calls(
        trajectory(("exec", {"input": script}), ("python_execution", {"code": code})), FILES
    )
    assert [(a.tool, a.kind, a.files) for a in actions] == [
        ("codex.exec_command", "read", ("client.py",)),
        ("codex.apply_patch", "edit", ("client.py",)),
        ("codex.exec_command", "read", ("client.py",)),
        ("vis.patch", "edit", ("client.py",)),
        ("vis.patch", "edit", ("service.py",)),
        ("vis.cat", "read", ("client.py",)),
    ]
    profile = edit_stats.edit_profile(actions)
    assert (profile["re_edits"], profile["reads_after_edit"]) == (1, 2)

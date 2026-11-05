"""Opt-in checks against the installed Dirge binary and the real provider."""

import json
import os
import subprocess
import tarfile

import pytest
from harbor.models.agent.context import AgentContext

from ctb.agents.dirge import Dirge
from ctb.report.transcripts import read_dirge


@pytest.mark.skipif(os.environ.get("CTB_LIVE_DIRGE") != "1", reason="requires live model access")
def test_dirge_reports_provider_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_MIN_TOKEN_S", "120")
    agent = Dirge(logs_dir=tmp_path / "logs", model_name="openai/gpt-6.1-sol")
    home, logs, installed = (tmp_path / name for name in ("home", "logs", "installed"))
    for directory in (home, logs, installed):
        directory.mkdir(mode=0o700)
    with tarfile.open(agent.ARCHIVE) as archive:
        archive.extractall(installed, filter="data")
    for name, content in agent.secret_files().items():
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o600)
    try:
        result = subprocess.run(
            [
                "podman",
                "run",
                "--rm",
                "--platform",
                "linux/amd64",
                "-v",
                f"{home}:/tmp/ctb-home",
                "-v",
                f"{logs}:/logs/agent",
                "-v",
                f"{installed}:/installed-agent:ro",
                "-e",
                "HOME=/tmp/ctb-home",
                "-e",
                "RUST_LOG=rig::providers::openai::responses_api::streaming=debug,rig_core::providers::openai::responses_api::streaming=debug",
                "-e",
                "DIRGE_LOG=/logs/agent/dirge-debug.log",
                "docker.io/library/python:3.12-slim-bookworm",
                "sh",
                "-c",
                agent.command("Use the bash tool to run printf CTB_TOOL_TRACE_OK. Then reply OK."),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        events = [json.loads(line) for line in (logs / "dirge.jsonl").read_text().splitlines()]
        final = next(event for event in reversed(events) if event.get("type") == "result")
        assert not final["is_error"], final
        context = AgentContext()
        agent.populate_context_post_run(context)
        assert context.n_input_tokens > 0
        assert context.n_output_tokens > 0
        tools = [event for event in read_dirge(logs, agent.model_name) if event["kind"] == "tool"]
        assert tools
        assert any("CTB_TOOL_TRACE_OK" in (tool["output"] or "") for tool in tools)
        assert all(tool["name"] and tool["input"] and tool["at"] for tool in tools)
        assert all(tool["ms"] is not None and tool["ms_source"] == "recorded" for tool in tools)
    finally:
        for path in home.rglob("*"):
            if path.is_file():
                path.unlink()

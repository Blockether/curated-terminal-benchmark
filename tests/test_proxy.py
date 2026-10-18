"""Provider stream boundaries for the benchmark proxy."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from ctb import proxy


@pytest.mark.parametrize("terminal", ["response.completed", "response.incomplete"])
def test_codex_returns_when_the_response_completes(monkeypatch, terminal):
    def events():
        yield b'data: {"type":"response.output_item.done","item":{"type":"message","content":[{"text":"OK"}]}}\n'
        yield (
            "data: "
            + json.dumps(
                {
                    "type": terminal,
                    "response": {
                        "model": "gpt-6.1-sol",
                        "usage": {"input_tokens": 18, "output_tokens": 5},
                    },
                }
            )
            + "\n"
        ).encode()
        raise AssertionError("The completed response must not wait for the connection to close")

    monkeypatch.setattr(
        proxy.credentials, "codex_vis", lambda _: SimpleNamespace(token="test", account_id="test")
    )
    monkeypatch.setattr(proxy, "_open", lambda *args: nullcontext(events()))
    reply, usage = proxy.call_codex({"model": "gpt-6.1-sol", "messages": []})
    assert reply["message"]["content"] == "OK"
    assert usage["prompt_tokens"] == 18
    assert usage["completion_tokens"] == 5
    assert reply["finish_reason"] == ("length" if terminal == "response.incomplete" else "stop")


@pytest.mark.parametrize("terminal", ["response.failed", "error"])
def test_codex_stops_and_reports_terminal_errors(monkeypatch, terminal):
    def events():
        yield (
            "data: " + json.dumps({"type": terminal, "error": {"message": "Failed"}}) + "\n"
        ).encode()
        raise AssertionError("A terminal error must not wait for the connection to close")

    monkeypatch.setattr(
        proxy.credentials, "codex_vis", lambda _: SimpleNamespace(token="test", account_id="test")
    )
    monkeypatch.setattr(proxy, "_open", lambda *args: nullcontext(events()))
    with pytest.raises(proxy.UpstreamError, match="Failed"):
        proxy.call_codex({"model": "gpt-6.1-sol", "messages": []})

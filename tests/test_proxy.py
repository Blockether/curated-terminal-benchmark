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


def test_codex_reports_a_stream_that_ends_without_a_response(monkeypatch):
    def events():
        yield b'data: {"type":"response.output_item.done","item":{"type":"message","content":[{"text":"OK"}]}}\n'

    monkeypatch.setattr(
        proxy.credentials, "codex_vis", lambda _: SimpleNamespace(token="test", account_id="test")
    )
    monkeypatch.setattr(proxy, "_open", lambda *args: nullcontext(events()))
    with pytest.raises(proxy.UpstreamError, match="without a response") as error:
        proxy.call_codex({"model": "gpt-6.1-sol", "messages": []})
    assert error.value.status == 502


def test_a_streaming_client_gets_text_tool_calls_and_usage(monkeypatch, tmp_path):
    """Wisp always streams; the OpenAI SDK must read the proxy answer as a stream."""
    from openai import OpenAI

    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "bash", "arguments": '{"c":1}'},
    }
    usage = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
    usage["prompt_tokens_details"] = {"cached_tokens": 4}
    reply = {"message": {"role": "assistant", "content": "Hi", "tool_calls": [call]}}
    reply |= {"finish_reason": "tool_calls", "model": "gpt-6.1-sol"}
    monkeypatch.setattr(proxy, "call_codex", lambda req: (dict(reply), usage))
    server = proxy.Proxy(tmp_path / "proxy.jsonl").start()
    try:
        client = OpenAI(api_key=server.key, base_url=f"http://127.0.0.1:{server.port}/v1")
        chunks = list(
            client.chat.completions.create(
                model="gpt-6.1-sol",
                messages=[{"role": "user", "content": "x"}],
                stream=True,
                stream_options={"include_usage": True},
            )
        )
    finally:
        server.stop()
    deltas = [c.choices[0] for c in chunks if c.choices]
    assert "".join(d.delta.content or "" for d in deltas) == "Hi"
    calls = [t for d in deltas for t in d.delta.tool_calls or []]
    assert [(t.index, t.id, t.function.name, t.function.arguments) for t in calls] == [
        (0, "call_1", "bash", '{"c":1}')
    ]
    assert deltas[-1].finish_reason == "tool_calls"
    assert chunks[-1].usage.prompt_tokens_details.cached_tokens == 4

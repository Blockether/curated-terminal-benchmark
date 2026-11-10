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


def test_codex_body_keeps_tool_result_images():
    """Wisp sends a load_image result as a user message with image_url parts."""
    image = "data:image/png;base64,iVBORw0KGgo="
    call = {"id": "c1", "type": "function", "function": {"name": "load_image", "arguments": "{}"}}
    messages = [
        {"role": "user", "content": "Read the score."},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "content": "Loaded image: page0.png"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Attached image(s) from tool result:"},
                {"type": "image_url", "image_url": {"url": image}},
            ],
        },
    ]
    items = proxy.codex_body({"model": "gpt-6.1-sol", "messages": messages})["input"]
    assert items[0]["content"] == [{"type": "input_text", "text": "Read the score."}]
    assert items[-1]["content"] == [
        {"type": "input_text", "text": "Attached image(s) from tool result:"},
        {"type": "input_image", "image_url": image},
    ]


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


def test_a_responses_client_gets_the_upstream_stream_unchanged(monkeypatch, tmp_path):
    """Wisp speaks Responses: the proxy adds the login and settings, not a conversion."""
    from openai import OpenAI

    done = {
        "type": "response.completed",
        "sequence_number": 2,
        "response": {
            "id": "resp_1",
            "object": "response",
            "model": "gpt-6.1-sol",
            "status": "completed",
            "output": [],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 3,
                "total_tokens": 13,
                "input_tokens_details": {"cached_tokens": 4},
            },
        },
    }
    delta = {"type": "response.output_text.delta", "sequence_number": 1, "delta": "Hi"}
    delta |= {"item_id": "m1", "output_index": 0, "content_index": 0}
    sent = {}

    def upstream(url, body, headers):
        sent.update(url=url, body=body, headers=headers)
        lines = [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in (delta, done)]
        return nullcontext(iter(line.encode() for text in lines for line in text.splitlines(True)))

    monkeypatch.setattr(
        proxy.credentials, "codex_vis", lambda _: SimpleNamespace(token="tok", account_id="acc")
    )
    monkeypatch.setattr(proxy, "_open", upstream)
    server = proxy.Proxy(tmp_path / "proxy.jsonl").start()
    try:
        client = OpenAI(api_key=server.key, base_url=f"http://127.0.0.1:{server.port}/v1")
        events = list(
            client.responses.create(
                model="gpt-6.1-sol",
                input=[{"type": "message", "role": "user", "content": "x"}],
                stream=True,
                store=False,
                prompt_cache_key="s1",
                reasoning={"effort": "medium", "summary": "auto"},
            )
        )
    finally:
        server.stop()
    assert [e.type for e in events] == ["response.output_text.delta", "response.completed"]
    assert events[-1].response.usage.input_tokens_details.cached_tokens == 4
    assert sent["url"] == proxy.CODEX_URL
    assert sent["body"]["text"] == {"verbosity": proxy.VERBOSITY}
    assert sent["body"]["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert sent["body"]["input"] == [{"type": "message", "role": "user", "content": "x"}]
    assert sent["headers"]["Authorization"] == "Bearer tok"
    assert sent["headers"]["session_id"] == "s1"
    row = json.loads((tmp_path / "proxy.jsonl").read_text())
    assert (row["status"], row["usage"]["prompt_tokens"], row["error"]) == (200, 10, None)
    assert row["usage"]["prompt_tokens_details"]["cached_tokens"] == 4

"""OpenAI Chat Completions proxy for agents that only speak that wire.

samizdat reaches models only through an OpenAI-compatible endpoint. This proxy
runs on the host. It sends `gpt-*` models to the ChatGPT Codex Responses API,
with the subscription access token that Vis keeps. The container gets only a
random proxy key.
"""

import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ctb import credentials
from ctb.agents.base import REASONING_SUMMARY, VERBOSITY

CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
MODELS = ("gpt-6.1-sol",)
TOKEN_MIN_S = 120
TIMEOUT_S = 900


class UpstreamError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"upstream HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def _text(content) -> str:
    """Flatten OpenAI message content (a string or a list of parts) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content if isinstance(p, dict))


def _tool_specs(tools) -> list[dict]:
    return [t["function"] for t in tools or [] if t.get("type") == "function"]


def _forced_tool(tool_choice) -> str | None:
    if isinstance(tool_choice, dict):
        return (tool_choice.get("function") or {}).get("name")
    return None


# --- Codex Responses ---------------------------------------------------------


def codex_body(req: dict) -> dict:
    instructions, items = [], []
    for m in req.get("messages") or []:
        role, text = m.get("role"), _text(m.get("content"))
        if role in ("system", "developer"):
            instructions.append(text)
        elif role == "user":
            items.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                }
            )
        elif role == "assistant":
            if text:
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                )
            for call in m.get("tool_calls") or []:
                fn = call.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": fn["name"],
                        "arguments": fn.get("arguments") or "{}",
                    }
                )
        elif role == "tool":
            items.append(
                {"type": "function_call_output", "call_id": m.get("tool_call_id"), "output": text}
            )
    instructions_text = "\n\n".join(i for i in instructions if i) or "You are a coding agent."
    # One key per conversation (the system prompt and the first user turn), as Codex sends
    # its session id: without it the Codex backend does not reuse the cached prefix.
    first = next((json.dumps(i, sort_keys=True) for i in items if i.get("role") == "user"), "")
    cache_key = hashlib.sha256((instructions_text + "\0" + first).encode()).hexdigest()[:32]
    body = {
        "model": req["model"],
        "instructions": instructions_text,
        "input": items,
        "store": False,
        "stream": True,
        "parallel_tool_calls": False,
        "prompt_cache_key": cache_key,
        # The GPT settings of every agent; without them the backend uses `medium` verbosity.
        "text": {"verbosity": VERBOSITY},
    }
    effort = req.get("reasoning_effort")
    if effort:
        body["reasoning"] = {"effort": effort, "summary": REASONING_SUMMARY}
    tools = _tool_specs(req.get("tools"))
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("parameters") or {"type": "object", "properties": {}},
            }
            for t in tools
        ]
        forced = _forced_tool(req.get("tool_choice"))
        body["tool_choice"] = {"type": "function", "name": forced} if forced else "auto"
    return body


def codex_reply(events: list[dict]) -> tuple[dict, dict]:
    text, calls, usage, model, status = [], [], {}, None, None
    for ev in events:
        kind = ev.get("type")
        if kind == "response.output_item.done":
            item = ev.get("item") or {}
            if item.get("type") == "message":
                text.extend(c.get("text", "") for c in item.get("content") or [] if "text" in c)
            elif item.get("type") == "function_call":
                calls.append(
                    {
                        "id": item.get("call_id"),
                        "type": "function",
                        "function": {"name": item["name"], "arguments": item.get("arguments")},
                    }
                )
        elif kind in ("response.completed", "response.incomplete"):
            resp = ev.get("response") or {}
            usage, model, status = resp.get("usage") or {}, resp.get("model"), kind
        elif kind in ("response.failed", "error"):
            raise UpstreamError(502, json.dumps(ev)[:2000])
    if status is None:
        # A stream that closes before its terminal event is a failed call, not an empty answer.
        raise UpstreamError(502, f"stream ended after {len(events)} events without a response")
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
    message = {"role": "assistant", "content": "".join(text)}
    if calls:
        message["tool_calls"] = calls
    finish = "tool_calls" if calls else ("length" if status == "response.incomplete" else "stop")
    return {"message": message, "finish_reason": finish, "model": model}, {
        "prompt_tokens": usage.get("input_tokens") or 0,
        "completion_tokens": usage.get("output_tokens") or 0,
        "total_tokens": usage.get("total_tokens") or 0,
        "prompt_tokens_details": {"cached_tokens": cached},
    }


def call_codex(req: dict) -> tuple[dict, dict]:
    access = credentials.codex_vis(TOKEN_MIN_S)
    headers = {
        "Authorization": f"Bearer {access.token}",
        "chatgpt-account-id": access.account_id or "",
        "accept": "text/event-stream",
    }
    events = []
    with _open(CODEX_URL, codex_body(req), headers) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
                event = json.loads(line[5:])
                events.append(event)
                if event.get("type") in (
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                    "error",
                ):
                    break
    return codex_reply(events)


# --- HTTP --------------------------------------------------------------------


def _open(url: str, body: dict, headers: dict):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", **headers},
        method="POST",
    )
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT_S)
    except urllib.error.HTTPError as e:
        raise UpstreamError(e.code, e.read().decode("utf-8", "replace")) from e


def complete(req: dict) -> dict:
    """One OpenAI chat completion, answered by the upstream of its model."""
    if req.get("stream"):
        raise UpstreamError(400, "stream is not supported; remove :stream from the features")
    model = str(req.get("model") or "")
    choice, usage = call_codex(req)
    return {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": choice.pop("model") or model,
        "choices": [{"index": 0, **choice}],
        "usage": usage,
    }


class Proxy:
    """A threaded proxy server; `url` is for the host, `key` for the agent."""

    def __init__(self, log_path: Path, host: str = "127.0.0.1", port: int = 0):
        self.key = secrets.token_urlsafe(24)
        self.log_path = log_path
        self._lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _send(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self) -> bool:
                if self.headers.get("Authorization") == f"Bearer {proxy.key}":
                    return True
                self._send(401, {"error": {"message": "bad proxy key"}})
                return False

            def do_GET(self):
                if not self._authorized():
                    return
                if self.path.rstrip("/").endswith("/models"):
                    data = [{"id": m, "object": "model", "owned_by": "ctb"} for m in MODELS]
                    self._send(200, {"object": "list", "data": data})
                else:
                    self._send(404, {"error": {"message": "not found"}})

            def do_POST(self):
                if not self._authorized():
                    return
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    self._send(404, {"error": {"message": "not found"}})
                    return
                started = time.monotonic()
                req = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
                try:
                    out = complete(req)
                    status, usage, error = 200, out["usage"], None
                except UpstreamError as e:
                    out = {"error": {"message": str(e), "type": "upstream_error"}}
                    status, usage, error = e.status, {}, str(e)[:300]
                except Exception as e:  # noqa: BLE001 - report every failure to the agent
                    out = {"error": {"message": repr(e), "type": "proxy_error"}}
                    status, usage, error = 502, {}, repr(e)[:300]
                proxy.record(req.get("model"), status, usage, time.monotonic() - started, error)
                self._send(status, out)

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]

    def record(self, model, status, usage, seconds, error) -> None:
        row = {
            "ts": time.time(),
            "model": model,
            "status": status,
            "seconds": round(seconds, 2),
            "usage": usage,
            "error": error,
        }
        with self._lock, self.log_path.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def start(self) -> "Proxy":
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

"""Read each harness's own records into one transcript of events.

A trial is a dict with the Harbor facts of one run and its `events`. An event is a dict:

- `kind`: user, assistant, reasoning, call (one provider call), tool or command.
- `at`: epoch milliseconds, or None when the harness writes no times.
- `ms`: duration in milliseconds, or None when it is not recorded.
- `ms_source`: "recorded" when the harness wrote the duration, "derived" when it comes
  from the times of nearby records, or None.
- `name`, `input`, `output`, `text`: what the event shows.
- `error`: True or False for a tool, command or call; None when the harness has no flag.
- `tokens`: {"input", "cache", "output"} of a provider call; input includes cache reads.
- `cost`, `cost_source`: USD of a provider call, "recorded" or "list price".

Nothing is invented: a value that a harness does not write stays None. The report shows
the same fields for every harness and marks a missing value as not recorded.
"""

import gzip
import io
import json
import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import IO

import edn_format
import zstandard

from ctb.metrics import list_price
from ctb.paths import ARTIFACTS

#: The longest text of one event that the report keeps.
MAX_TEXT = 20_000
DATASETS = ARTIFACTS / "datasets" / "terminal-bench"
WALL_TIME = re.compile(r"Wall time ([0-9.]+) seconds")
FRACTION = re.compile(r"(\.\d{6})\d+")


def epoch_ms(stamp: str | None) -> float | None:
    """Epoch milliseconds of an ISO 8601 time, also with nanoseconds and a Z suffix."""
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(FRACTION.sub(r"\1", stamp)).timestamp() * 1000
    except ValueError:
        return None


def clip(value: object) -> str | None:
    """`value` as text, cut to MAX_TEXT characters with a note of the rest."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
    if len(text) <= MAX_TEXT:
        return text
    return f"{text[:MAX_TEXT]}\n\n[{len(text) - MAX_TEXT} more characters not shown]"


def event(kind: str, **fields: object) -> dict:
    """One event with every field present."""
    base = {
        "kind": kind,
        "at": None,
        "ms": None,
        "ms_source": None,
        "name": None,
        "input": None,
        "output": None,
        "text": None,
        "error": None,
        "tokens": None,
        "cost": None,
        "cost_source": None,
        # Commands that this tool call ran, for example the shell commands of a Codex exec script.
        "commands": [],
    }
    base.update(fields)
    for key in ("input", "output", "text"):
        base[key] = clip(base[key])
    return base


def call(
    model: str,
    at: float | None,
    ms: float | None,
    ms_source: str | None,
    tokens: dict | None,
    cost: float | None = None,
) -> dict:
    """A provider call. A missing or zero cost falls back to the list price of its tokens."""
    source = "recorded" if cost else None
    if not cost and tokens:
        cost = list_price(model, tokens["input"], tokens["cache"], tokens["output"])
        source = "list price" if cost is not None else None
    return event(
        "call",
        at=at,
        ms=ms,
        ms_source=ms_source if ms is not None else None,
        tokens=tokens,
        cost=cost or None,
        cost_source=source,
    )


def tokens(input_: object, cache: object, output: object) -> dict:
    """Token counts of one call; input includes the cache reads."""
    return {"input": int(input_ or 0), "cache": int(cache or 0), "output": int(output or 0)}


def open_text(path: Path) -> IO[str]:
    """A text handle of a plain, .gz or .zst file."""
    if path.suffix == ".zst":
        raw = zstandard.ZstdDecompressor(max_window_size=2**31).stream_reader(
            path.open("rb"), read_across_frames=True, closefd=True
        )
        return io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open(encoding="utf-8", errors="replace")


def jsonl(path: Path, skip: tuple[str, ...] = ()) -> Iterator[dict]:
    """The JSON objects of a JSON Lines file, also .gz or .zst, up to a cut-off tail.

    A line that starts with one of the `skip` prefixes is not parsed.
    """
    with open_text(path) as handle:
        try:
            for line in handle:
                if line.strip() and not line.startswith(skip):
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        yield record
        except (EOFError, zstandard.ZstdError):
            # A trace copied while the agent still wrote it ends inside a frame.
            return


def text_of(content: object) -> str | None:
    """The text parts of a message content: a string or a list of typed parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text") for p in content if isinstance(p, dict) and p.get("text")]
        return "\n\n".join(parts) or None
    return None


def pair(pending: dict, key: object, **fields: object) -> None:
    """Complete the tool event that waits for result `key`."""
    target = pending.pop(key, None)
    if target is not None:
        for name, value in fields.items():
            target[name] = clip(value) if name in ("input", "output", "text") else value


# Harness readers. Each returns the events.


def read_pi(agent: Path, model: str) -> list[dict]:
    events: list[dict] = []
    pending: dict = {}
    for path in sorted((agent / "pi").rglob("*.jsonl")):
        for record in jsonl(path):
            if record.get("type") != "message":
                continue
            message = record.get("message") or {}
            role, at = message.get("role"), message.get("timestamp")
            if role == "user":
                events.append(event("user", at=at, text=text_of(message.get("content"))))
            elif role == "assistant":
                usage = message.get("usage") or {}
                events.append(
                    call(
                        message.get("model") or model,
                        at,
                        message.get("durationMs"),
                        "recorded",
                        tokens(
                            (usage.get("input") or 0)
                            + (usage.get("cacheRead") or 0)
                            + (usage.get("cacheWrite") or 0),
                            usage.get("cacheRead"),
                            usage.get("output"),
                        ),
                        (usage.get("cost") or {}).get("total"),
                    )
                )
                events[-1]["error"] = message.get("stopReason") == "error"
                for part in message.get("content") or []:
                    kind = part.get("type")
                    if kind == "text" and part.get("text"):
                        events.append(event("assistant", at=at, text=part["text"]))
                    elif kind == "thinking" and part.get("thinking"):
                        events.append(event("reasoning", at=at, text=part["thinking"]))
                    elif kind == "toolCall":
                        tool = event(
                            "tool", at=at, name=part.get("name"), input=part.get("arguments")
                        )
                        pending[part.get("id")] = tool
                        events.append(tool)
            elif role == "toolResult":
                pair(
                    pending,
                    message.get("toolCallId"),
                    output=text_of(message.get("content")),
                    error=bool(message.get("isError")),
                    ms=message.get("durationMs"),
                    ms_source="recorded" if message.get("durationMs") is not None else None,
                )
    return events


def opencode_messages(agent: Path) -> dict[str, dict]:
    """The assistant messages of OpenCode's session export, by message id."""
    try:
        exported = json.loads((agent / "opencode" / "session.json").read_text())
    except (OSError, ValueError):
        return {}
    messages = exported.get("messages") if isinstance(exported, dict) else None
    return {
        m["id"]: m
        for m in messages or []
        if isinstance(m, dict) and m.get("type") == "assistant" and isinstance(m.get("id"), str)
    }


def opencode_step(
    model: str, at: float | None, message: dict | None, tools: list, used: dict, cost
) -> dict:
    """A finished OpenCode step as a call, without the time of its tools.

    OpenCode writes step_start when the output starts to stream, so the stream events cannot time
    the call. The session export records when the request started and when its stream ended.
    Tools can run before the stream ends; that overlap is tool time, not model time.
    """
    times = (message or {}).get("time") or {}
    created, streamed = times.get("created"), times.get("streamed")
    timed = all(type(t) in (int, float) for t in (created, streamed)) and streamed >= created
    busy, reach = 0.0, created
    for start, end in sorted(tools) if timed else []:
        start, end = max(start, reach), min(end, streamed)
        if end > start:
            busy, reach = busy + end - start, end
    cache = (used.get("cache") or {}).get("read") or 0
    return call(
        model,
        created if timed else at,
        streamed - created - busy if timed else None,
        "recorded",
        tokens(
            (used.get("input") or 0) + cache,
            cache,
            (used.get("output") or 0) + (used.get("reasoning") or 0),
        ),
        cost,
    )


def read_opencode(agent: Path, model: str) -> list[dict]:
    events: list[dict] = []
    step: dict | None = None
    step_message: str | None = None
    step_tools: list[tuple[float, float]] = []
    observed_messages: set[str | None] = set()
    messages = opencode_messages(agent)
    # OpenCode 2 omits the step_finish of the last step; its session export still has the usage.
    unfinished: list[tuple[dict, str | None, list]] = []
    path = agent / "opencode.txt"
    for record in jsonl(path) if path.is_file() else []:
        kind, part, at = record.get("type"), record.get("part") or {}, record.get("timestamp")
        if kind == "step_start":
            if step is not None:
                unfinished.append((step, step_message, step_tools))
            step, step_tools = call(model, at, None, None, None), []
            step_message = part.get("messageID")
            observed_messages.add(step_message)
            events.append(step)
        elif kind == "step_finish" and step is not None:
            step.update(
                opencode_step(
                    model,
                    step["at"],
                    messages.get(step_message or ""),
                    step_tools,
                    part.get("tokens") or {},
                    part.get("cost"),
                )
            )
            step = None
        elif kind in ("text", "reasoning") and part.get("text"):
            label = "assistant" if kind == "text" else "reasoning"
            events.append(event(label, at=at, text=part["text"]))
        elif kind == "tool_use":
            state = part.get("state") or {}
            time = state.get("time") or {}
            ms = time["end"] - time["start"] if time.get("end") and time.get("start") else None
            if ms is not None:
                step_tools.append((time["start"], time["end"]))
            failed = state.get("status") == "error"
            events.append(
                event(
                    "tool",
                    at=time.get("start") or at,
                    name=part.get("tool"),
                    input=state.get("input"),
                    output=state.get("error") if failed else state.get("output"),
                    error=failed,
                    ms=ms,
                    ms_source="recorded" if ms is not None else None,
                )
            )
    if step is not None:
        unfinished.append((step, step_message, step_tools))
    for open_step, message_id, tools_run in unfinished:
        if message := messages.get(message_id or ""):
            used = message.get("tokens") or {}
            open_step.update(
                opencode_step(model, open_step["at"], message, tools_run, used, message.get("cost"))
            )

    # The export includes the final call that can be absent from the stream.
    def recorded_usage(message):
        used = message.get("tokens")
        if not isinstance(used, dict) or not {"input", "output"} <= used.keys():
            return False
        cache = used.get("cache", {})
        if not isinstance(cache, dict):
            return False
        counts = [
            used["input"],
            used["output"],
            used.get("reasoning", 0),
            cache.get("read", 0),
            cache.get("write", 0),
        ]
        return all(type(n) is int and n >= 0 for n in counts)

    complete = (
        bool(messages)
        and observed_messages <= messages.keys()
        and all(
            recorded_usage(m) and (m.get("time") or {}).get("completed") for m in messages.values()
        )
    )
    total = None
    if complete:
        used = [m["tokens"] for m in messages.values()]
        cached = sum((u.get("cache") or {}).get("read") or 0 for u in used)
        total = tokens(
            sum(u.get("input") or 0 for u in used) + cached,
            cached,
            sum((u.get("output") or 0) + (u.get("reasoning") or 0) for u in used),
        )
    events.append(
        event(
            "result",
            name="OpenCode session accounting",
            tokens=total,
            usage_complete=complete,
            text="Complete session export, including reasoning output."
            if complete
            else "Incomplete session export. Recorded steps are not a full run total.",
        )
    )
    return events


def read_codex(agent: Path, model: str) -> list[dict]:
    events: list[dict] = []
    pending: dict = {}
    anchor: float | None = None
    # The exec call that is running. Commands that end while it runs belong to it; a
    # background command that ends later stays a separate event.
    running: dict | None = None
    for path in sorted((agent / "sessions").rglob("rollout-*.jsonl")):
        for record in jsonl(path):
            payload = record.get("payload") or {}
            at = epoch_ms(record.get("timestamp"))
            kind = payload.get("type")
            if record.get("type") == "token_usage_record":
                used = payload.get("usage") or {}
                ms = at - anchor if at is not None and anchor is not None else None
                events.append(
                    call(
                        model,
                        at,
                        ms,
                        "derived",
                        tokens(
                            used.get("input_tokens"),
                            used.get("cached_input_tokens"),
                            used.get("output_tokens"),
                        ),
                    )
                )
                anchor = at
            elif kind == "task_started":
                anchor = at
            elif kind == "message" and payload.get("role") in ("user", "assistant"):
                label = "user" if payload["role"] == "user" else "assistant"
                text = text_of(payload.get("content"))
                if text:
                    events.append(event(label, at=at, text=text))
                if label == "user":
                    anchor = at
            elif kind == "reasoning":
                text = text_of(payload.get("summary"))
                if text:
                    events.append(event("reasoning", at=at, text=text))
            elif kind in ("custom_tool_call", "function_call"):
                tool = event(
                    "tool",
                    at=at,
                    name=payload.get("name"),
                    input=payload.get("input") or payload.get("arguments"),
                )
                pending[payload.get("call_id")] = tool
                events.append(tool)
                running = tool
            elif kind in ("custom_tool_call_output", "function_call_output"):
                output = payload.get("output")
                text = text_of(output) if isinstance(output, list) else str(output or "")
                target = pending.get(payload.get("call_id"))
                if target is running:
                    running = None
                wall = WALL_TIME.search(text or "")
                if wall:
                    ms, source = float(wall.group(1)) * 1000, "recorded"
                elif target is not None and target["at"] is not None and at is not None:
                    ms, source = at - target["at"], "derived"
                else:
                    ms, source = None, None
                pair(
                    pending,
                    payload.get("call_id"),
                    output=text,
                    ms=ms,
                    ms_source=source,
                    error=(text or "").startswith("Script failed"),
                )
                anchor = at
            elif kind == "item_completed":
                item = payload.get("item") or {}
                into = running["commands"] if running is not None else events
                if item.get("type") == "CommandExecution":
                    command = item.get("command") or []
                    shown = command[-1] if command[:1] == ["/bin/bash"] else " ".join(command)
                    took = item.get("duration") or {}
                    ms = (took.get("secs") or 0) * 1000 + (took.get("nanos") or 0) / 1e6
                    into.append(
                        event(
                            "command",
                            at=at,
                            name="shell",
                            input=shown,
                            output=item.get("aggregated_output"),
                            error=item.get("status") == "failed"
                            or item.get("exit_code") not in (0, None),
                            ms=ms if took else None,
                            ms_source="recorded" if took else None,
                        )
                    )
                elif item.get("type") == "FileChange":
                    changes = item.get("changes") or {}
                    into.append(
                        event(
                            "command",
                            at=at,
                            name="file change",
                            input="\n".join(f"{c.get('type')} {p}" for p, c in changes.items()),
                            output="\n\n".join(
                                c.get("unified_diff") or "" for c in changes.values()
                            ),
                            error=item.get("status") == "failed",
                        )
                    )
    return events


def read_dirge(agent: Path, model: str) -> list[dict]:
    events: list[dict] = []
    pending: dict = {}
    seen: set = set()
    path = agent / "dirge.jsonl"
    immediate = path.is_file() and any(r.get("type") == "ctb_tool_call" for r in jsonl(path))
    for record in jsonl(path) if path.is_file() else []:
        message = record.get("message") or {}
        if record.get("type") == "ctb_tool_call":
            tool = event(
                "tool",
                name=record.get("name"),
                input=record.get("input"),
                at=record.get("at_ms"),
            )
            pending[record.get("id")] = tool
            events.append(tool)
        elif record.get("type") == "ctb_tool_result":
            if record.get("id") not in pending:
                events.append(event("user", name="Unpaired tool result", text=record.get("output")))
                continue
            duration = record.get("duration_ms")
            pair(
                pending,
                record.get("id"),
                output=record.get("output"),
                ms=duration,
                ms_source="recorded" if duration is not None else None,
            )
        elif record.get("type") == "assistant":
            key = message.get("id")
            if key is None or key not in seen:
                if key is not None:
                    seen.add(key)
                used = message.get("usage")
                events.append(
                    call(
                        model,
                        None,
                        None,
                        None,
                        tokens(
                            used.get("input_tokens"),
                            used.get("cached_input_tokens") or used.get("cache_read_input_tokens"),
                            used.get("output_tokens"),
                        )
                        if used
                        else None,
                    )
                )
            for part in message.get("content") or []:
                kind = part.get("type")
                if kind == "text" and part.get("text"):
                    events.append(event("assistant", text=part["text"]))
                elif kind == "thinking" and part.get("thinking"):
                    events.append(event("reasoning", text=part["thinking"]))
                elif kind == "tool_use" and not immediate:
                    tool = event("tool", name=part.get("name"), input=part.get("input"))
                    pending[part.get("id")] = tool
                    events.append(tool)
        elif record.get("type") == "user":
            content = message.get("content")
            if isinstance(content, str):
                events.append(event("user", text=content))
            for part in content if isinstance(content, list) else []:
                if part.get("type") == "tool_result" and not immediate:
                    pair(
                        pending,
                        part.get("tool_use_id"),
                        output=text_of(part.get("content")),
                        error=part.get("is_error"),
                    )
                elif part.get("type") == "text":
                    events.append(event("user", text=part.get("text")))
        elif record.get("type") == "result":
            used = record.get("usage") or {}
            # Dirge writes 0.0 when it has no price for the model, so only a positive cost counts.
            cost = record.get("total_cost_usd") or None
            events.append(
                event(
                    "result",
                    name=record.get("subtype"),
                    error=bool(record.get("is_error")),
                    ms=record.get("duration_ms"),
                    ms_source="recorded" if record.get("duration_ms") is not None else None,
                    tokens=tokens(
                        used.get("input_tokens"),
                        used.get("cached_input_tokens") or used.get("cache_read_input_tokens"),
                        used.get("output_tokens"),
                    )
                    if used
                    else None,
                    cost=cost,
                    cost_source="recorded" if cost is not None else None,
                )
            )
    return events


def edn_plain(value: object) -> object:
    """EDN values as JSON-like Python: keywords become their names."""
    if isinstance(value, edn_format.Keyword):
        return value.name
    if isinstance(value, Mapping):
        return {str(edn_plain(k)): edn_plain(v) for k, v in value.items()}
    if isinstance(value, (frozenset, set)) or (
        isinstance(value, Sequence) and not isinstance(value, str)
    ):
        return [edn_plain(v) for v in value]
    return value


def read_kmet(agent: Path, model: str) -> list[dict]:
    events: list[dict] = []
    pending: dict = {}
    previous: float | None = None
    for path in sorted((agent / "kmet-sessions").rglob("*.ednl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                entry = edn_plain(edn_format.loads(line))
            except edn_format.EDNDecodeError:
                # One bad line, such as a cut-off tail, must not hide the transcript.
                continue
            if not isinstance(entry, dict):
                continue
            at = epoch_ms(entry.get("timestamp"))
            role = entry.get("role")
            if role == "user":
                events.append(event("user", at=at, text=text_of(entry.get("content"))))
            elif role == "assistant":
                used = entry.get("usage") or {}
                cached = (used.get("input_tokens_details") or {}).get("cached_tokens")
                ms = at - previous if at is not None and previous is not None else None
                events.append(
                    call(
                        entry.get("model") or model,
                        at,
                        ms,
                        "derived",
                        tokens(used.get("input_tokens"), cached, used.get("output_tokens")),
                        (used.get("cost") or {}).get("total"),
                    )
                )
                text = text_of(entry.get("content"))
                if text:
                    events.append(event("assistant", at=at, text=text))
                for item in entry.get("tool-calls") or []:
                    tool = event("tool", at=at, name=item.get("name"), input=item.get("arguments"))
                    pending[item.get("id")] = tool
                    events.append(tool)
            elif role == "tool":
                for part in entry.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    target = pending.get(part.get("tool_use_id"))
                    start = target["at"] if target else None
                    pair(
                        pending,
                        part.get("tool_use_id"),
                        output=text_of(part.get("content")) or part.get("content"),
                        error=bool(entry.get("is-error")),
                        ms=at - start if at is not None and start is not None else None,
                        ms_source="derived",
                    )
            if at is not None:
                previous = at
    return events


def read_samizdat(agent: Path, model: str) -> list[dict]:
    path = agent / "samizdat-transcript.json"
    if not path.is_file():
        return []
    body = json.loads(path.read_text(encoding="utf-8"))
    turns = [t for branch in body.get("branches") or [] for t in branch.get("turns") or []]
    turns.sort(key=lambda t: (t.get("created_at") or "", t.get("turn") or 0))
    events: list[dict] = []
    for turn in turns:
        at = epoch_ms(turn.get("created_at"))
        branch = turn.get("branch_id")
        events.append(
            call(
                model,
                at,
                turn.get("elapsed_ms"),
                "recorded",
                tokens(
                    turn.get("prompt_tokens"),
                    turn.get("cache_hit_tokens"),
                    turn.get("completion_tokens"),
                ),
            )
        )
        events[-1]["name"] = branch
        if turn.get("reasoning_text"):
            events.append(event("reasoning", at=at, text=turn["reasoning_text"], name=branch))
        if turn.get("assistant_text"):
            events.append(event("assistant", at=at, text=turn["assistant_text"], name=branch))
        if turn.get("tool_name"):
            failed = (
                bool(turn.get("parse_error"))
                or bool(turn.get("policy_refusal"))
                or (turn.get("category") == "failure")
            )
            events.append(
                event(
                    "tool",
                    at=at,
                    name=turn["tool_name"],
                    input=turn.get("args"),
                    output=turn.get("full_result") or turn.get("result"),
                    error=failed,
                )
            )
    return events


#: Streaming deltas: the iteration-final record holds the same text whole.
VIS_DELTAS = tuple(
    f'{{"event":"trace-chunk","payload":{{"phase":"{phase}"' for phase in ("content", "reasoning")
)


def vis_trace(agent: Path) -> Path | None:
    for name in ("vis-trace.jsonl.zst", "vis-trace.jsonl.gz", "vis-trace.jsonl"):
        if (agent / name).is_file():
            return agent / name
    return None


def read_vis(agent: Path, model: str) -> list[dict]:
    path = vis_trace(agent)
    if path is None:
        return []
    events: list[dict] = []
    calls: dict = {}
    pending: dict = {}
    for record in jsonl(path, VIS_DELTAS):
        payload = record.get("payload") or {}
        phase = payload.get("phase")
        iteration = payload.get("iteration")
        if phase == "provider-call":
            entry = call(
                payload.get("model") or model, payload.get("started-at-ms"), None, None, None
            )
            calls[iteration] = entry
            events.append(entry)
        elif phase == "response-parse" and payload.get("provider-duration-ms") is not None:
            entry = calls.pop(iteration, None)
            if entry is not None:
                entry["ms"], entry["ms_source"] = payload["provider-duration-ms"], "recorded"
        elif phase == "iteration-final":
            if payload.get("thinking"):
                events.append(event("reasoning", text=payload["thinking"]))
            if payload.get("assistant-prose"):
                events.append(event("assistant", text=payload["assistant-prose"]))
        elif phase == "form-start":
            tool = event(
                "tool",
                at=payload.get("started-at-ms"),
                name=payload.get("tool-name"),
                input=payload.get("code"),
            )
            pending[payload.get("scope")] = tool
            events.append(tool)
        elif phase == "form-result":
            envelope = payload.get("envelope") or {}
            error = payload.get("error")
            start, end = envelope.get("started-at-ms"), envelope.get("finished-at-ms")
            output = payload.get("stdout") or ""
            if error:
                message = error.get("message") if isinstance(error, dict) else str(error)
                output = f"{output}\n{message}".strip()
            pair(
                pending,
                payload.get("scope"),
                output=output,
                error=bool(error) or bool(payload.get("timeout?")),
                ms=end - start if start is not None and end is not None else None,
                ms_source="recorded" if start is not None and end is not None else None,
            )
        elif record.get("event") == "result":
            for block in payload.get("content") or []:
                text = block.get("markdown") or block.get("message")
                if text:
                    events.append(event("assistant", text=text))
            used, cost = payload.get("tokens") or {}, payload.get("cost") or {}
            events.append(
                event(
                    "result",
                    name=payload.get("status"),
                    error=payload.get("status") == "error",
                    ms=payload.get("duration-ms"),
                    ms_source="recorded" if payload.get("duration-ms") is not None else None,
                    tokens=tokens(used.get("input"), used.get("cached"), used.get("output"))
                    if used
                    else None,
                    cost=cost.get("total_cost"),
                    cost_source="recorded" if cost.get("total_cost") is not None else None,
                )
            )
    return events


def wisp_log(path: Path) -> tuple[list[list], dict[str, float]]:
    """Start time and HTTP milliseconds of each request round, and milliseconds of each tool call."""
    rounds: list[list] = []
    tools: dict[str, float] = {}
    if not path.is_file():
        return rounds, tools
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stamp, _, rest = line.partition(" ")
        fields = dict(f.split("=", 1) for f in rest.split() if "=" in f)
        try:
            if rest.startswith("request "):
                rounds.append([float(stamp), None])
            elif rest.startswith("http_done ") and rounds:
                rounds[-1][1] = float(fields["ms"])
            elif rest.startswith("tool end "):
                tools[fields["call_id"]] = float(fields["ms"])
        except (KeyError, ValueError):
            continue
    return rounds, tools


def read_wisp(agent: Path, model: str) -> list[dict]:
    events: list[dict] = []
    for session in sorted(p for p in (agent / "wisp-sessions").glob("*") if p.is_dir()):
        path = session / "wisp.jsonl"
        if not path.is_file():
            continue
        # wisp.jsonl has no times; wisp.log has one request round for each assistant record.
        rounds, tool_ms = wisp_log(session / "wisp.log")
        pending: dict = {}
        turn = 0
        for record in jsonl(path):
            kind = record.get("type")
            if kind == "user":
                events.append(event("user", text=text_of(record.get("content"))))
            elif kind == "assistant":
                at, ms = rounds[turn] if turn < len(rounds) else (None, None)
                turn += 1
                used = [u for u in record.get("usage") or [] if isinstance(u, dict)]
                events.append(
                    call(
                        model,
                        at,
                        ms,
                        "recorded",
                        tokens(
                            sum(u.get("prompt_tokens") or 0 for u in used),
                            sum(
                                (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                                for u in used
                            ),
                            sum(u.get("completion_tokens") or 0 for u in used),
                        )
                        if used
                        else None,
                    )
                )
                for item in record.get("output") or []:
                    if item.get("type") == "text" and item.get("text"):
                        events.append(event("assistant", at=at, text=item["text"]))
                    elif item.get("type") == "tool_call":
                        tool = event(
                            "tool", at=at, name=item.get("name"), input=item.get("arguments")
                        )
                        pending[item.get("id")] = tool
                        events.append(tool)
            elif kind == "tool":
                key = record.get("call_id")
                ms = tool_ms.get(key)
                pair(
                    pending,
                    key,
                    output=text_of(record.get("content")),
                    error=record.get("exit") not in (0, None),
                    ms=ms,
                    ms_source="recorded" if ms is not None else None,
                )
    return events


#: CTRF test statuses; any other value is shown as "other".
TEST_STATUSES = ("passed", "failed", "skipped", "pending", "other")
#: The largest service artifact that the verification view reads as text.
MAX_ARTIFACT = 1_000_000
CTRF = re.compile(r"ctrf(?:_(\d+))?\.json")


def ctrf_run(path: Path, label: str, *, failed_only: bool) -> list[dict]:
    """One run summary and its tests from a CTRF report. A repeat run lists only failed tests."""
    try:
        results = json.loads(path.read_text(encoding="utf-8")).get("results") or {}
    except (OSError, ValueError):
        return [
            {
                "kind": "suite",
                "name": label,
                "status": "other",
                "ms": None,
                "text": f"{path.name} is not a readable CTRF report.",
            }
        ]
    summary = results.get("summary") or {}
    tests = [t for t in results.get("tests") or [] if isinstance(t, dict)]

    def status(value: object) -> str:
        return value if value in TEST_STATUSES else "other"

    counts = Counter(status(t.get("status")) for t in tests)
    start, stop = summary.get("start"), summary.get("stop")
    entries = [
        {
            "kind": "suite",
            "name": label,
            "status": "failed" if counts["failed"] else "passed" if tests else "other",
            "ms": stop - start if isinstance(start, int) and isinstance(stop, int) else None,
            "text": f"{len(tests)} tests: "
            + ", ".join(
                f"{counts[s]} {s}" for s in TEST_STATUSES if counts[s] or s in ("passed", "failed")
            ),
        }
    ]
    for test in tests:
        if failed_only and status(test.get("status")) != "failed":
            continue
        detail = "\n\n".join(str(test[k]) for k in ("message", "trace") if test.get(k))
        duration = test.get("duration")
        entries.append(
            {
                "kind": "test",
                "name": str(test.get("name") or "unnamed test"),
                "status": status(test.get("status")),
                "ms": int(duration) if isinstance(duration, (int, float)) else None,
                "text": clip(detail or None),
            }
        )
    return entries


def read_verification(trial: Path) -> list[dict]:
    """What the verifier ran: CTRF test runs, its output and the logs of task services."""
    verifier = trial / "verifier"
    entries: list[dict] = []
    # ctrf.json is the scored run; ctrf_<n>.json are repeat runs of tasks that check determinism.
    runs = sorted(
        (int(m[1] or 0), p) for p in verifier.glob("ctrf*.json") if (m := CTRF.fullmatch(p.name))
    )
    for number, path in runs:
        label = f"Repeat run {number}" if number else "Test run"
        entries.extend(ctrf_run(path, label, failed_only=bool(number)))
    stdout = verifier / "test-stdout.txt"
    if stdout.is_file():
        text = stdout.read_text(encoding="utf-8", errors="replace")
        entries.append(
            {
                "kind": "log",
                "name": "Verifier output",
                "status": None,
                "ms": None,
                "text": clip(text) or None,
            }
        )
    manifest = trial / "artifacts" / "manifest.json"
    try:
        artifacts = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else []
    except ValueError:
        artifacts = []
    for item in artifacts if isinstance(artifacts, list) else []:
        # Only logs of task services; the agent's own files are not verification output.
        if not isinstance(item, dict) or not item.get("service") or item.get("type") != "file":
            continue
        destination = str(item.get("destination") or "")
        path = trial / destination
        if (
            item.get("status") != "ok"
            or not destination.startswith("artifacts/")
            or ".." in Path(destination).parts
            or not path.is_file()
            or path.is_symlink()
            or path.stat().st_size > MAX_ARTIFACT
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        entries.append(
            {
                "kind": "log",
                "name": f"{item['service']}: {item.get('source')}",
                "status": None,
                "ms": None,
                "text": clip(text) or None,
            }
        )
    return entries


READERS = {
    "pi": read_pi,
    "opencode": read_opencode,
    "codex": read_codex,
    "dirge": read_dirge,
    "kmet": read_kmet,
    "samizdat": read_samizdat,
    "vis": read_vis,
    "wisp": read_wisp,
}


def harness(name: str) -> str | None:
    """The reader key of a Harbor agent name, like vis-native or codex-gpt."""
    return next((key for key in READERS if name.startswith(key)), None)


#: A blank line between two lines that are each one bold heading, as reasoning summaries write them.
HEADING_GAP = re.compile(r"(?m)^(\*\*[^\n]+\*\*)[ \t]*\n\s*\n(?=\*\*[^\n]+\*\*[ \t]*$)")


def merge_reasoning(events: list[dict]) -> list[dict]:
    """One reasoning event for each run of reasoning events with nothing between them.

    Harnesses write each reasoning summary part as its own record, often one short heading.
    Headings that follow each other are one line apart; a paragraph keeps its blank lines.
    """
    merged: list[dict] = []
    for e in events:
        last = merged[-1] if merged else None
        if last is not None and e["kind"] == last["kind"] == "reasoning":
            last["text"] = "\n\n".join(t for t in (last["text"], e["text"]) if t) or None
        else:
            merged.append(dict(e) if e["kind"] == "reasoning" else e)
    for e in merged:
        if e["kind"] == "reasoning" and e["text"]:
            e["text"] = clip(HEADING_GAP.sub(r"\1\n", e["text"]))
    return merged


def read_events(key: str, agent: Path, model: str) -> list[dict]:
    """The transcript events of one trial, read by the reader of harness `key`."""
    return merge_reasoning(READERS[key](agent, model))


def instruction(trial: Path, result: dict) -> str | None:
    """The task prompt: the first user step of the trajectory, else the task's instruction.md."""
    path = trial / "agent" / "trajectory.json"
    if path.is_file():
        for step in json.loads(path.read_text(encoding="utf-8")).get("steps") or []:
            if step.get("source") == "user" and step.get("message"):
                return text_of(step["message"])
    task = ((result.get("config") or {}).get("task") or {}).get("path")
    if not task:
        return None
    # Older rounds name the dataset by an absolute path of an earlier layout.
    for candidate in (Path(task), DATASETS / Path(task).name):
        if (candidate / "instruction.md").is_file():
            return (candidate / "instruction.md").read_text(encoding="utf-8")
    return None

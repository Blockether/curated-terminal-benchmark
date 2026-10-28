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

Nothing is invented: a value that a harness does not write stays None, and the reader adds
a note to `notes` that the report shows.
"""

import gzip
import io
import json
import re
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


# Harness readers. Each returns (events, notes).


def read_pi(agent: Path, model: str) -> tuple[list[dict], list[str]]:
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
    return events, []


def read_opencode(agent: Path, model: str) -> tuple[list[dict], list[str]]:
    events: list[dict] = []
    step: dict | None = None
    step_start = 0.0
    step_tools = 0.0
    path = agent / "opencode.txt"
    for record in jsonl(path) if path.is_file() else []:
        kind, part, at = record.get("type"), record.get("part") or {}, record.get("timestamp")
        if kind == "step_start":
            step, step_start, step_tools = call(model, at, None, None, None), at or 0, 0.0
            events.append(step)
        elif kind == "step_finish" and step is not None:
            used = part.get("tokens") or {}
            cache = (used.get("cache") or {}).get("read") or 0
            done = call(
                model,
                step_start,
                max(0.0, (at or step_start) - step_start - step_tools),
                "derived",
                tokens(
                    (used.get("input") or 0) + cache,
                    cache,
                    (used.get("output") or 0) + (used.get("reasoning") or 0),
                ),
                part.get("cost"),
            )
            step.update(done)
            step = None
        elif kind in ("text", "reasoning") and part.get("text"):
            label = "assistant" if kind == "text" else "reasoning"
            events.append(event(label, at=at, text=part["text"]))
        elif kind == "tool_use":
            state = part.get("state") or {}
            time = state.get("time") or {}
            ms = time["end"] - time["start"] if time.get("end") and time.get("start") else None
            step_tools += ms or 0
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
    notes = ["Provider time is each step's time minus the tool time inside it."]
    return events, notes


def read_codex(agent: Path, model: str) -> tuple[list[dict], list[str]]:
    events: list[dict] = []
    pending: dict = {}
    anchor: float | None = None
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
            elif kind in ("custom_tool_call_output", "function_call_output"):
                output = payload.get("output")
                text = text_of(output) if isinstance(output, list) else str(output or "")
                target = pending.get(payload.get("call_id"))
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
                if item.get("type") == "CommandExecution":
                    command = item.get("command") or []
                    shown = command[-1] if command[:1] == ["/bin/bash"] else " ".join(command)
                    took = item.get("duration") or {}
                    ms = (took.get("secs") or 0) * 1000 + (took.get("nanos") or 0) / 1e6
                    events.append(
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
                    events.append(
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
    notes = [
        (
            "Provider time runs from the previous tool result or provider call to the next "
            "token record."
        ),
        "Commands are the shell runs inside each exec script.",
    ]
    return events, notes


def read_dirge(agent: Path, model: str) -> tuple[list[dict], list[str]]:
    events: list[dict] = []
    pending: dict = {}
    seen: set = set()
    path = agent / "dirge.jsonl"
    for record in jsonl(path) if path.is_file() else []:
        message = record.get("message") or {}
        if record.get("type") == "assistant":
            key = message.get("id") or id(record)
            if key not in seen:
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
                elif kind == "tool_use":
                    tool = event("tool", name=part.get("name"), input=part.get("input"))
                    pending[part.get("id")] = tool
                    events.append(tool)
        elif record.get("type") == "user":
            content = message.get("content")
            if isinstance(content, str):
                events.append(event("user", text=content))
            for part in content if isinstance(content, list) else []:
                if part.get("type") == "tool_result":
                    pair(
                        pending,
                        part.get("tool_use_id"),
                        output=text_of(part.get("content")),
                        error=part.get("is_error"),
                    )
                elif part.get("type") == "text":
                    events.append(event("user", text=part.get("text")))
    notes = ["Dirge writes no times for single events, so call and tool times are not recorded."]
    return events, notes


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


def read_kmet(agent: Path, model: str) -> tuple[list[dict], list[str]]:
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
    notes = ["Provider and tool times come from the times of consecutive session entries."]
    return events, notes


def read_samizdat(agent: Path, model: str) -> tuple[list[dict], list[str]]:
    path = agent / "samizdat-transcript.json"
    if not path.is_file():
        return [], []
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
    notes = [
        (
            "Samizdat writes no tool times. A tool error is a turn in category failure, a parse "
            "error or a policy refusal."
        )
    ]
    return events, notes


#: Streaming deltas: the iteration-final record holds the same text whole.
VIS_DELTAS = tuple(
    f'{{"event":"trace-chunk","payload":{{"phase":"{phase}"' for phase in ("content", "reasoning")
)


def vis_trace(agent: Path) -> Path | None:
    for name in ("vis-trace.jsonl.zst", "vis-trace.jsonl.gz", "vis-trace.jsonl"):
        if (agent / name).is_file():
            return agent / name
    return None


def read_vis(agent: Path, model: str) -> tuple[list[dict], list[str]]:
    path = vis_trace(agent)
    if path is None:
        return [], []
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
    notes = ["Vis writes the tokens and the cost of the whole run, not of each provider call."]
    return events, notes


READERS = {
    "pi": read_pi,
    "opencode": read_opencode,
    "codex": read_codex,
    "dirge": read_dirge,
    "kmet": read_kmet,
    "samizdat": read_samizdat,
    "vis": read_vis,
}


def harness(name: str) -> str | None:
    """The reader key of a Harbor agent name, like vis-native or codex-gpt."""
    return next((key for key in READERS if name.startswith(key)), None)


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

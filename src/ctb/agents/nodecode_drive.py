"""Drive one Nodecode RPC turn and write each frame to the trial log."""

import json
import os
import subprocess
import sys
from pathlib import Path

LOG_DIR = Path(os.environ.get("CTB_NODECODE_LOG_DIR", "/logs/agent"))
FRAMES = LOG_DIR / "nodecode-rpc.jsonl"
STDERR = LOG_DIR / "nodecode-stderr.log"
TERMINAL = {"turn.completed", "turn.failed", "turn.cancelled"}
EXIT_S = int(os.environ.get("CTB_NODECODE_EXIT_S", "120"))


def frame_event(line: str) -> tuple[dict, dict]:
    """The gateway payload and the session-stream event of one JSON line."""
    try:
        obj = json.loads(line)
    except ValueError:
        return {}, {}
    payload = obj.get("payload") if isinstance(obj, dict) else None
    if not isinstance(payload, dict):
        return {}, {}
    frame = payload.get("frame")
    event = frame.get("event") if isinstance(frame, dict) else None
    return payload, event if isinstance(event, dict) else {}


def turn_id_of(payload: dict, event: dict) -> str | None:
    """The turn id of a command result or a session-stream event."""
    if payload.get("type") == "gateway_turn_command_result":
        result = payload.get("result") or {}
        response = result.get("response") or {}
        return response.get("turn_id")
    metadata = event.get("metadata")
    return metadata.get("turn_id") if isinstance(metadata, dict) else None


def main() -> int:
    prompt = Path(os.environ["CTB_PROBLEM_FILE"]).read_text()
    binary = sys.argv[1] if len(sys.argv) > 1 else "nodecode"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    status = "missing"
    admitted = None
    error = None
    with STDERR.open("w") as stderr:
        proc = subprocess.Popen(
            [binary, "rpc"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            bufsize=1,
            cwd=os.getcwd(),
        )
        with FRAMES.open("w") as frames:
            proc.stdin.write(
                json.dumps({"type": "start_turn", "request": {"prompt": prompt}}) + "\n"
            )
            proc.stdin.flush()
            for line in proc.stdout:
                frames.write(line if line.endswith("\n") else line + "\n")
                frames.flush()
                payload, event = frame_event(line)
                kind = event.get("type")
                turn_id = turn_id_of(payload, event)
                if payload.get("type") == "gateway_turn_command_result" and turn_id:
                    admitted = turn_id
                    result = payload.get("result") or {}
                    if result.get("status") not in ("accepted", "queued"):
                        error = result
                        status = "failed"
                        break
                if kind in TERMINAL and (admitted is None or turn_id in (None, admitted)):
                    status = kind
                    if kind != "turn.completed":
                        error = event.get("payload")
                    break
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
            try:
                proc.wait(timeout=EXIT_S)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
    if error is not None:
        print(json.dumps(error)[:4000], flush=True)
    print(f"nodecode rpc: {status}", flush=True)
    return 0 if status == "turn.completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

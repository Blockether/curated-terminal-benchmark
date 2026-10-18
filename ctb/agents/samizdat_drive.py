"""Run the released Samizdat server and retain full conversation records."""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

TERMINAL = {"completed", "aborted", "failed", "interrupted", "exhausted", "abandoned"}


def request(path, body=None):
    token = (Path.home() / ".config/samizdat/http/3985.token").read_text().strip()
    req = urllib.request.Request(
        "http://127.0.0.1:3985" + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as response:
        return json.load(response)


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def export_transcript(run_id, detail, output, get=request):
    branches = []
    for branch in detail.get("branches", []):
        branch_id = branch["id"]
        full = get(f"/v1/runs/{run_id}/branches/{branch_id}")
        full["turns"] = [
            get(f"/v1/runs/{run_id}/branches/{branch_id}/turns/{turn['turn']}")
            for turn in full.get("turns", [])
        ]
        branches.append(full)
    save(output, {"run_id": run_id, "run": detail.get("run"), "branches": branches})


def main():
    out = Path("/logs/agent")
    deadline = time.monotonic() + 300
    while True:
        try:
            request("/v1/runs")
            break
        except (OSError, urllib.error.URLError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
    started = request(
        "/v1/runs",
        {
            "problem": Path(os.environ["CTB_PROBLEM_FILE"]).read_text(),
            "beam_width": 1,
        },
    )
    if started.get("error") or not started.get("run_id"):
        raise RuntimeError(f"Run rejected: {started}")
    run_id = started["run_id"]
    while True:
        detail = request(f"/v1/runs/{run_id}")
        save(out / "samizdat-run.json", detail)
        export_transcript(run_id, detail, out / "samizdat-transcript.json")
        status = detail["run"]["status"]
        if status in TERMINAL:
            print(f"Run {run_id}: {status}", flush=True)
            return
        time.sleep(10)


if __name__ == "__main__":
    main()

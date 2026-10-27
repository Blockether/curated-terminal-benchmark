# Curated Terminal-Bench

Compare coding agents on 12 CPU-only Terminal-Bench tasks with Harbor.
New runs use **GPT `gpt-6.1-sol`, medium reasoning**.

Agents: Codex, OpenCode, Pi, Dirge, Kmet, Samizdat, Vis solo and Vis team.
Claude Code is not part of the active configuration.

## Run

Requires Python 3.12+, uv, a running Podman connection, and a valid Vis ChatGPT login.
Runs consume subscription quota. Configure `CONTAINER_CONNECTION` and `DOCKER_HOST` for your Podman machine.

```sh
uv sync
uv run python -m ctb.cli fetch
uv run python -m ctb.cli run --round my-round --agents codex --models openai --parallel 1
uv run python -m ctb.cli seal --round my-round --agents codex --models openai
uv run python -m ctb.cli metrics --round my-round
```

Start from a clean commit. Use a new round name for each repeat.
`seal` and `metrics` write `results/<round>.json` and `results/<round>-metrics.json`.

## Privacy and seals

Transcripts, credentials and task artifacts stay local; do not commit `jobs/` or `runs/`.
Commit only results, metrics and manifests containing hashes.
The seal hook checks hashes, evidence coverage and credentials before accepting a seal commit.
Stage the transcript manifest with its result seals; keep the referenced evidence available locally.
Missing files, unsafe links or incomplete scans block the commit.

Enable the tracked hook in your checkout after installing dependencies:

```sh
git config core.hooksPath .githooks
```

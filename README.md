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

## Results

Round `gpt-all-482f2f5`: GPT (`openai`) on all 12 tasks, one trial each, 2 h agent limit. Harness commits `482f2f5` and `ad88d89`.

| Agent | Pass | Agent min | Cost USD |
| --- | --- | --- | --- |
| codex 0.163.0 | 7/12 | 118.7 | 5.29 |
| opencode 1.19.3 | 6/12 | 103.4 | 2.96 |
| pi 1.3.0 | 5/12 | 87.0 | 2.73 |
| kmet f249f0bf | 2/12 | 67.5 | 1.76 |
| dirge 45377633 | 1/12 | 358.4 | 21.26 |
| samizdat 97727796 | 0/12 | 686.5 | 91.27 |

The seal, metrics and transcript manifest are in `results/gpt-all-482f2f5*.json`. Notes:

- kmet: 2 runs ended on a TLS `bad_record_mac` from the Codex endpoint. kmet does not retry.
- dirge: 2 runs hung after one minute until the 2 h limit. One verifier timed out after 1800 s.
- samizdat: runs stop at its own turn limit (`gates.edn`, 200 turns for a small change). 2 runs hit the 2 h limit.

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

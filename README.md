# Curated Terminal-Bench comparison

This repository compares coding agents on 12 CPU-only tasks from Terminal-Bench 4.0.
It answers one question: does Vis with Council subagents solve more tasks than Vis alone,
and how do both compare with other agents on the same models?

## When to use

- You changed Vis subagents or Council, and you want a PASS rate on real tasks.
- You want to compare Vis with Claude Code, Codex, dirge, kmet or samizdat on the same model.
- You want a small, repeatable suite that runs on one machine without a GPU.

## What it runs

Each trial is one agent, one model and one task in a separate Harbor job.

| Agent | Source | Models |
|---|---|---|
| `vis-solo` | Native Vis Linux build of `profiles.toml`, profile `solo`: Council off | Opus 5.5, GPT-6.1 |
| `vis-team` | Native Vis Linux build of `profiles.toml`, profile `team`: Council on (subagents) | Opus 5.5, GPT-6.1 |
| `claude-code` | Harbor built-in agent | Opus 5.5 |
| `codex` | Harbor built-in agent | GPT-6.1 |
| `dirge` | `yogthos/dirge` master, static musl build, with `patches/dirge-usage.patch` | Opus 5.5, GPT-6.1 |
| `kmet` | `kmetia/kmet-agent` master, with `patches/kmet-print-thinking.patch` and `patches/kmet-usage.patch` | GPT-6.1 |
| `samizdat` | `yogthos/samizdat` master on Jolt 0.8.20, one beam branch, through the model proxy | Opus 5.5, GPT-6.1 |
| `opencode` | Harbor built-in agent, `opencode-ai` 1.19.0, model variant `medium` | GPT-6.1 |
| `pi` | Harbor built-in agent, pi 1.2.0, provider `openai-codex`, `--thinking medium` | GPT-6.1 |

The models are `anthropic/claude-opus-5-5` and `openai/gpt-6.1-sol`. All agents use medium
reasoning effort. `tasks.toml` lists the tasks: 6 easy tasks (expert estimate at most 2 hours)
and 6 hard tasks (4 to 8 hours). No task needs a GPU or more than 8 GB of memory.

### Limits of the comparison

- kmet runs only on GPT. kmet has no Anthropic subscription (OAuth) transport.
- OpenCode and pi run only on GPT. Anthropic bills a subscription token that a third-party app
  uses as extra usage, not as plan usage. Each run gets a ChatGPT login without a refresh token.
- kmet print mode ignores `--thinking`. The patch passes the thinking level, as interactive mode does.
- samizdat speaks only OpenAI Chat Completions. `ctb run` starts a proxy on the host
  (`ctb/proxy.py`). The proxy sends Opus requests to the Anthropic Messages API and GPT
  requests to the ChatGPT Codex API with your Vis logins. The container gets only a random
  proxy key. `runs/<round>/proxy.jsonl` records token counts and errors, not content.
- Opus 5.5 refuses a forced tool choice. For samizdat on Opus, the proxy asks for the tool
  in the prompt instead.
- samizdat runs one beam branch (`CTB_BEAM_WIDTH`, default 1), so it costs as much as one agent.
- samizdat runs on Jolt 0.8.20, not 0.8.21. Jolt 0.8.21 changed its timer clock, and on that
  version no samizdat run starts.
- dirge and kmet print no token counts. Their usage patches add the totals that each agent
  already keeps. Trials that ran before a patch have no token counts.
- A trial without a reward counts as a fail, for example after a verifier timeout.
- If Harbor does not stop a trial, `ctb` stops it after all task timeouts plus 30 minutes.
  The trial then counts as a fail with `HarnessTimeoutError`.
- An agent install gets 5 times the Harbor setup timeout of 360 seconds. npm and nvm installs
  under amd64 emulation can take longer than 360 seconds.
- Agents can run in separate `ctb run` processes on the same round. Each `run` call adds its
  record to `jobs/<round>/ctb-runs.json` under a file lock.
- One trial per cell is a small sample. Treat a difference of one task as noise.

## Prerequisites

- `uv` and Podman with an amd64 machine.
- The Terminal-Bench 4.0 dataset in `terminal_bench/artifacts/datasets/terminal-bench`.
  `CTB_DATASET` changes the path.
- The Harbor Compose provider in `terminal_bench/artifacts/docker-compose`.
  `PODMAN_COMPOSE_PROVIDER` changes the path.
- Vis subscription logins: `~/.vis/anthropic-auth.json` and `~/.vis/openai-codex-auth.json`.
  The runner copies only the access token into the container, never the refresh token.
  A token must stay valid for the agent timeout (`CTB_MIN_TOKEN_S`, default 7500 seconds).
  If the Claude token expires too soon, the runner defers its trials and runs the others.
  Then it waits for the Vis refresh window and runs one short host Vis turn
  (`vis-agent` on `PATH`), so Vis refreshes the token under its own lock.
- The `gh` CLI. `fetch` downloads the native Vis Linux build that `profiles.toml` pins: the
  workflow artifact of one `native-release.yml` dry run on Vis `main`. It checks the commit in
  the build stamp and the sha256, and keeps the bundle in `artifacts/vis-builds/<run_id>/`.
  `uv run python -m ctb.agents.vis fetch` gets only this build. To test another build, change
  the `[build]` pin. The bundle has no JVM fallback. Until the baseline round is sealed,
  `fetch` also gets the `v0.2.37` release (`VIS_RELEASE`), because that round records it.
- `profiles.toml` names the Vis profiles: toggles, extensions, effort, the session database
  and a configuration overlay. The Harbor kwarg `profile` (`--ak profile=<name>`) selects one.
  Vis writes the profile into the ATIF `trajectory.json`, and `ctb run` adds the build and the
  profiles to the run record.

Each run uses your subscription quota. A full matrix is 132 trials and takes many hours.

Give the Podman machine at most about a third of your RAM, for example
`podman machine set --memory 10240 vis-amd64`. Use `--parallel 1` on a laptop.
The task containers and the amd64 emulation can use all the memory that the machine has.

## How to run

1. Install the environment:

   ```bash
   uv sync
   ```

2. Export the Podman connection and socket:

   ```bash
   export CONTAINER_CONNECTION=vis-amd64
   export DOCKER_HOST=unix:///path/to/podman/vis-amd64-api.sock
   ```

3. Get the agents: native Vis, and dirge, kmet and samizdat from their pinned master commits:

   ```bash
   uv run python -m ctb.cli fetch
   ```

   dirge builds in an amd64 `rust:1-bookworm` container. samizdat builds in an amd64
   `python:3.12-slim-bookworm` container with a 4 GB memory limit. kmet needs Babashka
   1.13.225 or later on `PATH` or in `artifacts/tools`. To build an agent again, delete its
   archive in `artifacts/`.

4. Run a round. A finished trial is skipped when you run the same round again:

   ```bash
   uv run python -m ctb.cli run --round r2 --tasks all --parallel 1
   ```

   Use `--agents`, `--models` (`anthropic`, `openai`) and `--tasks` (`easy`, `hard` or names)
   to run a part of the matrix. `--timeout-multiplier` scales the task timeout
   (default 0.25, which is 2 hours).

5. Print the PASS table:

   ```bash
   uv run python -m ctb.cli summary --round r2
   ```

6. Seal the round when every trial has a result:

   ```bash
   uv run python -m ctb.cli seal --round r2
   ```

   `seal` writes `results/<round>.json` one time and never replaces it. The file has the
   harness commit, pins and archive hashes of each `run` call, one row for each trial and
   the SHA-256 of each `result.json`. Its `digest` is the SHA-256 of the other fields.
   `seal` refuses a round with a missing trial or a run from an uncommitted harness.
   Use the same `--agents`, `--models` and `--tasks` as for `run`. Commit the file.

Harbor writes jobs to `jobs/<round>/` and the runner writes logs to `runs/<round>/`.

## Full Vis suite

`terminal_bench/` is the full 66-task Terminal-Bench harness for Vis alone. It came from the
Vis repository and keeps its own `uv` project, README and tests. It runs the same adapter,
`ctb/agents/vis.py`, with the `solo` profile. The Vis `evals` extension runs it. Set `VIS_TERMINAL_BENCH` if this repository is not in `~/curated-terminal-benchmark`.

```bash
cd terminal_bench && uv sync --locked && uv run pytest tests
```

## Privacy

Trials hold source code, prompts, command output and sometimes credentials. They stay on this
machine:

- Each Harbor process gets `HARBOR_TELEMETRY=off`. `sitecustomize.py` also sets it in each Python
  process that has this repository on `PYTHONPATH`, so a running round stops telemetry at its next
  trial.
- `ctb` runs only `harbor run`. It refuses the Harbor Hub flags `--upload`, `--launch`, `--public`,
  `--share-org`, `--share-user` and `--stream`.
- The Harbor plugin `ctb.privacy:Redactor` replaces secret values with `[REDACTED]` in the text
  files of each trial when the trial ends, and in the job directory when the job ends. The secrets
  are the tokens in the credential files and the environment values whose names contain `KEY`,
  `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL` or `AUTH`. Binary files stay as they are.
- Each round directory under `jobs/` and `runs/` has mode `0700`. Git ignores both directories. A
  sealed result in `results/` has no trajectory content.

## Checks

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

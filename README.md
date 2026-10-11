# Curated Terminal-Bench comparison

This repository compares coding agents on 12 CPU-only tasks from Terminal-Bench 4.0.
It answers one question: does Vis with Council subagents solve more tasks than Vis alone,
and how do both compare with other agents on the same models?

## When to use

- You changed Vis subagents or Council, and you want a PASS rate on real tasks.
- You want to compare Vis with Claude Code, Codex, dirge or kmet on the same model.
- You want a small, repeatable suite that runs on one machine without a GPU.

## What it runs

Each trial is one agent, one model and one task in a separate Harbor job.

| Agent | Source | Models |
|---|---|---|
| `vis-solo` | Vis Linux bundle, Council off | Opus 5.5, GPT-6.1 |
| `vis-team` | Vis Linux bundle, Council on (subagents) | Opus 5.5, GPT-6.1 |
| `claude-code` | Harbor built-in agent | Opus 5.5 |
| `codex` | Harbor built-in agent | GPT-6.1 |
| `dirge` | `yogthos/dirge` master, static musl build | Opus 5.5, GPT-6.1 |
| `kmet` | `kmetia/kmet-agent` master, with `patches/kmet-print-thinking.patch` | GPT-6.1 |

The models are `anthropic/claude-opus-5-5` and `openai/gpt-6.1-sol`. All agents use medium
reasoning effort. `tasks.toml` lists the tasks: 6 easy tasks (expert estimate at most 2 hours)
and 6 hard tasks (4 to 8 hours). No task needs a GPU or more than 8 GB of memory.

### Limits of the comparison

- kmet runs only on GPT. kmet has no Anthropic subscription (OAuth) transport.
- kmet print mode ignores `--thinking`. The patch passes the thinking level, as interactive mode does.
- samizdat (`yogthos/samizdat`) is not in the matrix. Its master branch has only OpenAI
  Chat Completions and Ollama adapters and no one-shot command. It needs an API key or an
  OpenAI-compatible proxy.
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
- The Vis Linux bundle `artifacts/vis-agent-linux-amd64.tar.gz`. Download it from the
  `native-release.yml` workflow run of the Vis commit that you test.

Each run uses your subscription quota. A full matrix is 120 trials and takes many hours.

## How to run

1. Install the environment:

   ```bash
   uv sync
   ```

2. Build dirge and kmet from their pinned master commits:

   ```bash
   uv run python -m ctb.cli fetch
   ```

   dirge builds in an amd64 `rust:1-bookworm` container. kmet needs Babashka 1.13.225 or
   later on `PATH` or in `artifacts/tools`.

3. Export the Podman socket:

   ```bash
   export DOCKER_HOST=unix:///path/to/podman/vis-amd64-api.sock
   ```

4. Run a round. A finished trial is skipped when you run the same round again:

   ```bash
   uv run python -m ctb.cli run --round r2 --tasks all --parallel 2
   ```

   Use `--agents`, `--models` (`anthropic`, `openai`) and `--tasks` (`easy`, `hard` or names)
   to run a part of the matrix. `--agent-timeout-multiplier` scales the task timeout
   (default 0.25, which is 2 hours).

5. Print the PASS table:

   ```bash
   uv run python -m ctb.cli summary --round r2
   ```

Harbor writes jobs to `jobs/<round>/` and the runner writes logs to `runs/<round>/`.

## Full Vis suite

`terminal_bench/` is the full 66-task Terminal-Bench harness for Vis alone. It came from the
Vis repository and keeps its own `uv` project, README and tests. The Vis `evals` extension
runs it. Set `VIS_TERMINAL_BENCH` if this repository is not in `~/curated-terminal-benchmark`.

```bash
cd terminal_bench && uv sync --locked && uv run pytest tests
```

## Checks

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

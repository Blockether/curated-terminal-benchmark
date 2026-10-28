# Curated Terminal-Bench

Compare coding agents on 12 CPU-only Terminal-Bench tasks with Harbor.
New runs use **GPT `gpt-6.1-sol`, medium reasoning**.

Agents: Codex, OpenCode, Pi, Dirge, Kmet, Samizdat, Vis solo and Vis team.
Claude Code is not part of the active configuration.

## Layout

All code is in `src/ctb/`. One `pyproject.toml` and one `uv.lock` serve both runners.

| Path | Purpose |
| --- | --- |
| `src/ctb/cli.py` | The curated matrix: 12 tasks from `tasks.toml`, every agent, `fetch`, `run`, `seal`, `metrics`. |
| `src/ctb/suite/` | The Vis-only queue over the full Terminal-Bench 4.0 dataset, on Z.ai `glm-5.3-flash`. |
| `src/ctb/agents/` | One Harbor agent class for each harness. Both runners use these classes. |
| `src/ctb/toolkit.py` | The Python toolkit that every agent gets. |
| `profiles.toml` | The pinned Vis build and the Vis profiles `solo` and `team`. |
| `artifacts/` | Ignored downloads: the dataset, Docker Compose, agent bundles, the toolkit. |
| `jobs/`, `runs/` | Ignored Harbor output. The suite queue writes to `jobs/suite/`. |

Both runners start Harbor with `PYTHONPATH=src`. Harbor then imports `ctb.agents.*` and the `ctb.privacy` plugin.

## Python toolkit and Vis

Every agent gets the same Python toolkit in `/installed-agent/ctb-python`:

- The CPython of the Vis runtime (3.14.7, python-build-standalone).
- The packages of `src/ctb/toolkit-requirements.txt`: numpy, scipy, pandas, pytest and others.

`ctb fetch` builds the toolkit once for linux/amd64 in Podman. Each agent setup unpacks it.
Each agent command adds `ctb-python/bin` to the end of `PATH`. A Python of the task image still comes first.

Vis also imports these packages in its sandbox through `VIS_PYTHON_PACKAGES`. Vis gets the `py` extension (`Blockether/vis-lang-python`), installed by `ctb fetch` with the pinned Vis build.
Each Vis trial keeps its runtime files in `$HOME/.vis-runtime` through `VIS_RUNTIME_DIR`, away from the task workspace.

To pin a new Vis build, use a successful `native-release.yml` run of Blockether/vis:

```sh
uv run python -m ctb.agents.vis pin RUN_ID
uv run ctb fetch
```

## Run the curated matrix

Requires Python 3.12+, uv, a running Podman connection, and a valid Vis ChatGPT login.
Runs consume subscription quota. Configure `CONTAINER_CONNECTION` and `DOCKER_HOST` for your Podman machine.

```sh
uv sync
uv run harbor download terminal-bench/terminal-bench@4.0.0 --output-dir artifacts/datasets
uv run ctb fetch
uv run ctb run --round my-round --agents codex --models openai --parallel 1
uv run ctb seal --round my-round --agents codex --models openai
uv run ctb metrics --round my-round
```

Start from a clean commit. Use a new round name for each repeat.
`seal` and `metrics` write `results/<round>.json` and `results/<round>-metrics.json`.

## Run the full Vis suite

The queue runs `ctb.agents.vis:VisSolo` on every CPU task of Terminal-Bench 4.0. Put the key in the shell variable `ZAI_CODING_API_KEY`. Do not put it in a file or an argument.

```sh
uv run ctb fetch
uv run python -m ctb.suite.run_suite --dry-run
uv run python -m ctb.suite.run_suite --max-tasks 2
uv run python -m ctb.suite.run_suite
uv run python -m ctb.suite.run_suite --retry-task NAME
uv run python -m ctb.suite.archive_traces
uv run python -m ctb.suite.summarize
```

- The queue skips completed and running trials. It keeps up to two tasks running.
- Each task gets its own Harbor job in `jobs/suite/jobs/` and a log in `jobs/suite/runs/`.
- Use `--retry-task` only for an attempt that infrastructure interrupted. Preview it with `--dry-run`.
- The queue stops when Harbor omits results, when agent errors repeat quickly, or when free disk falls below 12 GB.
- `archive_traces` replaces finished traces with zstd after it checks their SHA-256.
- `summarize` writes `jobs/suite/runs/summary.json`. It scores each task by its latest verified attempt.

Run only one queue at a time. GPU-only tasks stay unattempted on the Podman machine.

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

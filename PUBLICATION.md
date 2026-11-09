# Public comparison and sanitized transcripts

`main` holds source code, result seals, evidence manifests and public transcript hashes.
`gh-pages` holds only the Pages workflow and `site/`: the original report layout, a text viewer, manifests,
and 60 individual `.json.gz` transcripts. Never upload `jobs/`, `runs/`, raw logs, task
artifacts, or the local HTML report. The public export is a different directory.

## Data boundary

`public_report.py` projects a closed set of agents, tasks, errors, outcomes and hashes.
`public/report-metrics.json` holds reviewed numeric measurements, phase times and known tool
labels for the same 60 result hashes. Unknown fields and tool names are refused. Rewards and
start times must match the seals. The Pages checker does not read source logs.
The public pages use `ctb.report.render` and the original report assets. Fonts are local,
with their Open Font License. Trial HTML has metadata only; transcript text stays in gzip.
`public_transcripts.py export` reads the selected local transcripts. It removes current known
credential values and their common encodings, credential-like patterns, opaque credential
fields and local user paths before compression. It does not clip text before the scan.
Each event has a fixed schema with text fields only. This normalized view is not a full raw log.
A trial that failed before its agent started has an explicit status event, not a made-up trace.
A tool call holds the commands that it ran, for example the shell commands of a Codex exec script.
The page shows them inside the row of that tool call, not as separate rows.
Reasoning summaries with no other event between them are one reasoning event.
Each transcript (schema 3) also holds what the verifier ran: CTRF test runs, the verifier output
and the logs of task services from the artifact manifest. Repeat runs list only failed tests.
The trial page shows these entries in a Verification tab next to the Implementation tab.

The export records gzip hashes in `public/transcript-hashes.json` on main. The CI checker
uses that approved manifest, not one supplied by the publication branch. Changed hashes,
unknown fields, missing or duplicate trials, extra files, hidden files, symlinks, invalid gzip
headers, trailing data and oversized expansion stop publication. The checker scans the decoded
text again. It never needs benchmark credentials or access to raw local evidence.

The browser downloads only the selected gzip file. It checks its hash and size before it
expands it with `DecompressionStream`. All event content uses `textContent`, never HTML.
There are no external assets or analytics. A current browser with gzip streaming support is required.

The workflow uses SHA-pinned actions and no persisted checkout credentials. Its check job has
read-only access. A separate deployment job can write Pages and request an identity token.
It uploads only the checked `publication/site` directory. Pushes to `gh-pages` trigger the workflow.
The checker source comes from `main`; publication files come from the triggering commit.

These controls reduce disclosure risk. They do not prove that secrets can never leak from
arbitrary text. Protect both branches, review changes to the exporter and workflow, and
restrict who can modify Actions settings. Repository writers can change these controls.

## Seals

Three full rounds are sealed after the evidence reset. The comparison selects 12 tasks for each
of five agents: Codex, OpenCode, Pi, Wisp and Kmet. Recovery trials are not substituted.
The first Kmet round is not sealed because an untracked file made its run record dirty. A seal proves
that recorded hashes did not change, not fairness or correctness. All selected rounds ran the
linux/amd64 tasks under Rosetta emulation in a Podman VM on an Apple Silicon Mac: a MacBook Pro
with Apple M4 Max (14 cores, 36 GB) and a VM with 12 CPUs and 24 GiB. The page states this. New rounds on that host need `--allow-emulation`.

`results/*-transcripts-seal.json` contains local evidence paths and hashes, not file contents.
Each round is audited and committed separately within the same expansion budget. No evidence
is omitted. The local hook checks evidence coverage and scans supported archives. Missing agent
evidence is valid only for a documented failure before the agent starts. Configuration, trial
log and exception evidence are still required, hashed and scanned. Incomplete audits block commits.

## Publish an update

1. Finish and audit trials. Seal the original results without changing their outcomes.
2. Update the explicit selection and `public/comparison.json` from `from_seals`.
   Use `public_report.collect_metrics(root)` to collect the matching metrics from local evidence.
   Validate and review them before writing `public/report-metrics.json`. This operation checks
   each source result hash and does not change evidence.
3. Export into a new directory, for example:

   ```sh
   PYTHONPATH=src .venv/bin/python -m ctb.public_transcripts export --out artifacts/public-new
   PYTHONPATH=src .venv/bin/python -m ctb.public_transcripts check --out artifacts/public-new
   ```

4. Run tests and the independent credential scan. Inspect the sanitized output locally.
5. Commit the source, seals, reviewed metrics and hash-only public manifests on `main`. Keep evidence local.
6. Put only the exported site and `.github/workflows/pages.yml` on `gh-pages`. Do not merge main
   into that branch or put source evidence there. Check the exact site again before its commit.
7. Push `main`, then `gh-pages`. The workflow checks and deploys the public branch automatically.
8. Watch the deployment and verify the live page and a transcript. A failed check blocks deployment.

The workflow supports a manual run too. It does not automatically select new benchmark data.

## Agent tabs

The agent table has two tabs. **General** is the default. It includes every attempt of each
agent, including failures and extra tasks, and shows attempts, passes, pass rate and totals.
It does not intersect task sets. A total is unavailable if any attempt lacks that
measurement; a partial sum is not a total.

**Normalized** includes only the tasks that every agent passed, with one attempt each and the
same model. A task with two attempts by one agent is excluded, not resolved by the best
result. The tab lists those tasks. If no task was passed by every agent, it says so.
The URL stores `tab=normalized`. Trial-list filters do not change these tabs.

Equal task content does not make different harnesses, packages or limits equivalent. It also
does not prove equal host load or provider conditions.

New trials use the same pinned Python archive and package inventory. Each agent receives
the same note about `ctb-python` and `ctb-pytest`. Task `python3` stays first on PATH so the
harness does not replace task-specific runtimes. The setup checks package versions before
the agent runs and records `toolkit-inventory.json` and `python-resolution.txt` locally.
These records are evidence, not public transcript payloads. Historical runs are not changed.
Every new trial uses one verifier. It installs the same toolkit before the task tests and
runs them with the same `PATH` as the agent commands.

## Asset updates

All public styles, scripts, font files and nested CSS imports use one content-derived URL
revision. The revision changes when any approved UI asset changes. This prevents a cached
script or stylesheet from a previous deployment from controlling newer HTML. Transcript
payloads keep their existing content hashes and are not changed by a layout update.

## Metric coverage

Missing values never become zero. Provider time and tool time are not inferred from total
agent time. Interrupted runs can lack a final usage record. Full trial time includes
environment setup and verification; agent time is separate. A failed attempt can consume
less without being better, so General resource totals must be read with the pass rate.

Derived report totals use complete run records before partial call records. OpenCode session
exports include visible output and reasoning output in the API list-price equivalent. Invalid
exports have no full-run token or cost total. Dirge messages without IDs are distinct calls;
only explicit duplicate IDs are removed. Samizdat run totals include its side calls.
These reader corrections do not change source results or seals. Re-export and audit normalized
transcripts after reader changes, then approve the new hashes on main. Per-call timings that
were never recorded remain unavailable. API-equivalent cost is not a subscription charge.

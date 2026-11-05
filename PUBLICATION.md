# Public comparison and sanitized transcripts

`main` holds source code, result seals, evidence manifests and public transcript hashes.
`gh-pages` holds only the Pages workflow and `site/`: the original report layout, a text viewer, manifests,
and 72 individual `.json.gz` transcripts. Never upload `jobs/`, `runs/`, raw logs, task
artifacts, or the local HTML report. The public export is a different directory.

## Data boundary

`public_report.py` projects a closed set of agents, tasks, errors, outcomes and hashes.
`public/report-metrics.json` holds reviewed numeric measurements, phase times and known tool
labels for the same 72 result hashes. Unknown fields and tool names are refused. Rewards and
start times must match the seals. The Pages checker does not read source logs.
The public pages use `ctb.report.render` and the original report assets. Fonts are local,
with their Open Font License. Trial HTML has metadata only; transcript text stays in gzip.
`public_transcripts.py export` reads the selected local transcripts. It removes current known
credential values and their common encodings, credential-like patterns, opaque credential
fields and local user paths before compression. It does not clip text before the scan.
Each event has a fixed schema with text fields only. This normalized view is not a full raw log.
A trial that failed before its agent started has an explicit status event, not a made-up trace.

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

Four original full rounds are sealed. The comparison selects 12 tasks for each of six agents.
Older rounds and recovery trials are not substituted. A seal proves that recorded hashes did
not change, not fairness or correctness. Vis used newer verifier and health-check fixes;
the public page states this difference.

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

## Matched task comparisons

The selector has two views. **General** includes every attempt for each selected agent,
including failures and extra tasks. It shows attempts, passes, overall pass rate and total
cost and time. It does not intersect task sets. A total is unavailable if any attempt lacks
that measurement; a partial sum is not a total.

**Normalized** includes only tasks that every selected agent completed successfully, with
the same task checksum and model. Duplicate attempts are excluded, not selected by the best
result. It shows mean cost and time on that shared successful set, not pass rates. Each
metric requires measurements for every selected agent on the same tasks. The page lists
those tasks and metric coverage. No shared successes means no normalized comparison.

Setup consistency is a separate warning in both views. Equal task content does not make
different harnesses, packages or limits equivalent. Recorded setup equality also does not
prove equal host load or provider conditions. Trial-list filters do not change these views.

New trials use the same pinned Python archive and package inventory. Each agent receives
the same note about `ctb-python` and `ctb-pytest`. Task `python3` stays first on PATH so the
harness does not replace task-specific runtimes. The setup checks package versions before
the agent runs and records `toolkit-inventory.json` and `python-resolution.txt` locally.
These records are evidence, not public transcript payloads. Historical runs are not changed.

## Agent selection

The comparison selector accepts two or more agents. A URL with `compare=codex,dirge`
keeps the selection when the page reloads. Only the selected comparison is visible.
An empty selection or one agent does not produce a comparison. Different setups show a warning.
Open the metric coverage section to see which shared trials record each measurement.
Provider time and tool time are not inferred from total agent time. The selected Dirge
logs record final usage for completed runs but no per-call usage or event timing.
Interrupted runs can lack a final usage record. Missing values stay missing.

## Asset updates

All public styles, scripts, font files and nested CSS imports use one content-derived URL
revision. The revision changes when any approved UI asset changes. This prevents a cached
script or stylesheet from a previous deployment from controlling newer HTML. Transcript
payloads keep their existing content hashes and are not changed by a layout update.

## Comparison and metric coverage

General is the default view. The URL stores `mode=general` or `mode=normalized` and the
selected agents. Missing values never become zero. Full trial time includes environment
setup and verification; agent time is separate. A failed attempt can consume less without
being better, so General resource totals must be read with the overall pass rate.

Derived report totals use complete run records before partial call records. OpenCode session
exports include visible output and reasoning output in the API list-price equivalent. Invalid
exports have no full-run token or cost total. Dirge messages without IDs are distinct calls;
only explicit duplicate IDs are removed. Samizdat run totals include its side calls.
These reader corrections do not change source results or seals. Re-export and audit normalized
transcripts after reader changes, then approve the new hashes on main. Per-call timings that
were never recorded remain unavailable. API-equivalent cost is not a subscription charge.

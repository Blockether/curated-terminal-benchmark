# Public comparison and sanitized transcripts

`main` holds source code, result seals, evidence manifests and public transcript hashes.
`gh-pages` holds only the Pages workflow and `site/`: a comparison, a text viewer, manifests,
and 72 individual `.json.gz` transcripts. Never upload `jobs/`, `runs/`, raw logs, task
artifacts, or the local HTML report. The public export is a different directory.

## Data boundary

`public_report.py` projects a closed set of agents, tasks, errors, outcomes and hashes.
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
3. Export into a new directory, for example:

   ```sh
   PYTHONPATH=src .venv/bin/python -m ctb.public_transcripts export --out artifacts/public-new
   PYTHONPATH=src .venv/bin/python -m ctb.public_transcripts check --out artifacts/public-new
   ```

4. Run tests and the independent credential scan. Inspect the sanitized output locally.
5. Commit the source, seals and hash-only public manifests on `main`. Keep evidence local.
6. Put only the exported site and `.github/workflows/pages.yml` on `gh-pages`. Do not merge main
   into that branch or put source evidence there. Check the exact site again before its commit.
7. Push `main`, then `gh-pages`. The workflow checks and deploys the public branch automatically.
8. Watch the deployment and verify the live page and a transcript. A failed check blocks deployment.

The workflow supports a manual run too. It does not automatically select new benchmark data.

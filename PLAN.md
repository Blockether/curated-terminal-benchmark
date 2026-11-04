# Benchmark error recovery

## Context
The audit found scoring timeouts, a pre-agent healthcheck failure, and provider transport failures.
Keep original trials unchanged. Do not seal until recovery evidence has been reviewed.

## Phases
1. Fix photonic verification. State: implemented; live verification pending.
   - Rationale: rendering can time out after the verifier writes its reward.
   - Data: two trials have a reward file but no recorded reward.
   - Acceptance criteria: tests preserve pass and fail exit codes without running the renderer.
   - Finding: bundled images ignore local fixes unless the standard verifier is selected explicitly.
2. Regrade saved artifacts. State: queued.
   - Rationale: verification does not require another model call.
   - Data: five verifier timeouts across Codex, OpenCode and Dirge.
   - Acceptance criteria: preserve source hashes and record each new verdict separately.
   - Unknowns: whether MVCC submissions exceed the original time limit again.
3. Recover the pre-agent failure. State: healthcheck fix tested; recovery queued.
   - Rationale: Dirge never started on kv-live-surgery.
   - Data: the environment healthcheck timed out after ten seconds.
   - Acceptance criteria: retry only that trial in a new round after checking environment health.
   - Unknowns: whether the infrastructure failure repeats.
4. Review and deliver. State: pending.
   - Rationale: transport failures and agent timeouts must not be hidden.
   - Data: original trials and recovery trials.
   - Acceptance criteria: run checks, commit and push the fixes; report unresolved errors before sealing.
   - Unknowns: provider failures might require upstream changes, not harness changes.

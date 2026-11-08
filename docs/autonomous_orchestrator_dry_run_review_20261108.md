# FutureEval autonomous orchestrator integration – OFFLINE ONLY (2026-11-08)

## What this PR really delivers

A single, testable program module now composes the previously separate safeguards:

1. Read the authoritative **disabled-by-default** controller state from
   the separate control-store interface. Neither a real state branch nor a
   control token is created in this PR.
2. Before any reservation, require four independently checked,
   **individually fresh (<=5 min)** evidence clocks: GitHub queued/running
   workflows, Metaculus-donated OpenRouter credit balance, Perplexity
   spending authorization, and encrypted publication journal status.
   Missing, stale, future, unknown or contradictory evidence fails closed.
   In a *real deployment*, readers MUST authenticate each source; a Python
   boolean or synthetic fixture is not itself independent evidence.
3. Reuse the PR #14 `scheduler_policy` (15-minute spacing, no overlaps)
   and PR #16 `dispatch_intent_controller` CAS state machine. The
   default `evaluate_offline_tick` only PREVIEWS a potential reservation.
   A separate explicit opt-in is required even to write the control state.
   The optional `reserve_only_after_preflight` performs a durable CAS
   write and leaves `pending` **without dispatching a GitHub workflow**.
4. `persist_emergency_stop` models a narrowly authorized, single-CAS
   kill-switch update to `enabled=false`. It READS BACK state to verify
   that the stop actually persisted; ambiguous writes or racing updates
   do not claim success. A future live controller must ALSO cancel running
   work, and each worker must independently check the switch before paid
   requests and Metaculus POST.
5. `assess_settlement` only reports future manual reconciliation
   eligibility if the same intent is tied to a successful future
   `autonomous_one_worker.yaml` run and the same public post ID appears in
   the worker's final report, confirmed encrypted publication journal
   and Metaculus forecast history. Four independently fresh completion,
   journal, history, and billing checks are mandatory. A green Actions
   status alone is NOT enough. Pending state is **never cleared here**.
   Zero-candidate, failure, unknown/cancelled, or no-POST results currently
   remain blocked until separately reviewed verified no-publication
   reconciliation exists.

## Important limitations

- **There is still no scheduled or runnable automatic publisher.**
  No `--automatic-one` CLI flag exists. The existing publisher still
  requires a human-specified post ID and one explicit approval.
- There is no `autonomous_one_worker.yaml` or
  `autonomous_dispatch.yaml` file. The orchestrator contains no
  HTTP client or GitHub `workflow_dispatch` adapter. The separate
  `metaculus-dispatch-control` branch does not exist yet. Test readers
  and stores are synthetic, with a socket-level network guard.
- The provider/source readers, future worker authentication, durable
  run-ID correlation, real kill-switch distribution, and actual safe
  reconciliation remain to be implemented in separately reviewed code.
- Merely passing this PR's offline tests **does not** verify that
  Perplexity API spending is globally capped. The existing prepaid
  dashboard balance attestation expires in six hours and cannot by
  itself support hands-off operation.
- The user has approved code/design work, not recurring paid calls
  or posting to Metaculus. Never reuse the old publishing cron,
  enable the private read-only monitor to publish, or modify
  `metaculus-publication-state`.

## Synthetic regression scenarios

- Default-disabled controller rejects any tick before contacting
  third-party evidence sources.
- Fresh all-green synthetic readers yield **preview only** (no CAS).
- Missing, stale, future, invalid, or contradictory source readings
  block the tick before a CAS.
- Even a permitted control-state write only reserves a pending intent.
  Repeat, concurrent dispatch and ambiguous store writes fail closed.
- Emergency stop verifies an actual durable OFF state by a subsequent
  read, preserves any pending ambiguous transaction and never claims
  that already-running jobs have been cancelled.
- Future settlement evidence must correlate **intent ID, worker path,
  successful run, public post, journal and actual Metaculus history**.
  Evaluation never mutates the state.

## Future activation gate

The next work is *real* read-only authenticated provider evidence
adapters and a source-authenticated live worker design, without
access to paid models until separate authorization. Later:
- independent default-off operator kill switch,
- a separately permissioned control branch and narrow write token,
- safe workflow dispatch with persisted intent and run ID,
- no-POST reconciliation for empty batches,
- proof that pre-spend Perplexity budgeting meets the operator's limits,
- explicit operator approval of unattended polling and publication.

**NO live APIs called, no workflow or cron activated, no encrypted
publication journal changed, and no production merge in this PR.**

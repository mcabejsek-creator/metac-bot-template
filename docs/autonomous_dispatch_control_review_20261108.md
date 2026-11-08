# Autonomous dispatch control: dormant persistent intent layer, 2026-11-08

## Current state: NOT ENABLED

- Production source has **no `--automatic-one` CLI flag**, scheduled publisher,
  enabled dispatcher, or endpoint that automatically launches paid forecasts.
- This PR adds a *transport-free* state machine and a dormant GitHub Contents
  store adapter, with synthetic tests. Neither is imported by the production
  workflow or invoked by an external scheduler.
- The separate `metaculus-dispatch-control` branch and its
  `dispatch-controller.json` file **do not exist** as part of this PR.
  A missing branch, missing file, missing secret, malformed record, or
  unavailable GitHub API must deny dispatch: no auto-initialization.
- This controller branch is separate from
  `metaculus-publication-state` and must NEVER reset or write to the
  encrypted publication ledger. Existing private 15-minute read-only
  cronjob.org monitor is not changed.

## Persistent state design

One strict JSON object, version 1:

```json
{"version":1,"enabled":false,"approval_epoch":0,"last_intent_at":null,"last_slot":null,"pending":null}
```

- The default state is DISABLED, with no authorization generation.
  This PR has no method to create a live enabled state. A production
  activation UI and operator authorization are still outstanding.
- When a future controller receives a trusted snapshot of provider budgets,
  GitHub active/queued jobs, eligible run status, and explicit approval,
  `propose_reservation` still must pass an independently held persistent
  `enabled` switch and matching `approval_epoch`.
- Every eligible tick is quantized into a UTC 15-minute window. Its ID is
  deterministic SHA-256 of version, activation epoch, and window (not a
  secret or signature); the durable last-slot and last-intent timestamps
  also prevent older/replayed slots and too-frequent dispatches.
- Before any future `workflow_dispatch` POST, `reserve_with_store`
  requires a *confirmed* GitHub Contents expected-blob-SHA update on the
  independent control branch (one attempt; no retry after HTTP ambiguity).
  A 409/422 conflict fails closed, as do network errors and malformed
  responses.
- On success, the controller leaves a `pending` intent. **Pending stays
  blocking, including after a lost dispatch response, GitHub job failure,
  or timeout.** This PR deliberately contains no auto-acknowledge or
  auto-unblock path; durable end-to-end reconciliation must be implemented,
  authorized, and tested before activation.
- `disable_controller` models the emergency kill switch while preserving
  pending intent. No live switch is installed. A future adapter must
  persist the disabled state and independently cancel any queued/running
  workflow; a local flag alone cannot stop already-started paid work.

## Remaining engineering required BEFORE publishing automation

1. Approve and create the separate control branch with default-off state,
   protection rules, narrowly scoped write token, and reliable CAS testing.
2. Build a controller application that checks all facts from authoritative
   sources. In particular, it must NOT manufacture true flags for the
   `scheduler_policy` when provider billing or prior forecast outcomes
   are unknown.
3. Implement operator-signed kill switch and reconciliation for pending
   intents. Verify publication report, encrypted journal, and Metaculus
   forecast history before clearing a pending intent; never trust a green
   GitHub check alone. No replay after ambiguous acknowledgement.
4. Add a separately reviewed default-deny `workflow_dispatch` worker
   with server-side activation checks immediately before **each**
   billable model operation and Metaculus POST, reusing single-post
   selection and the existing GitHub publication lock.
5. Perplexity has no authenticated public prepaid balance endpoint
   accessible through this API key. The existing six-hour manual
   balance attestation cannot support permanent hands-off operation.
   Obtain provider-enforced spending control / accept explicitly
   bounded variable-cost exposure and design verified reconciliation.
6. Explicitly authorize the external timer, actual GitHub writes,
   billed agent calls, live Metaculus GET/POST, and activation
   separately. The old publishing cron and read-only monitor remain
   untouched until then.

## GitHub API design constraints

- A GitHub Actions `workflow_dispatch` request can request a workflow
  run ID via `return_run_details` (supported since February 2026),
  but an HTTP timeout remains ambiguous.
  https://github.blog/changelog/2026-02-19-workflow-dispatch-api-now-returns-run-ids/
- GitHub's Contents API requires the existing blob SHA when updating an
  existing file and can signal conflicts. This must be interpreted as
  optimistic concurrency, not as a substitute for proof of publication:
  https://docs.github.com/en/rest/repos/contents
- Never place API keys, personal billing details, private account
  sessions, journal encryption keys, or live control tokens in source
  or plaintext state. Content repository may be publicly visible.

**No paid call, no Metaculus forecast, no production merge, no authenticated
GitHub GET/PUT, no new cron task and no secret rotation performed by this PR.**

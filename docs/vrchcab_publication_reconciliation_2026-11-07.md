# Vrchcab publication reconciliation: evidence snapshot (2026-11-07)

**Scope:** Read-only investigation of GitHub Actions metadata and job logs, with no forecasts submitted, no historical writes retried, and no credentials exposed. This is NOT an exhaustive publication inventory or authorization to initialize a new journal.

## Verified infrastructure state

- Repository: `mcabejsek-creator/metac-bot-template`.
- Production `main` HEAD at last check: `f0da8ce7b3351f48115fdeb8394fcfb2e88c1341`. No production merge during this investigation.
- Rollout PR #1 stays open/unmerged; diagnostic PR #2 stays a separate draft.
- Exactly one workflow run remained queued (run `34463156990`, created 2026-09-10 09:53:49 UTC, old commit `e521169cc6bc9cdba5b9e83efeafd020e430f76c`). Its GitHub jobs endpoint returned zero jobs, and no artifacts were available. This does NOT prove it can never start if GitHub repairs the queue.
- At the check, GitHub returned zero runs in `in_progress`, `waiting`, `pending` or `requested` states. Other than the old run there were zero `queued` runs.

## Confirmed historical forecast posts

Two distinct October 5 workflow logs explicitly contain the SDK message `Posted prediction on question ...`. No question IDs or forecast values are reproduced here:

| GitHub run | Evidence timestamp (UTC) | Recorded estimated model cost |
|---|---|---|
| [37306672801](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/37306672801) | 2026-10-05 12:03:46 | USD 0.12956 |
| [37373289415](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/37373289415) | 2026-10-05 21:08:08 | USD 0.13630 |

These are evidence of two acknowledged publication events in **sampled** logs, not a full list of published forecasts.

Some neighboring success logs loaded open MiniBench questions but returned `No new questions to forecast on this run`; this is compatible with the SDK skipping questions already forecast.

## Overlapping/cancelled runs that remain ambiguous

The following workflow runs are marked completed with failure/cancelled jobs, but their GitHub job logs currently return `BlobNotFound`, and no run artifacts are available:

- [37366002616](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/37366002616): 2026-10-05 19:50:41–20:05:44 UTC.
- [37367114796](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/37367114796): 2026-10-05 20:01:06–20:16:10 UTC.
- [37372241017](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/37372241017): 2026-10-05 20:50:40–21:05:45 UTC.

The 20:50 run overlapped the 21:01 run, which later reported one confirmed publication. This is **an overlap and an uncertainty**, not proof of duplicate publication. A cancelled run can, in principle, have made a POST before cancellation; absence of logs cannot reconcile it.

## API diagnostics, limitations

- On 2026-11-07 11:39 UTC a previously user-approved, one-time authenticated Metaculus check returned **zero open posts on first page** in Fall FutureEval 2026 (`33121`) and MiniBench (`minibench`). Two GET requests were sent, no POSTs. This describes only that point in time.
- Three earlier history-audit attempts failed. The third failed with HTTP 429, and the later pinned audit script was not subsequently invoked.
- An offline method-compatibility check on installed `forecasting-tools 0.2.92` found that this release does not expose the upstream `_get_acting_user_forecasts` helper being compared. Therefore any suspected GET-vs-POST mismatch between the historical-audit script and a newer upstream SDK remains **unproven for the installed release**. Do not infer the historical audit is correct or incorrect from this comparison.
- GitHub logs are incomplete; this report does NOT establish a complete history for Vrchcab (Metaculus user 307558), nor reconcile uncertain POST acknowledgments.

## Required before first new publication

1. Obtain an authoritative resolution for old queued run `34463156990`, ideally GitHub Support clearing it; or otherwise establish a technically enforceable barrier to **every** old publisher execution.
2. Validate a bounded, account-specific historical forecast inventory using a Metaculus-compatible read method, within API limits and only with explicit authorization for any further real API calls. Investigate known overlapping/cancelled windows separately.
3. Reconcile uncertain historical writes and prove new journal initialization will not incorrectly claim a blank or comprehensive history.
4. Verify all production publication entry points share the new lock and journal; check latest main/PR integration offline.
5. Obtain user's explicit consent before merge, journal initialization, any live forecast or scheduling.

**Safety:** This document is evidence only. No production merge, initialization, scheduler restart or live write is authorized.

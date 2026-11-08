# Safe single-question automation: review gate (2026-11-08)

## What this draft changes

- The new deterministic **offline selector** picks at most one atomic unforecasted question after **both** fully bounded inventories (Fall FutureEval 33121 and MiniBench) have succeeded.
- Prefer the candidate with the earliest closing time, with at least 90 minutes remaining (60-minute per-question budget plus 30-minute buffer). On ties, prefer Fall FutureEval then the smallest public post ID.
- Exclude conditional, multi-question/group posts, duplicates, known prior token forecasts, invalid ID/history, unsupported kinds, missing timing, and malformed publish formats.
- If there is no eligible candidate, return empty batches; do not initialize the publication journal or call any paid model.
- The selector is wired into `forecast_for_mode(... automatic_one=True)` for **offline tests only**. That dormant code path now requires exactly one OpenRouter current-key metadata GET for the explicitly fingerprinted donor key after choosing a candidate and before any model/journal work; missing funding/identity/reserve fails closed. There is **no CLI flag**, no scheduler, no new workflow_dispatch, no external cron changes and no new way to run paid autonomous forecasts from GitHub Actions.
- The preexisting one-public-post manual approval is unaffected. The already-confirmed 2026-11-08 forecast for public post 46347 must never be repeated.

## Before ANY paid automation can be activated

1. **Donated GPT credits are CONFIRMED, not personal spending.** On 2026-09-10 the Metaculus FutureEval organizer provided a $100 OpenRouter key for FutureEval + MiniBench (verified privately; never copy its secret value into repository). The Metaculus resource page explicitly says its keys are connected to Metaculus's own OpenRouter account, not a personal dashboard. A GET-only key metadata check on 2026-11-08 confirmed a $100 cap, ~ $34.29 remaining, ~ $65.71 in BYOK usage, with BYOK included in the limit. Do not tell the operator to create or pay for a personal OpenRouter key. Future unattended runs must stop early if donated credits are exhausted or if the identity of the approved donated key cannot be verified. The new dormant guard leaves a $5 reserve; this is a stop threshold, **not** a per-run spending cap.
   **Perplexity research is separately billed to the operator's API credit wallet**, and was intentionally added for research quality. As of the last operator screenshot, its remaining balance was $14.09 and auto top-up was not enabled. OpenRouter grant does not cover it. Do not silently replace Perplexity or disable valuable research. Before auto publishing, bound research calls, check wallet billing and preserve a manual budget veto.
2. Verify the tournament participation rules, visibility of previously forecasted questions, account-specific history and absence of unresolved legacy publications. The authenticated inventory snapshot alone does not prove tournament eligibility or exhaustive history.
3. The existing durable GitHub publication journal, original-scope key, global publication concurrency and failure/uncertain POST no-retry behavior must remain mandatory. Never reset it or substitute per-run local state.
4. The dormant `scheduler_policy.py` checks a planned 15-minute cadence, fresh GitHub status, zero active/queued runs, explicit approval and both budget guards. It does not dispatch anything. The scenario tests prove serial single-question runs cannot guarantee every question during a burst; see `docs/futureeval_scheduler_activation_design_20261108.md`. Approve a **separate** tightly gated scheduled/externally dispatched workflow with `default-deny`, an explicit operator-approved activation setting, run_attempt==1, one selected post per run, and no overlap. Do not turn the existing private 15-minute read-only monitor into a publisher or reenable old publishing cron by accident.
5. Prove with offline tests that (a) zero candidates incurs zero paid calls/POST, (b) one candidate returns just one atomic question, (c) old/history/uncertain candidates are rejected, and (d) all production publishers remain locked until explicit activation.
6. After activation, monitor actual provider invoices and GitHub journal checkpoints independently. A green workflow log is not proof of successful paid publication or no charge.

## Current authorization boundary

Draft-only/offline code review and CI are authorized. **No merge, real authenticated GET, model call, POST, new automatic trigger, cron-job.org change or change to the publication-state branch is authorized here.**

The first real forecast was explicitly authorized separately and completed under GitHub run 42476608583. Future fully automatic publishing needs a separate, explicit operator approval.

# Safe single-question automation: review gate (2026-11-08)

## What this draft changes

- The new deterministic **offline selector** picks at most one atomic unforecasted question after **both** fully bounded inventories (Fall FutureEval 33121 and MiniBench) have succeeded.
- Prefer the candidate with the earliest closing time, with at least 2 hours remaining for model work and publication. On ties, prefer Fall FutureEval then the smallest public post ID.
- Exclude conditional, multi-question/group posts, duplicates, known prior token forecasts, invalid ID/history, unsupported kinds, missing timing, and malformed publish formats.
- If there is no eligible candidate, return empty batches; do not initialize the publication journal or call any paid model.
- The selector is wired into `forecast_for_mode(... automatic_one=True)` for **offline tests only**. There is **no CLI flag**, no scheduler, no new workflow_dispatch, no external cron changes and no new way to run paid autonomous forecasts from GitHub Actions.
- The preexisting one-public-post manual approval is unaffected. The already-confirmed 2026-11-08 forecast for public post 46347 must never be repeated.

## Before ANY paid automation can be activated

1. **Independent provider cost enforcement required.** The live GitHub run log reported estimated `$0.00000`, and explicitly warns that not all models/search providers are metered by LiteLLM. This is **not** a real spend ceiling. Require a verified provider-side hard wallet/quota, bounded per-run model/search calls and a firm owner-selected daily and total budget before enabling a scheduler. No claim of a dollar cap is made by this PR.
2. Verify the tournament participation rules, visibility of previously forecasted questions, account-specific history and absence of unresolved legacy publications. The authenticated inventory snapshot alone does not prove tournament eligibility or exhaustive history.
3. The existing durable GitHub publication journal, original-scope key, global publication concurrency and failure/uncertain POST no-retry behavior must remain mandatory. Never reset it or substitute per-run local state.
4. Approve a **separate** tightly gated scheduled/externally dispatched workflow with `default-deny`, an explicit operator-approved activation setting, run_attempt==1, one selected post per run, and no overlap. Do not turn the existing private 15-minute read-only monitor into a publisher or reenable old publishing cron by accident.
5. Prove with offline tests that (a) zero candidates incurs zero paid calls/POST, (b) one candidate returns just one atomic question, (c) old/history/uncertain candidates are rejected, and (d) all production publishers remain locked until explicit activation.
6. After activation, monitor actual provider invoices and GitHub journal checkpoints independently. A green workflow log is not proof of successful paid publication or no charge.

## Current authorization boundary

Draft-only/offline code review and CI are authorized. **No merge, real authenticated GET, model call, POST, new automatic trigger, cron-job.org change or change to the publication-state branch is authorized here.**

The first real forecast was explicitly authorized separately and completed under GitHub run 42476608583. Future fully automatic publishing needs a separate, explicit operator approval.

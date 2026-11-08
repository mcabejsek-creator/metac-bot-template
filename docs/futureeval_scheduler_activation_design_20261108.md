# Fall FutureEval autonomous scheduling: offline design (NOT activated)

Date: 2026-11-08. This document describes a future design, not approval to
launch paid forecasts or to modify existing cron-job.org or private monitors.

## Verified external constraints

- Fall FutureEval runs Sep 28, 2026 to Jan 6, 2027, tournament ID `33121`.
  MiniBench uses the tournament alias `minibench`.
- Metaculus organizer announced three-hour question windows in June 2026 as
  mitigation for unreliable GitHub Actions timing. This is **not a guarantee**
  that every question has a three-hour window. Intermittent queues, shorter
  windows, and bursts may still occur.
- Existing separate private read-only monitor fires every 15 minutes via
  cron-job.org; no existing publisher cron is authorized to resume.
- GitHub Support ticket #4966035 about old stuck run #34463156990 is still a
  separate support issue. Do not mix it with a new publisher activation.

Official sources:
https://www.metaculus.com/notebooks/38928/bot-tournament-resources-page/
https://www.metaculus.com/tournament/fall-futureeval-2026/

## Selection and time budget

- Candidate admission margin: **90 minutes** remaining at selection time.
  The runtime grants at most 60 minutes for one atomic question; preserve at
  least ~30 minutes for a late write and uncertainty, rather than requiring
  the earlier overly conservative two hours. This does **not** guarantee
  successful completion: GitHub delays, model timeouts, and server closures
  can still defeat a forecast, and publication checks remain authoritative.
- Select at most one individual unforecasted, supported atomic post from two
  **complete** bounded tournament inventories per live job.
- Prefer the earliest closing time. Reject group/conditional posts and any
  missing or contradictory own-forecast history. Recheck the known close
  snapshot immediately before a new forecast POST.
- Keep the existing global GitHub publication concurrency lock and the
  durable encrypted publication journal. Never retry a possibly-sent POST
  without independent reconciliation.

## Prospective scheduler (not installed)

- A **new, separate** publisher controller may poll every 15 minutes from a
  dedicated external trigger. Do **not** enable the old `FutureEval GitHub
  trigger` cron-job or convert the private `vrchcab-readonly-watch` into
  a publishing job. Do not rely solely on GitHub's intermittent `schedule`.
- Before *considering* dispatch, evaluate the pure `scheduler_policy` against
  a fresh GitHub status (<5 min old), zero active/queued publisher jobs, a
  verified last-dispatch timestamp at least 15 min old, an explicit persistent
  operator activation switch, a verified sponsored OpenRouter credit guard,
  an independently verified **Perplexity** spending control, and a certain
  prior publication outcome. All unknown states deny a dispatch.
- The controller must persist dispatch intent **before** any workflow dispatch
  and must not retry on ambiguous HTTP acknowledgment. GitHub concurrency
  alone does not make the external dispatcher idempotent.
- The future job performs both authenticated tournament scans again, then
  chooses one question and rechecks the $100 donated OpenRouter key using the
  operator-verified masked suffix, with at least $5 reserve. Perplexity Agent
  is billed separately; no provider-side Perplexity hard cap is verified yet.
- A production CLI and a default-deny new workflow **do not exist** in this
  draft. Do not expose `automatic_one=True` in the current publishing CLI.
  A future PR must implement the persistent switch and billing checks first.

## Simultaneous questions: throughput limitation

At a 60-minute per-question runtime, 15-minute polling and a hard single-job
concurrency restriction, it is **impossible** to guarantee catching three
questions that all close in three hours if queueing consumes most of the
window. A synthetic test shows a first forecast at t=0 and a second at
t=75 minutes, but the third cannot safely be admitted at t=150 with only
30 minutes left. Never claim 100% coverage.

Before enabling automatic publication, measure *real* per-question runtime,
provider costs, start-up delay and batch-size frequencies. If bursts are
common, reconsider whether the one-post-per-run policy is worthwhile or
prepare a separately reviewed bounded multi-post scheduler with a
corresponding hard cost cap and explicit user approval.

## Activation conditions

1. Operator separately authorizes a concrete automatic scheduler and spend
   boundaries. Existing approval to create draft PRs is not authorization.
2. The Perplexity research API spending budget is enforced or fail-closed:
   no unknown-balance automated paid API calls.
3. All tests including no-network/missing-secrets checks are green.
4. First automatic run is monitored for forecast POST, private comment,
   journal checkpoint and real billing; an Actions green check alone is
   insufficient.
5. There is an immediate and tested kill switch, independent of the private
   read-only watcher and historical GitHub Support case.

**Current PR is entirely offline: no new schedule, no new authenticated GET,
no paid models, no Metaculus POST, no merge and no journal write.**

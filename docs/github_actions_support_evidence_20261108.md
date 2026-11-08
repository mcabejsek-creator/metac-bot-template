# GitHub Support #4966035: evidence update (8 November 2026 UTC)

Repository: `mcabejsek-creator/metac-bot-template`.

This is a technical read-only snapshot, not evidence that GitHub Support
has closed the ticket. It deliberately does NOT alter GitHub Actions settings,
scheduled triggers, existing workflows, publication journal, or any API key.

## 1. Long-stuck publishing run — STILL OPEN

- Run URL:
  https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/34463156990
- ID: `34463156990`, workflow: `run_bot_on_tournament.yaml`,
  workflow_id `347996133`.
- GitHub REST GET reported `status: queued`, `conclusion: null`,
  `run_attempt: 1`, `event: workflow_dispatch`, created and last
  updated `2026-09-10T09:53:49Z`.
- GitHub workflow jobs endpoint returned an empty jobs array.
- Global repository status-filtered queue API returned exactly **one**
  queued run, namely this one, as checked Nov 8. The fact that later
  PR and supervised runs succeed means the queue is not a total account
  outage. It must NOT be presumed safe to ignore in an autonomous
  publisher; a stale run may remain an ambiguous publication risk.
- Only GitHub Support can reliably clear or explain the old run. Existing
  automation policy refuses dispatch while any publishing run is queued.

## 2. Built-in GitHub `schedule` — NOT COMPLETELY DEAD, BUT UNRELIABLE

- Workflow file:
  `.github/workflows/watch_open_metaculus_questions_readonly.yaml`
- Active on `main`, configured `7,22,37,52 * * * *` UTC (four
  theoretical starts per hour).
- Contrary to the earlier support update of Nov 7 17:03 UTC, **scheduled**
  workflow runs now EXIST. Four successful examples, each with BOTH
  offline safety and live read-only monitor jobs succeeded:

  - 2026-11-07T19:24:18Z run `42406043187`
  - 2026-11-07T22:57:41Z run `42423014753`
  - 2026-11-08T02:22:30Z run `42438268494`
  - 2026-11-08T09:26:17Z run `42467179735`

  These are **not** at a reliable 15-minute cadence; intervals are
  approximately 3h33m, 3h25m, and 7h04m.
- The latest monitor on 2026-11-08T09:26Z returned one eligible-looking
  first-page *candidate* in Fall FutureEval and zero on the first
  MiniBench page. Both project inventories declared additional pages,
  so this was **not** global coverage, and a candidate is NOT evidence
  of current eligibility or lack of Vrchcab's prior forecast.
- GitHub's official docs explicitly say `schedule` may run late or
  be dropped due to load, even when manual and PR workflows work:
  https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule

## 3. Safe next steps

- **KEEP #4966035 OPEN** until Support confirms whether it can clean the
  old queued run and provides guidance about skipped/delayed scheduler
  triggers. It is no longer accurate to claim schedule NEVER triggers.
- Do not edit the old workflow or cron just to nudge it while Support is
  investigating, and do not send new live publishing dispatches to test
  scheduling.
- Develop and test a future **external** publisher dispatch pathway
  (separately from the existing private read-only 15-min monitor), using
  the dormant CAS intent journal, activation kill switch, provider budget
  proof, and server-side publication validation. Do not enable it without
  a separate explicit operator approval.
- `github_publisher_queue_readonly.py` introduced alongside this
  evidence uses up to FIVE strictly allowlisted GitHub GETs, querying
  `in_progress`, `queued`, `requested`, `waiting`, and `pending`
  without pagination truncation or retries. Any unknown run status,
  race or API failure denies a "no pending publisher" claim. It intentionally
  includes run #34463156990 in queue counts; it cannot cancel it.
- The reader is deliberately **dormant** and not wired into any live
  workflow, so creating/merging this PR does not cause API traffic.

**No GitHub Support request was sent, no scheduled workflows changed, no
control branch created, and no forecast or paid LLM request made by this PR.**

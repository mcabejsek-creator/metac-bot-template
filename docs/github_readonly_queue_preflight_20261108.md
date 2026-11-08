# FutureEval GitHub Actions preflight: genuine read-only queue check

Date: 2026-11-08. **This PR does not activate any automatic publisher.**

## Fixed a concrete safety gap after PR #18

All three currently existing GitHub workflows share the same encrypted
Metaculus publication journal and publication concurrency lock:

- `.github/workflows/run_bot_on_tournament.yaml` (historic ID 347996133)
- `.github/workflows/run_bot_on_metaculus_cup.yaml`
- `.github/workflows/test_bot.yaml`

The planned future `.github/workflows/autonomous_one_worker.yaml`, if
separately approved and created, would also publish and must be counted.
The previous queue reader identified only the tournament workflow ID;
that was insufficient to guarantee no overlapping publisher.

`github_publisher_queue_readonly.py` now counts any queued/running run
whose path matches one of the four publishing workflows OR whose ID
matches the original historic workflow ID. Every run in every relevant
state must have a valid workflow path to avoid false clearance.
Unknown/missing identity, contradictory status, incomplete/paginated
response and any request error all fail closed.

## Actual reader wired into the dormant orchestrator

`github_queue_preflight.py` wraps the authenticated GET-only GitHub
queue reader and produces ONLY checked-at, queued, and active run
counts. All independent Perplexity, OpenRouter, journal, operator, and
approval flags remain **FALSE** / missing. Consequently, even a
successfully empty GitHub queue does NOT authorize dispatch: separate
trusted provider readers remain outstanding.

## Optional, individually confirmed diagnostic (no automatic trigger)

`.github/workflows/manual_github_queue_diagnostic.yaml` exists as
a new **workflow_dispatch-only** diagnostic with default `DO_NOT_RUN`
and an exact one-time choice `YES_READ_ONLY_GITHUB_QUEUE`.
The workflow is NOT executed when this PR is opened or merged. It does
not have a cron, push, or pull_request trigger. Re-running an approved
workflow cannot authorize another diagnostic (`run_attempt == 1`).
It uses the ephemeral GitHub Actions `github.token` with only
`actions: read` and `contents: read` permissions, never stored
personal or model tokens. It requires **explicit human authorization
each time** and performs at most FIVE fixed allowlisted status GETs
before printing a safe, public status report.

No live diagnostic is executed in this PR. Tests use fake responses and
the existing zero-network CI guard.

## GitHub Support #4966035 remains outstanding

As checked 2026-11-08, run #34463156990 is still `queued` with
zero jobs, created and last updated 2026-09-10T09:53:49Z. Keep it in
the queue count; do NOT silently exempt it. GitHub Support was sent a
follow-up with evidence of intermittent scheduled runs (Nov 7–8).
The separate public read-only monitor's built-in schedule has triggered
only intermittently, with multi-hour gaps despite its nominal 15-minute
cron. GitHub documents that scheduled Actions may be delayed or dropped.

Until this run is resolved or separately verified safe through an
approved reconciliation procedure, future autonomous execution stays
blocked. The existing private read-only 15-minute cron is unrelated.

## Remaining blockers

1. GitHub Support resolution or separately authorized authoritative
   reconciliation of the historic queued run.
2. Provider-authenticated Perplexity spending/balance evidence; the
   present manual screenshot attestation cannot support permanent
   unattended runs.
3. Authenticate and integrate actual donated OpenRouter balance, encrypted
   publication journal and Metaculus history for a future automatic worker.
4. Independently controlled persistent activation and kill switch,
   default-off worker, before-send intent CAS and safe no-POST outcome
   reconciliation. User approval required for all live automation.

**No existing cron modified. No active publisher, paid API, Metaculus
POST, control branch, or encryption journal changed.**

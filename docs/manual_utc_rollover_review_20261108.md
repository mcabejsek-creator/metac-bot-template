# One-day UTC quota rollover: OFFLINE REVIEW ONLY (2026-11-08)

## Why this is necessary
PRs #27–#29 implemented a daily policy gate, persistent CAS reservation of
attempt counts and conservative USD allowances, and sanitized Agent-response
cost observations. None of these can certify the full Perplexity billing
ledger. A lost HTTP response may still incur a charge; a screenshot of a
rounded 30-day dashboard total does not prove the exact costs of a day or
separate charges from another client.

**Never reset the daily counter merely because midnight UTC passed.**
An unaccounted request or uncertain Metaculus forecast must remain blocked.

## Prepared, but inert
`daily_rollover_review.preview_disabled_daily_rollover` accepts only
a DISABLED and nonpending validated v2 control state, an explicitly matched
approval epoch and exact synthetic/manual-review declaration,
an exact one-day UTC boundary, a fresh (at most five-minute-old) independently
asserted source snapshot, and strict accounting.

- Require an independently verified provider statement covering all
  relevant clients/keys (NOT simply `usage.cost.total_cost`).
- Require every Agent attempt and every publication outcome reconciled,
  no unknown Agent or publication attempts, zero active/queued publishers,
  and an already immutable-archived prior audit.
- Require the prior durable quota's date, attempt count and reserved amount
  to match the independently confirmed previous-day report.
- Deny prior-day verified charges above the approved previous-day cap;
  reject malformed monetary values, missing timestamps and stale evidence.
- Require an operator's distinct single-day review choice.
- The **only** permitted result is an in-memory PROPOSAL of a still DISABLED
  control state, with day advanced by exactly one and the new day starting
  from zero attempts / zero reserved dollars. It does NOT write the state
  or authorize the state to be enabled.

No code authenticates the evidence or the operator; these fields are
ASSERTIONS. A trusted future provider-statement reader and external
authorization gate must establish them. An unverified local process must
never set these flags. For an API key without an authoritative billing
endpoint, the operator must obtain independently auditable itemized
provider billing or the review is permanently blocked. Merely trusting the
GitHub model output or dashboard screenshot is prohibited.

## Mandatory gates before any future state write
1. GitHub Support must resolve, or safely reconcile, the September run
   `#34463156990`. It is still queued as of this review. The reviewer
   explicitly requires zero active or queued publishing workflows; this
   known stuck run CANNOT be excluded.
2. Implement immutable, independent archive/audit proof for the prior UTC
   day, including unpriced attempts and provider statement cross-check.
   Never upload invoices, API keys, cookies or account information to a
   public repository. The current `metaculus-dispatch-control` branch
   does not exist and is NOT created by this PR.
3. Obtain a separate explicit authorization for exactly one manual CAS on
   the independent control branch after a fresh real re-check, with no
   retries on conflict or ambiguous response. Preserving the previous
   audit is mandatory; the in-memory proposal is not an actual reset.
4. Leave the switch OFF after any future authorized CAS. Activation, paid
   agents, the schedule, and Metaculus publication each require their own
   later approvals and independent spending controls.

## Checks
`tests/test_daily_rollover_review.py` tests synthetic good evidence,
source/approval gating, cost mismatches, unknown attempts, old queued
runs, malformed monetary and UTC dates, stale source clocks, pending
intents, and the still-disabled result. CI has credentials blank and
a socket guard; this PR does not contact Metaculus, Perplexity or any
payment provider.

**Merge of this draft does NOT authorize a real daily reset.**

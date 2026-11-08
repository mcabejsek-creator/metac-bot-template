# Perplexity Agent research funding: automatic-mode guard (NOT activated)

Date: 2026-11-08. This file is a proposal, not an automatic publisher.
It MUST NOT be presented as proof of a hard dollar spending limit.

## Verified situation
- The donated Metaculus OpenRouter key (`...9fc`) has a separate $100 grant
  for FutureEval/MiniBench, currently protected by a $5 reserve guard.
- Perplexity Agent research is a *separate personal prepaid API wallet*.
  User dashboard previously showed $14.09 credit remaining and auto top-up OFF.
  This is a manual snapshot, not a live/authoritative API reading.
- Current bot does two serial Perplexity Agent phases per question:
  initial research and a fact-check. Existing manual/supervised workflow
  retries transient failures up to 3 times per phase (potentially six
  billable HTTP requests), because a lost response can be billed.
- Public Perplexity API-key endpoints DO NOT provide an authoritative
  prepaid balance/credit lookup as of this review. Do not scrape account
  session cookies into GitHub secrets, rely on undocumented private API
  endpoints, or misinterpret an API HTTP 200 as wallet-balance verification.
- Some Perplexity products return `usage.cost.total_cost` per response;
  the exact **Agent API** cost reporting must be confirmed independently
  before treating the ledger as an audited invoice.

References:
https://community.perplexity.ai/t/cost-information-now-included-in-api-response-json/833
https://community.perplexity.ai/t/introducing-the-agent-api-a-managed-runtime-for-agentic-workflows/4733
https://www.perplexity.ai/hub/legal/perplexity-api-terms-of-service

## Dormant protection implemented in this PR
- Only the already-dormant `forecast_for_mode(... automatic_one=True)`
  constructs an `PerplexityAutoRunBudget`. No CLI flag, new publisher
  workflow, new cron schedule, external monitor mutation or activation.
- Automatic attempt count is capped at **TWO total Perplexity Agent POSTs
  per run** (initial research, fact-check), with **NO automatic retry** on
  HTTP 429/5xx, timeout, malformed output or uncertain/billed response.
  The attempted POST is counted BEFORE transmission. On any unknown
  provider response the guard permanently stops additional requests.
- Require explicit operator attestation `APPROVE_TWO_AGENT_REQUESTS`, a
  recent (<=6-hour) UTC dashboard balance snapshot, an operator-set
  per-run observed-cost threshold, and operator affirmation that
  auto-recharge remains OFF. After run, discard guard state.
- Require a minimum $2 dashboard reserve after the proposed run cap.
  Reject missing, invalid, stale, unverified or insufficient snapshots.
- If the Agent API provides `usage.cost.total_cost`, accumulate the price
  from successful responses. Stop research if the cap is reached/exceeded.
  Missing/invalid pricing stops further calls rather than assuming $0.
- These controls DO NOT prevent a *single* completed POST from exceeding
  its dollar cap. They do NOT measure other API clients, shared credits,
  provider retries inside Perplexity or changes in wallet balance.
- Manual/supervised first-live-post mode remains unchanged: the
  `_automatic_perplexity_budget` property is only set in the dormant
  automatic path and cleared in `finally`.
- No plaintext provider API key, billing dashboard cookie, Metaculus
  publication journal key or payment details in source or logs.

## Activation blockers before unattended runs
1. Verify *in real billing* that the Agent API consistently provides the
   expected response cost and that prepaid balance exhaustion genuinely
   denies spending without auto-recharge; don't guess from a green run.
2. Identify a provider-enforced budget/capped account, or approve the
   limited exposure of two variable-cost Agent POSTs per run. A local
   post-response cost checker cannot be a strict up-front hard cap.
3. A six-hour manual balance attestation is **not suited for permanent
   unattended execution**. Design an independently verified and secure
   budget reconciliation/refresh workflow before final activation.
4. Build persistent dispatch-intent journal and an independently
   disableable scheduler/controller, reusing PR #14's pure
   `scheduler_policy` and the existing frozen encrypted publication
   journal. No modification to the private read-only cronjob.
5. Confirm competition timing and eligibility; grouped questions may
   exceed serial one-question throughput even at 15-minute polling.
6. Obtain a separate approval for the exact automatic production
   workflow, billing limits, external schedule and kill switch.

**This PR and its offline CI are safe to review/merge but DO NOT authorize
any paid Perplexity request, Metaculus GET/POST, automatic publication or
journal write.**

# Supervised one-post financial safety — DRAFT, not activated (2026-11-08)

## Why this change is necessary
The successfully verified first bot forecast (public post 46347, Actions run
42476608583) established that a single manual tournament publication works.
Its SDK log displayed **$0 estimated** but explicitly excluded untracked
model/tool costs. It was NOT an independent Perplexity Agent bill.

The latest approved GET-only tournament scan (run 42495481455) found public
binary post **46335**, unforecasted at that snapshot, closing at
2026-11-08 18:00 UTC (19:00 Prague time). The snapshot is NOT an
irrevocable publication authorization; question/own-history must be
rechecked immediately before attempting a new forecast.

Manual `first_live_post_id` previously used the old research retry policy,
up to THREE attempts per Agent phase. Two requested research reports with
both original research and fact checking can therefore create multiple
unpredictable billed requests when responses are lost.

## This DRAFT's concrete changes
- For **one explicitly approved manual public post ID**, AFTER both bounded
  authorized tournament inventories and eligibility selection, make ONE
  authenticated read-only metadata GET to verify the Metaculus-donated
  OpenRouter key (three-character authorized suffix, BYOK, minimum $5
  reserve). Never fall back to a personal OpenRouter account/key.
- The supervised one-post mode shares the existing guarded Perplexity
  transport (attempt debited before transmission, no retry on HTTP 429,
  timeout, uncertain outcome or missing cost). Its maximum is now
  **FOUR Agent API POST attempts across the whole question**: two research
  reports x original research/fact-check. The future autonomous mode
  **still defaults to TWO** attempts, no policy relaxation for automation.
- Require `research_approval=APPROVE_FOUR_AGENT_REQUESTS`, manual
  `perplexity_auto_recharge_off=YES`, and a **current** Perplexity API
  prepaid-wallet balance, verification time in ISO UTC and approved
  observed-cost threshold. Reject all missing, malformed, too-old (>6h)
  or insufficient values. Leave minimum $2 reported reserve after the
  user-approved per-run threshold.
- Keep the human's own approved `publication_approval=APPROVE_ONE_LIVE_PUBLICATION_RUN`
  and exact public `first_live_post_id`, and forbid `run_attempt>1`.
- After each successful Agent reply, inspect `usage.cost.total_cost` and
  stop if absent/invalid or already at/over approved threshold. This is a
  **post-response** guard; it CANNOT guarantee that one already-transmitted
  request will cost less than the threshold. Provider-side bill/invoice is
  authoritative, and missed response may still have been billed.
- No requests during code import/CI/PR merge. All publish workflows still
  manual only, no restored cron or automatic publishing.

## Important: public repository, private financial details
The repository is PUBLIC. GitHub workflow inputs and run logs are not a
safe place for balance information. Therefore:
- Keep the **balance** in Actions repository secret
  `VRCHCAB_PERPLEXITY_VERIFIED_BALANCE_USD` (USD decimal, e.g. `14.09`).
- Keep the **UTC time of this verification** in repository secret
  `VRCHCAB_PERPLEXITY_VERIFIED_AT_UTC` (e.g. `2026-11-08T16:00:00Z`,
  generated at the moment the operator checks the wallet).
- Keep the **operator-approved per-run threshold** in repository secret
  `VRCHCAB_PERPLEXITY_MAX_ONE_RUN_USD` (USD decimal, e.g. `1.00`).

Do not paste provider API keys or dashboard session cookies in chat, source,
workflow inputs or Actions logs. No other account can silently substitute
for the personal Perplexity API wallet.

All three secrets must be added manually in GitHub Settings → Secrets and
variables → Actions, just before the eventual separately-approved live run.
Missing secrets result in failure before any paid model or journal operations.
Only the operator's real fresh dashboard evidence may be entered; do not
reuse historical screenshots as if fresh.

## Hard remaining limits and risk
The full model forecast may use additional donated OpenRouter GPT-4o credits;
the donor key can have its own per-token bill even if Perplexity is controlled.
Four Agent POST attempts do not promise 100% successful research or a hard
dollar maximum; the user explicitly assumes limited variable-cost risk after
checking actual personal balance and disabled auto-recharge. If an Agent
response does not report cost, the conservative result is NO publication.

The Sept. 10 GitHub Actions run 34463156990 is still queued; GitHub Support
ticket #4966035 remains pending. A one-run manual approval must NOT
activate the dormant independent automatic controller or schedule.
An old queued run is not proven cancelled and is a material residual risk.

**No actual publication or billable provider request is approved by this
document or PR; merger alone does not grant a live-run authorization.**

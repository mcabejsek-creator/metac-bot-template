# One historic first-live Metaculus post: manual authenticated history GET

Date: 2026-11-08. DRAFT; this is **not** a publication workflow.

## Why

The encrypted publication journal preserves a new epoch, and historical
attempts were frozen rather than verified against actual Metaculus
history. The old GitHub Actions run #34463156990 remains queued with zero
jobs, and Support ticket #4966035 remains unresolved. Neither a frozen
journal nor an Actions status alone proves a public forecast exists.

The official Metaculus API docs (https://www.metaculus.com/api/) say
that authenticated accounts can read their own forecasts via the
`my_forecasts` field on `GET /api/posts/{postId}/`. The gateway can
replace restricted fields with null; null MUST NOT mean "empty history".

## Exact scope, not whole-history reconciliation

- One **previously approved** public post ID, **46347** (initial
  supervised first-live test). The URL is fixed in source:
  `GET https://www.metaculus.com/api/posts/46347/`.
- Token is **only** existing `METACULUS_TOKEN_V2` of the bot.
  Do not use a user's personal token, impersonation query, source code
  secret, bearer query string or any other credential.
- At most ONE GET per approved diagnostic, 15-second timeout, no redirects,
  no retries, response capped at 1MB.
- Validate exact post ID, individual atomic question, question ID,
  authoritative `my_forecasts.history` list, and consistency with
  `my_forecasts.latest`. Missing/null fields or any mismatch fail closed.
- Only safe anonymous indicators/counts are printed, not token, forecast
  values, question ID/text, full JSON, comments or private history.
- A positive result means **one particular post's own forecast is
  observable**. A negative result means only that none was observed in
  this response, **NOT proof a historic POST never happened**.
- The report ALWAYS says `all_historic_forecasts_reconciled=false`,
  `github_stuck_run_reconciled=false` and
  `publication_authorized=false`.
- This cannot establish that all old forecasts, comments or ambiguous
  attempts have been reconciled. No automatic controller activation or
  clearing of pending intents.

## One-shot manual workflow, no activation on merge

`.github/workflows/manual_one_post_history_readonly.yaml` uses only
`workflow_dispatch` with default `DO_NOT_RUN` and explicit
`YES_ONE_HISTORY_GET` confirmation, plus `github.run_attempt == 1`.
No cron, push, pull_request trigger, paid models or GitHub writes.
**Opening or merging the PR will NOT run this workflow.** It still
requires the operator's separate explicit approval before one actual GET.

CI uses only synthetic fakes (including missing/null history,
identity mismatch, 1MB body rejection, network failure and
conflicting `latest`) and the socket-level no-network guard.

## Financial risk and GitHub Support

Perplexity's API-key endpoint still does not expose authenticated
prepaid-wallet balance; the provider acknowledged a request for such
an endpoint in September–October 2026
(https://community.perplexity.ai/t/an-endpoint-that-reports-the-api-credit-balance-and-a-401-that-says-which-balance-is-empty/6128).
Do not claim the current 6-hour dashboard attestation provides safe
unattended spending. The external read-only watcher, old disabled cron,
current `main` publisher, journal and Support ticket are all untouched.

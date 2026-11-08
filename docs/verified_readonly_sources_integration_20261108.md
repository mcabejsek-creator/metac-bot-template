# FutureEval authenticated provider evidence – real GET-only sources (2026-11-08)

## What changed

PR #19 exposed authentic GitHub Actions queue counts as `IndependentReadings`
but correctly declined to fabricate any funding, journal, or operator flags.
This PR composes the **existing genuine source implementations**:
1. `GitHubQueuePreflightSource` – authenticated bounded publisher-state GETs
   across tournament, Cup and Test Bot, as well as any future worker.
2. `SponsoredOpenRouterReader` – calls the existing authenticated
   `verify_sponsored_openrouter_credit` to inspect the **Metaculus-donated
   OpenRouter key only**, verifying the operator-supplied three-character
   fingerprint, BYOK accounting and $5 reserve. Uses exactly one
   authenticated key-metadata **GET**, never a billed chat completion.
3. `AuthenticatedJournalReader` – wraps the existing encrypted
   journal `audit_store` on the dedicated `metaculus-publication-state`
   branch, refusing any other repository/branch/path; retains
   `GetOnlyBoundedSession`'s hard limit of **202 GitHub GET attempts** and
   second-read snapshot consistency check. It never exports titles, post
   IDs or decrypted journal reports.

`ReadOnlyAuthenticatedSources.read()` obtains the GitHub queue first. A
queued or running publisher (including frozen run #34463156990) causes an
immediate **return with all other evidence flags unavailable**, rather
than touching donated funds or decrypting journal state unnecessarily.
Only a verified EMPTY queue causes the other two read-only source checks.

## Strict safety boundary: still NOT an authorized dispatcher

- The source does not touch Perplexity, whose personal API prepaid balance
  cannot currently be independently queried. `perplexity_spend_ok`
  and `perplexity_checked_at` therefore remain unset/false.
- The legacy encrypted publication journal explicitly records that
  historic outcomes were **frozen without full independent reconciliation**.
  It is NOT valid to assert `prior_publication_certain` merely because
  the new journal epoch has all its steps confirmed. Until independent
  Metaculus forecast history is verified, this flag stays FALSE.
- No `operator_approval_confirmed`, `automation_switch_confirmed`,
  `is_first_attempt`, dispatch CAS, or authorization is set by a
  provider GET. An empty queue plus sufficient sponsored credits
  is **not** authorization for an automated forecast.
- The combined source code is imported by **no existing workflow or
  CLI**. Merely merging cannot trigger even a read-only request.
- Tests use in-memory GitHub Contents, a synthetic OpenRouter response
  and a real encrypted AES-GCM journal codec with fake keys. There are
  **zero authenticated live requests, paid models, posts, or journal
  changes** in this PR.

## Remaining steps before actual unattended service

1. GitHub Support ticket #4966035: clear/resolve legacy queued
   `34463156990` or complete an approved evidence-based reconciliation
   before relaxing the queue gate. Do not bypass it.
2. Acquire a provider-enforced Perplexity spending policy / explicitly
   accept strictly limited variable-cost research exposure, with an
   independently authenticated ongoing reconciliation mechanism. A
   six-hour dashboard screenshot is insufficient for permanent polling.
3. Independently verify the old Metaculus publication outcomes to meet
   `previous_publication_outcome_certain`. The journal's frozen legacy
   policy alone does not prove the post was accepted.
4. Finish the future worker and separately authorized independent
   dispatcher, source-authenticated kill-switch checks before each
   billable request and Metaculus POST, and CAS intent/reconciliation.
5. Obtain explicit operator approval before creating live control
   branches, starting GitHub workflow dispatches, paid API requests,
   recurring automation or publication.

**The current public/private read-only monitors, external cron, encrypted
journal, workflow approvals and GitHub Support ticket remain unchanged.**

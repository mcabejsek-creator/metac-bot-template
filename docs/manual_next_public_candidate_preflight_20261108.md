# Operator-attended FutureEval candidate preview (GET-only)

Date: 2026-11-08. Proposed, not merged or activated.

## The practical gap

The previous fully bounded manual read-only preflight reports only aggregate
question counts and can inspect one *already-known* public post ID. This is not
enough to identify the next question for an operator-attended, separately
approved forecast. The public first-page scheduled watch has only incomplete
coverage and cannot responsibly recommend questions.

This PR reuses the **existing verified selector**
`select_one_automatic_candidate`, which is already tested against each
supported atomic type. The manual preview is allowed ONLY after the existing
bounded authenticated GitHub Actions workflow has completely scanned **both**
Fall FutureEval 2026 (33121) and MiniBench with at most **ten** real
Metaculus GETs altogether. Partial or contradictory inventories fail closed.

## Two entirely separate operator choices

Under existing manual workflow
`.github/workflows/readonly_inventory_preflight_manual.yaml`:

- `DO_NOT_RUN` (DEFAULT): zero jobs, zero GETs.
- `YES_READ_ONLY_MAX_TEN_GETS`: existing count-only/check-supplied-post mode,
  unchanged. No public IDs are printed.
- `YES_FIND_ONE_PUBLIC_POST_ID`: **NEW**, explicitly approved GET-only
  operator preview. After both scans, reveal exactly **one public post ID**
  and its public Metaculus URL, tournament, question type, and effective
  close time (UTC). NEVER reveal private/internal question IDs, titles,
  prior forecast values, token, private history, research or journal data.
  Do not combine this with the old operator-supplied candidate ID input.

The selector excludes known already-forecast questions, duplicates, invalid
or grouped/conditional questions, missing history/timing and candidates with
less than 90 minutes left. If no safe candidate exists, report
`candidate_found=false`, not an invented ID. Later re-verification at
publication time is mandatory because question statuses can change.

This is **NOT** a publisher. It makes no LLM or paid model requests, no
GitHub journal writes and no Metaculus POSTs. It neither enables the dormant
automatic-one CLI nor affects old cron-job.org or the scheduled first-page
read-only watcher. The current old publishing run #34463156990 remains
queued and blocks unattended dispatch. Support ticket #4966035 is pending.

## Cost and timing

The successful one-question first-live run #42476608583 on 2026-11-08
submitted one forecast. GitHub logs report estimated $0.0 model cost but
explicitly warn that untracked tools/models are omitted; this is **not**
a Perplexity receipt or hard budget assurance. For perpetual automated
paid research, Perplexity wallet balance still lacks an authenticated
read-only API-key endpoint as of the last independent documentation review.
We do not enable automatic research or spending here.

New public-post preview remains default-off and **must not be invoked until
the operator separately approves the actual read-only run**. The PR is
covered by synthetic no-network tests; no real Metaculus GET is performed
while creating, testing or merging this code.

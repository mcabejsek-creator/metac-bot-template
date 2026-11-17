# Vrchcab prospective model comparison

This bounded pilot compares the deployed GPT-4.1 three-estimate aggregate with
one blind GPT-5.6 Sol response (reasoning high) on the same frozen forecast
prompt. It measures deployed configurations, not equal-compute model capability.
It never publishes challenger forecasts or promotes a model automatically.

## Operation

- Only newly published binary forecasts after activation can enter the pilot.
  Previously answered questions are not reforecast. Production continues to
  select both new and older eligible unanswered questions as before.
- `shadow_capture.py` observes the exact final binary prompt before the original
  production model invocation. Capture/export failures do not change the
  production forecast. A successful publication readback is required before
  the encrypted input is uploaded.
- `openai_shadow.yaml` processes that input in a separate workflow and separate
  concurrency group. The challenger sees frozen evidence and formatting
  instructions, never the baseline answer, rationale, later research or tools.
- The source must be a trusted first-attempt main production run. Stale inputs
  older than fifteen minutes, closed questions and oversized inputs are skipped.
  After a verified publication, the existing production successor starts
  immediately; ordinary idle passes keep the existing fifteen-minute interval.
- The immutable experiment ID is `sol-shadow-20261117-v1`. Its existing ledger
  is `shadow-ledger.json` on branch `vrchcab-shadow-sol-20261117`. Missing or
  invalid accounting stops inference; it must never be reinitialized to recover
  a failed request. Twenty lifetime reservations are the maximum, across all
  runs, failures and restarts.

## Budget and expiry

Each attempt permanently reserves USD 0.24 before the request, with a maximum
of twenty attempts: USD 4.80 reserved for the whole experiment. Unknown outcomes
remain reserved and are never retried. This is an application-enforced
conservative estimate under verified published prices, not an OpenAI account
spending cap or a verified billing statement.

The dedicated `VRCHCAB_OPENAI_TEST_KEY` is the only provider credential in the
inference step. No payment method, recharge, fallback model or budget reset is
configured here. Production research retains its existing separate weekly
budget. The user has already activated the API grant for the key's organization;
this code does not inspect or change account billing.

Prices checked 2026-11-18 (CET): USD 4/M ordinary input, USD 5/M default 30-minute
cache writes, USD 20/M output. Usage estimates conservatively charge all input
at USD 5/M. The complete serialized request is at most 22,000 UTF-8 bytes,
with a further 512-token protocol allowance; output is capped at 6,000 tokens,
including reasoning. The worst estimated request is USD 0.23256, below its
reservation. Actual invoices may be lower because of tokenization/cache reads.

The pilot expires at **2026-11-30 00:00 UTC**, before the end of the verified
promotional price period and before the known December grant expiry. Continuing
requires a separately reviewed policy; neither time nor budget resets itself.

Primary pricing documentation:

- https://developers.openai.com/api/docs/models/gpt-5.6-sol
- https://developers.openai.com/api/docs/guides/prompt-caching

## Accuracy and reporting

`shadow_report.yaml` runs daily at 08:11 UTC (09:11 Prague in winter) and once
when this report code is deployed. It only reads the separate encrypted ledger
and up to twenty Metaculus question outcomes, with no inference or publication.
The resulting `shadow-accuracy-aggregate` artifact contains counts and aggregate
scores, without raw evidence, rationales or individual probabilities.

The offline evaluator `shadow_evaluate.py` validates input hashes, identities,
source commits, model and timestamp order. It requires separately obtained
actual closing and resolution timestamps. A pair is scored only when the
candidate completed before the captured, refreshed and independently verified
closing/resolution deadlines. Failed, pending, skipped, unresolved and excluded
records remain distinct from scored observations. Missing timestamps never
turn into an assumed successful comparison.

Scores are binary Brier score and natural-log loss (lower is better); log loss
uses a 1e-15 numerical floor. Candidate-minus-baseline differences below zero
favor the candidate. These diagnostic proper scores are not Metaculus peer
scores. A successful HTTP request or twenty completed calls proves neither
accuracy improvement nor a contest prize probability. Twenty questions are a
small prospective pilot; no automatic model switch follows the result.

For an entirely offline evaluation of already exported, decrypted local data:

```sh
python -B shadow_evaluate.py --pairs pairs.json --outcomes outcomes.json
```

Decrypted files are private local inputs. Raw prompts/results remain encrypted
in repository state and workflow artifacts; the report prints only aggregates.

## Release verification

Run focused shadow tests and the complete strict offline suite on the exact
candidate commit, obtain independent Codex review, then merge through the PR.
Check actual production and shadow/report job steps after deployment. A healthy
empty ledger or `no_new_eligible_capture` means the comparison is waiting for a
new eligible binary forecast, not that an accuracy result already exists.

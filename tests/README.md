# Offline regression tests

Cross-mode recovery fix on `fix/cross-mode-publication`: **273 tests, all pass**.
The original PUB-MODE-1 passes without an expected-failure tag. Strict mode and
simulated Actions both exit 0 with no unexpected live attempts. See
[recovery, schema migration and cutover](../docs/cross-mode-publication.md).

Run from the repository root, using the dependencies already installed from
`poetry.lock` (including `forecasting-tools==0.2.92`):

```bash
.venv/bin/python -B -m tests.run
# Optional machine-readable report, outside the checkout:
.venv/bin/python -B -m tests.run --report /tmp/metac-regressions.json
# Strict check: any newly tagged known bug also fails the run.
.venv/bin/python -B -m tests.run --strict-known-bugs
```

Poetry users can substitute `poetry run python -B`. Running tests requires no new
test dependency, manifest/lockfile change, production CLI or GitHub run. The current
instance was validated with Python 3.12 and a separate real CPython 3.11.17
environment: 273/273 pass in strict mode and simulated Actions on 3.11 too.
An actual GitHub-hosted job has not been dispatched. See the
[deployment runbook and offline-only CI draft](../docs/rollout-runbook.md).
Use the supplied runner rather than a bare `unittest discover`: its result policy
is part of the safety contract. Running plain discovery with the correct package
root reports tagged known bugs as failures instead of hiding them; none remain
on the question-context follow-up branch.

## Safety boundaries

- `tests/__init__.py` installs guards before importing application code.
- Test-process application credentials are removed and `.env` loading is disabled.
  The only synthetic Perplexity binding is `TEST_ONLY_NOT_A_REAL_CREDENTIAL`.
- DNS, Internet sockets, urllib, requests, httpx and real model calls are blocked.
  Socket sends on asyncio's local Unix socketpair are allowed for loop wakeups.
- Agent tests replace HTTP with local JSON and usually replace daemon scheduling
  with an inline fake worker. Deadline tests use bounded local workers with fake
  transports. Backoff is simulated: no real API requests, paid work or multi-second
  retry sleeps.
- Publication tests use `LocalMetaculus` or a fake requests transport. Tests that
  enable `publish_reports_to_metaculus` write only to this in-memory sink.
- Neither CLI entrypoint is executed. In particular, **do not run `main.py --mode
  tournament` or `main.py --mode test_questions` to validate these changes**.
- The publication follow-up includes two guarded local Python subprocesses to
  verify SQLite coordination across independent processes. Review-workflow tests
  run only its Bash input-validation prefix, never its review/API command.
  No production CLI or GitHub Action is started. The runner has a
  10-second watchdog per test on Linux; this is a test safeguard, not a fix for
  missing production deadlines. Guards protect this test process, not arbitrary
  commands launched separately by a developer.
- Runtime tests also use bounded local daemon workers and one pure stdlib sleeper
  subprocess to exercise TERM/KILL, without importing the bot in that child.
  A test-loop heartbeat compensates for sandbox-denied Unix self-pipe sends;
  network guards, the per-test watchdog and production wakeups stay unchanged.

## Interpreting results

Passing tests protect existing behavior: binary close/resolution separation,
effective-close helper, complete forecast criteria, initial research options,
hard-bound instructions, CBO year/value context, units, numeric/date CDF integrity,
real SDK parsing and aggregation, the 2×3 binary pipeline, skip behavior, 403
fail-fast behavior, failure propagation and current binary clamp.

Open defects assert **desired** behavior, tagged with `@known_bug("ID")`. Only an
explicit `require_fixed(...)` assertion becomes an expected failure in the default
runner. An unrelated assertion, exception, watchdog timeout or caught live-network
attempt still fails the run. A known-bug test that unexpectedly passes also fails
the run: inspect the fix, then remove the tag and retain the regression test.
Runner self-tests exercise these policies. Do not use `unittest.expectedFailure`,
blanket exception handlers or `skip` to make new regressions green.

The stalled-model deadline test accelerates application `asyncio.timeout` and
`asyncio.wait_for` deadlines to zero; its separate observer cancels the task if
no deadline is installed. It does not wait minutes for a mocked transport, and
does not mistake the harness watchdog for an application timeout.

At `ae727b3`, `--strict-known-bugs` exited 0; audit `0a98f97` then found a new
ordinary cross-mode failure. That failure is now fixed, with 30 additional
recovery regressions, and strict mode again exits 0. JSON separates passing checks, known
failures, ordinary failures/errors, skips, unexpected successes and live attempts.

## Historical evidence versus local contracts

`fixtures/historical_cases.json` reconstructs cases from the user-supplied
`prompt codex vlakno.docx`, dated 2026-11-06. These are **synthetic fixtures**, not
original Metaculus records. Original full criteria/logs were not supplied; the
criteria strings are explicit local contracts. Community estimates are excluded
because disagreement is not proof of a forecasting error.

The clock is fixed at 2026-11-06 12:00 UTC. Cases cover Japanese Diet and GPR timing,
FDA options, CBO FY2027/FY2028, current forecasters / 730 days, cumulative and
window-extreme bounds, percentages versus percentage-point differences, UNESCO
baseline versus hard bound, Lake Mead observed versus projected values, and dates.

Prompt tests check preservation of instructions and exact fixture context; they
do **not** prove an LLM obeys those instructions. CBO's corrected answer is supplied
by the simulated Agent, proving routing of corrected research, not fact-checking
intelligence. Structured-data retrieval, tracker-transition analysis, calibration,
evidence weighting and the forecasting effect of the VVIX rule remain unverified.
The original historical outcomes are reported by the handover, not independently
revalidated with live services.

The duplicate-publication cases now cover worker instances, independent Python
processes sharing a SQLite journal, restarts, partial publication and ambiguous
acknowledgements. Passing them cannot prove distributed idempotence across
independent GitHub runners without shared durable storage or API support. The
Actions follow-up simulates that storage through conditional GitHub Contents API
operations, encrypted per-question files and a resource index. It also exercises
initialization, lost acknowledgements, deleted files and key changes. All GitHub
responses are fake; no remote ref is created by the suite. See
[Actions guarantees and deployment prerequisites](../docs/github-actions-publication.md).

## Scope and follow-up

See [handover comparison and repair order](../docs/handover-audit.md).
Workflow files are read by the tests for configuration assertions only. Publishing
defaults, model choices and ensemble size are unchanged by the test infrastructure.
Each follow-up documents any authorized production changes separately.
The `fd407db` infrastructure commit changed no production code; its publication
follow-up adds `SafePublicationMixin` to the bot and promotes PUBLICATION-1/2 to
passing regressions. The Actions follow-up adds a shared group and durable backend
and promotes CONCURRENCY-2. Other known-bug tests remain unchanged. Future fixes should
promote relevant known-bug tests one issue at a time; attach the default and
strict results to their separate PRs.

The runtime follow-up promotes TIMEOUT-1, TIMEOUT-2 and CONCURRENCY-1 and adds
question/model/IO/run deadlines, cancellation and supervisor tests. Current result:
148 tests, 134 passed, 14 known failures at that step. See [runtime limits](../docs/runtime-limits.md).
Forecasting model parameters and prompts remain unchanged; publishing workflows
now wrap the original bot command in a hard-deadline process supervisor.

The research follow-up promotes five RESEARCH-1/2/3 regressions. Current result:
173 tests, 164 passed, 9 expected failures at that step. Added cases cover structural validation,
empty fact-checks, optional research, partial success, permanent billing/auth errors,
bounded transient retry, Retry-After and cancellation/deadlines. A few SDK tests
explicitly restore the original SDK invoke body with a synthetic direct model
boundary; all underlying HTTP/socket guards remain installed. See
[research validation and retry decisions](../docs/research-safety.md).

The forecast validation follow-up promotes MC-1/2/3. Result at that step: 214 tests,
208 passed, 6 expected failures. New cases cover the six existing SDK types, strict
raw forecast values, exact option identities, CDF/bound contracts, UTC date-only
inputs, aggregation, frozen-plan binding and typed conditional round-trips.
Invalid parser retries retain SDK attempt counts, with only the one-second sleep
accelerated in validation tests. Model parameters and prompt text stay unchanged.
See [forecast validity and compatibility decisions](../docs/forecast-validation.md).

The question-context follow-up promotes all six remaining tests: TIMING-1 has two
cases (numeric and MC), while BOUNDS-1 and CONTEXT-1/2/3 have one each. Current
result at that step: **242 passed, no expected failures, skips or unexpected live attempts**,
also in strict mode and with simulated Actions variables. The 28 new tests cover
timing roles/UTC, hard versus nominal/intraday date bounds, exact multiline source
data, original group/member rules, summaries, parser inputs, conditional chains,
complete explanations and safe recovery of frozen plans on independent runners.
Necessary prompt/data additions and the MC timing wording change are shown
individually in [question context and compatibility](../docs/question-context-integrity.md).

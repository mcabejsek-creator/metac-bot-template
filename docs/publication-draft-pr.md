# Draft PR: Journal Metaculus writes and quarantine ambiguous publication outcomes

The SDK retries forecast/comment POSTs after lost acknowledgements. Concurrent
workers can publish the same stale question snapshot, and a successful forecast
followed by a rejected comment has no recovery path. Introduce a durable SQLite
publication journal at the bot's publication boundary.

Stage the original report and exact SDK write plan before HTTP. Atomically claim
each step, skip confirmed steps and resume explicit rejections without generating
a new forecast. Quarantine timeouts/unknown outcomes instead of replaying them.
Use single-attempt POSTs without assumed server idempotency support. Protect
competing workers sharing the same persistent journal and publication identity.

Forecasting methods, prompts, models and CLI parameters remain identical to
`fd407db`; workflows, Secrets, dependencies and the alternative bot are unchanged.
No live API was called. Model calls for a normal fresh report are unchanged;
recovery reuses the frozen report. This is a client-side at-most-once attempt
policy for ambiguous writes, not a server-backed exactly-once guarantee.

Validation uses the existing guarded offline runner, promotes PUBLICATION-1/2,
and adds persistence, partial failure, lost acknowledgement, HTTP status/payload
compatibility and independent-process coordination cases. See
`docs/publication-safety.md` for storage configuration and limitations.
Current offline result: 74 tests, 56 passed, 18 unchanged expected failures;
strict mode has exactly those 18 failures, no errors, skips or unexpected live
attempts. PUBLICATION-1/2 now pass. AST comparison confirms every forecasting
method and the complete CLI configuration match `fd407db`.

Deployment prerequisite: provision durable shared coordination for all cooperating
workers, using one stable non-secret account scope. Independent GitHub runners
do not share the default journal; their global duplication risk is unresolved.
Do not delete the journal on rollback after any real writes: preserve it and
reconcile pending/unknown operations before re-enabling legacy SDK retry paths.

This PR description is local only. No branch has been pushed, merged or deployed.

# Draft PR: Coordinate Actions publication through encrypted GitHub journal files

Independent hosted runners lose the local publication journal after a job ends.
A shared concurrency group alone cannot distinguish a new attempt from a POST
whose acknowledgement was lost. Use a dedicated state branch in this repository
with conditional Contents API updates, keeping existing forecasting code intact.

All three publishing workflows and the manual initializer share one group across
branches and run attempts. Store an encrypted resource index and one encrypted
SQLite journal per question; stage and remotely acknowledge `sending` before any
Metaculus POST. Preserve frozen reports for partial recovery, reject conflicting
updates and keep ambiguous operations blocked. Missing registered files, invalid
keys and GitHub outages never trigger a fresh local journal fallback.

Use existing GITHUB_TOKEN/Metaculus secret, no new Secrets or external service.
State is encrypted with domain-separated keys; token rotation requires a planned
migration. Initial setup is explicit and cannot overwrite an existing ref. The
new ref has an orphan history, without editing source or workflow files.

Offline tests model independent runners, stale reads, concurrent creation and CAS,
lost acknowledgements at each boundary, deleted shards, manifest races, encryption,
reruns, partial writes, unchanged commands/triggers and minimum token permissions.
No API was called; live GitHub permissions/REST behavior remain deployment checks.
See `docs/github-actions-publication.md` for limits, rollout and rollback guidance.

Validation: 120 offline tests, 103 passed, 17 unchanged known failures, zero live
attempts. The same suite passed with simulated Actions environment variables.
Strict mode reports exactly those 17 backlog failures. Python/YAML/Bash syntax
and unchanged forecasting sources, triggers, schedules and commands were checked.

Rollout must first stop legacy writers, reconcile previous unknown outcomes and
avoid rerunning historical workflow SHAs that bypass the protocol. Keep state
history/keys on rollback; do not reactivate legacy POST retries over pending writes.
No push, remote initialization, merge or deployment was performed for this draft.

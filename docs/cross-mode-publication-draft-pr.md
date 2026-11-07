# Fix cross-mode publication recovery without creating another forecast

A Cup/test comment failure followed by tournament recovery could publish another
forecast; the reverse transition had the same issue. Persist each recovery
intent's binding to the original plan before draining it, and preserve that
choice through CAS retries and workflow reruns. Tournament also honors confirmed
journal receipts when API history is stale. Sending/uncertain stays quarantined.
Forced Actions reruns without a saved intent or pending recovery block before
models, covering a failed pre-binding checkpoint followed by another runner's recovery.

SQLite v2 adds bindings without rewriting frozen plans. Reopening v1 is supported;
older code must not be used after v2 checkpoints. Forecast prompts/models/settings,
including production's latest MC change, and all workflows remain unchanged.

Validation: **273 offline tests pass**, strict mode and simulated Actions, no
known failures or unexpected live attempts. Includes the original PUB-MODE-1,
both directions, independent runners, crash windows, CAS races, typed reports
and migration. See [compatibility and deployment gates](cross-mode-publication.md).

Local draft only. No live business API, push, merge, deployment or Secrets changes.

# Draft PR: Bound forecasting work and cancel publication safely

The research-only semaphore lets forecast/parser calls escape the configured
limit, while synchronous HTTP blocks cooperative deadlines. Bound admitted
questions through publication and share operation permits across research,
models and IO. Add 60-minute question, 120-minute conditional and 180-minute run
budgets; preserve existing prompts, models, sampling, aggregation and retry policy.

Move each synchronous publication boundary to a bounded daemon worker. Cancellation
never continues from a detached claim into a POST, retains sending for unknown
outcomes, and holds permits until actual worker completion. Keep the encrypted
GitHub CAS state protocol and cross-workflow concurrency intact. Join sibling
tasks on failure and clean notepads. Add a POSIX process-group supervisor with
TERM/60-second grace/KILL and 210-minute Actions job limits; schedules and bot
arguments stay unchanged.

Validation: 148 offline tests, 134 passed, 14 remaining known failures. Promote
TIMEOUT-1, TIMEOUT-2 and CONCURRENCY-1; no other expected failures removed. Tests
include independent-runner cancellation, partial publication, late IO completion,
model/parser deadlines, shared permits and a pure stdlib local child process.
The suite also passes with simulated Actions variables; strict mode reports
exactly the remaining 14 known failures. AST comparison confirms unchanged
forecast methods/prompts and bot parameters; YAML/Bash checks confirm unchanged
triggers, schedules, concurrency, permissions and state configuration.
No live API, push, merge, deployment, remote state initialization or Secrets change.
See `docs/runtime-limits.md` for budget reasoning, guarantees and remaining risks.

# Draft PR: Validate forecasts against Metaculus question contracts

Multiple-choice predictions were checked against each other, allowing duplicate,
missing and unknown question options to reach publication. Guard raw parser JSON
before coercion, normalization or dict conversion; validate each prediction and
the aggregate against its question. Cover the existing binary, MC, numeric,
discrete, date and conditional paths without adding types or changing prompts,
models, sampling, probability methods, research rules or runtime limits.

Validate complete frozen publication plans before claiming any new step, binding
wire values to the saved report and current question constraints. Reject changed
contracts or incomplete legacy metadata without rewriting intents. Preserve
uncertain-write quarantine, encrypted GitHub CAS and single-attempt publication.
Retain typed conditional question snapshots on serialization; restore legacy
subtypes only with sufficient embedded API evidence. Date-only inputs use UTC
as the existing prompt specifies; ambiguous unzoned times are rejected.

Validation: 214 offline tests, 208 passed, 6 remaining known failures. Promote
MC-1/2/3. Test real locked SDK parsing/aggregation, all supported payload shapes,
invalid values, CDF constraints, complete pipeline restoration, conditional
round-trips and pre-claim rejection. No live API, push, merge, deployment or Secrets
changes. See `docs/forecast-validation.md` for exact behavior changes and limits.

The same suite passes with simulated Actions variables; strict mode reports
exactly the six unchanged backlog failures. Static checks confirm unchanged
prompt bodies and model parameters. Workflow files, research policy, runtime
budgets, journal transactions and the GitHub backend are untouched.

# Draft PR: Validate research and bound transient retries

Agent failures, source-only responses and empty fact-checks previously reached
forecasting as successful research. Require completed, well-formed, nonempty
Agent reports for both passes, and nonempty text for enabled LLM researchers.
Keep explicitly disabled research functional; never convert a provider failure
into optional research. Preserve the existing successful-prediction threshold:
one valid branch may still support a report with visible research errors.

Retry Agent 408/429/5xx/network failures at most three times within the existing
210-second phase deadline, with jitter/backoff and Retry-After. Stop on permanent
errors and remember 401/403 per bot instance/provider. Distinguish abandoned IO
deadlines from completed socket timeouts. Apply classified retry only to the SDK
researcher copy, preserving allowed_tries and avoiding nested application retries.
Forecast models, prompts, sampling, probability methods, publication, GitHub
coordination, workflow schedules and existing runtime budgets stay intact.

Validation: 173 offline tests, 164 passed, 9 remaining expected failures. Promote
five RESEARCH-1/2/3 regressions. No live API, push, merge, deployment or Secrets
change. See `docs/research-safety.md` for exact forecast/no-forecast decisions,
the SDK text-only warning, historical journal limitations and possible duplicate
research billing after a lost reply.

The same suite passed with simulated Actions variables; strict mode reports
exactly the nine remaining known failures. AST checks confirm unchanged prompts,
forecast methods, model settings and runtime budgets. Publication sources and
all workflows remain identical to `c71e79f`.

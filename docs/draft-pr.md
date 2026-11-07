# Draft PR: Offline regression harness for Metaculus forecasting and publication

The production bot can publish from both CLI modes, while its concurrency,
publication retries and research validation have no safe automated regression
coverage. Add a local-only test harness, synthetic historical fixtures and an
evidence map comparing the supplied handover with checkout `1eaf23c`.

- Block network transports, real model calls and `.env` loading before app import;
  replace publication with in-memory sinks and Agent responses with local JSON.
- Exercise the actual SDK parsing/report pipeline, numeric/date CDFs, timing,
  criteria, answer options, hard-bound instructions and historical CBO context.
- Record unfixed contracts as explicit known failures. Unrelated exceptions,
  hidden live attempts and unexpectedly passing known-bug tests fail the runner.
- Provide strict backlog mode, a per-test watchdog and optional JSON reporting.

No production Python, prompts, dependency files or Actions files are changed.
No additional API calls or API costs; no production CLI execution, deployment,
schedule changes or publication. A green default suite means established checks
passed and known defects were reproduced, not that those defects are fixed or
that forecasting quality has been semantically validated.

Validation:

```bash
.venv/bin/python -B -m tests.run --report /tmp/metac-regressions.json
.venv/bin/python -B -m tests.run --strict-known-bugs
```

The strict command is expected to exit 1 for the documented backlog. See
`docs/handover-audit.md` and `tests/README.md` for limitations and repair order.
Current-instance result: 50 tests, 30 passed, 20 expected known failures; strict
mode reports those 20 as failures. No additional errors, skips or unexpected live
attempts. Syntax validation passed; production and workflow diffs are empty.
Rollback consists of removing the added `tests/` and documentation files; no
production rollback or credential changes are needed.

This is a local PR description only. No remote branch was pushed and no GitHub
API was called to create a pull request.

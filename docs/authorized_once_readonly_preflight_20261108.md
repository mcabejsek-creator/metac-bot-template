# Single authorization record — 2026-11-08

User explicitly approved **one** authenticated read-only metadata preflight
for Fall FutureEval (project 33121) and MiniBench, **at most ten Metaculus GETs total**.

Only the isolated preflight workflow is in scope. No Metaculus POST,
forecast generation, paid AI model, merge, update to the publication journal,
or restoration of scheduled publishing was authorized.

The one-shot trigger is this commit alone. Never reuse the authorization
message or rerun this workflow; request fresh user approval first.

Technical safeguards: GitHub Actions contents:read, at most 5 paginated
GETs per tournament, aggregate 10-GET cap, 6 seconds between GETs,
no retries, no redirects, and fail-closed on missing history/timing.
Only aggregate counts can be printed.

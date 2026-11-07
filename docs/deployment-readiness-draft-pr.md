# Integrate current production prompt and gate deployment on cross-mode recovery

Historical draft for `0a98f97`. The follow-up fixes the blocker and passes 269
tests; see [current fix and deployment conditions](cross-mode-publication.md).

Preserve the complete multiple-choice prompt update from production `91bf94b`
on top of all fixes through `ae727b3`. Forecast models, settings and all safety
modules/workflows remain unchanged.

The release audit found a blocker: recovering a rejected Cup/test comment from
tournament mode also creates a new initial forecast, despite the source question
being already forecasted. A new ordinary offline regression reproduces two
forecasts instead of one on independent simulated GitHub runners.

Validation: **243 tests, 242 pass, 1 failure**, in strict local mode and simulated
Actions; no expected failures/errors/skips/unexpected live calls. This branch is
**not ready for deployment**. Keep the new failing test as a release gate.

[Audit and deployment/rollback plan](deployment-readiness.md) describe the missing
state branch, reconciliation of old runs, permissions/key prerequisites and safe
initialization. No business API calls, real publication, push, merge, Secrets
changes or deployment were performed. This is a local draft only.

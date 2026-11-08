# Single-use encrypted publication journal read audit — 2026-11-08

Authorized by user for ONE GitHub Actions run with read-only GitHub GET requests,
maximum 202 GET attempts in total, using Metaculus V2 token ONLY as the
existing publication journal decryption key. The run may print anonymous
aggregate journal states but MUST NOT disclose question IDs, saved reports,
forecast values, cryptographic secrets or journal plaintext.

Forbidden: all Metaculus API requests, forecast or comment POSTs, paid AI
model calls, GitHub state writes, merge, or publishing/scheduling restarts.

Scope: existing encrypted GitHub state branch
`metaculus-publication-state`, `publication.enc` and its registered shards only.
The single-purpose GitHub Actions workflow runs a synthetic precheck,
then performs only bounded authenticated GETs. Retry attempts are disabled.

THIS commit triggers and consumes the single-use authorization. No new
authorization follows from this file or its content. The temporary push
trigger must be removed immediately after finalization.

"""Explicitly dispatch one bounded shadow comparison after artifact upload.

GitHub's temporary token supports workflow_dispatch. The separate worker still
verifies the completed source, original encrypted evidence and lifetime ledger.
An uncertain dispatch is never retried here; duplicate trigger events consume
no additional attempt because the worker shares its existing concurrency/ledger.
"""
from datetime import datetime, timezone
import http.client
import json
import os
import re

REPOSITORY = "mcabejsek-creator/metac-bot-template"
ROOT = "/repos/" + REPOSITORY
SOURCE_WORKFLOW = ".github/workflows/run_bot_automatic.yaml"
WORKFLOW = "openai_shadow.yaml"
ENDS_AT = datetime(2026, 11, 30, tzinfo=timezone.utc)
MAX_BYTES = 100_000


class DispatchShadowError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token):
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise DispatchShadowError("github_key_unavailable")
        self.token = token

    def _request(self, path, payload=None):
        connection = http.client.HTTPSConnection("api.github.com", timeout=20)
        try:
            connection.request("GET" if payload is None else "POST", ROOT + path,
                body=None if payload is None else json.dumps(payload).encode(),
                headers={"Authorization": "Bearer " + self.token,
                    "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                    "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "vrchcab-shadow-dispatch-v1"})
            response = connection.getresponse()
            if response.status != (200 if payload is None else 204):
                raise DispatchShadowError("github_dispatch_or_read_not_acknowledged_no_retry")
            if payload is not None:
                return None
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise DispatchShadowError("source_response_too_large")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise DispatchShadowError("source_response_invalid")
            return data
        except (OSError, ValueError, http.client.HTTPException):
            raise DispatchShadowError("github_outcome_unavailable_no_retry") from None
        finally:
            connection.close()

    def own_run(self, source_id):
        return self._request("/actions/runs/" + str(source_id))

    def dispatch(self, source_id):
        return self._request("/actions/workflows/" + WORKFLOW + "/dispatches",
            {"ref": "main", "inputs": {"source_run_id": str(source_id)}})


def dispatch_shadow(environ=None, *, api=None, now=None):
    env = os.environ if environ is None else environ
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REPOSITORY") != REPOSITORY
            or env.get("GITHUB_REF") != "refs/heads/main" or env.get("GITHUB_RUN_ATTEMPT") != "1"
            or env.get("GITHUB_WORKFLOW_REF") != REPOSITORY + "/" + SOURCE_WORKFLOW + "@refs/heads/main"
            or env.get("GITHUB_EVENT_NAME") not in ("push", "schedule", "workflow_dispatch")
            or not re.fullmatch(r"[1-9][0-9]{0,19}", env.get("GITHUB_RUN_ID", ""))
            or not re.fullmatch(r"[0-9a-f]{40}", env.get("GITHUB_SHA", ""))
            or env.get("VRCHCAB_VERIFIED_FORECASTS") != "1"
            or not re.fullmatch(r"[1-9][0-9]{0,19}", env.get("VRCHCAB_SHADOW_ARTIFACT_ID", ""))):
        raise DispatchShadowError("trusted_verified_capture_required")
    if any(env.get(name) for name in ("METACULUS_TOKEN", "OPENROUTER_API_KEY",
            "PERPLEXITY_API_KEY", "VRCHCAB_OPENAI_TEST_KEY")):
        raise DispatchShadowError("dispatch_credential_isolation_failed")
    current = datetime.now(timezone.utc) if now is None else now
    if current >= ENDS_AT:
        return {"status": "expired_no_dispatch"}
    source_id = int(env["GITHUB_RUN_ID"])
    api = GitHub(env.get("GITHUB_TOKEN", "")) if api is None else api
    source = api.own_run(source_id)
    if (type(source.get("id")) is not int or source["id"] != source_id
            or source.get("repository", {}).get("full_name") != REPOSITORY
            or source.get("head_repository", {}).get("full_name") != REPOSITORY
            or source.get("path") != SOURCE_WORKFLOW or source.get("head_branch") != "main"
            or source.get("head_sha") != env["GITHUB_SHA"] or type(source.get("run_attempt")) is not int
            or source["run_attempt"] != 1 or source.get("event") != env["GITHUB_EVENT_NAME"]
            or source.get("status") != "in_progress" or source.get("conclusion") is not None):
        raise DispatchShadowError("source_run_not_verified")
    api.dispatch(source_id)
    return {"status": "one_shadow_dispatch_requested", "source_run_id": source_id,
            "workflow": WORKFLOW, "ref": "main"}


def main():
    try:
        result = dispatch_shadow()
    except Exception:
        # Never print provider responses, credential values or an uncertain body.
        print("SHADOW_DISPATCH stopped_without_retry; production and successor remain independent")
        return 1
    print("SHADOW_DISPATCH " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

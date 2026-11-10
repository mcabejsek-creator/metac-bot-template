"""Queue one successor of the authorized V2 workflow; no forecasts or model calls.

GitHub explicitly allows GITHUB_TOKEN to trigger workflow_dispatch events.
The workflow supplies actions:write and the same publication concurrency lock.
Native cron remains the fallback if this single attempt fails. No HTTP retries.
"""

from datetime import datetime, timedelta, timezone
import json
import os
import re
import time
import urllib.error
import urllib.request


REPOSITORY = "mcabejsek-creator/metac-bot-template"
WORKFLOW = "run_bot_automatic.yaml"
WORKFLOW_PATH = ".github/workflows/" + WORKFLOW
ROOT = "https://api.github.com/repos/" + REPOSITORY
INTERVAL_SECONDS = 15 * 60
MAX_BYTES = 5_000_000
ALLOWED_EVENTS = {"push", "schedule", "workflow_dispatch"}


class QueueNextError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise QueueNextError("GitHub redirect refused; native cron remains available")


class GitHub:
    def __init__(self, token, *, opener=None):
        if not isinstance(token, str) or not token or token.strip() != token:
            raise QueueNextError("Temporary GitHub token is unavailable")
        self.token = token
        self.opener = opener or urllib.request.build_opener(_NoRedirect())

    def _request(self, suffix, *, dispatch=False):
        request = urllib.request.Request(
            ROOT + suffix,
            data=json.dumps({"ref": "main"}).encode() if dispatch else None,
            method="POST" if dispatch else "GET",
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                if response.status != (204 if dispatch else 200):
                    raise QueueNextError("GitHub request not acknowledged; no retry")
                if dispatch:
                    return None
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise QueueNextError("GitHub run response exceeded the fixed size limit")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise QueueNextError("Invalid GitHub run response")
                return data
        except (urllib.error.URLError, OSError, ValueError):
            raise QueueNextError("GitHub outcome unavailable; no retry, native cron remains available") from None

    def own_run(self, run_id):
        return self._request("/actions/runs/" + str(run_id))

    def workflow_runs(self):
        return self._request("/actions/workflows/" + WORKFLOW + "/runs?branch=main&per_page=100")

    def dispatch(self):
        return self._request("/actions/workflows/" + WORKFLOW + "/dispatches", dispatch=True)


def _moment(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise QueueNextError("GitHub run start time is unavailable") from None


def _context(environ):
    if (environ.get("GITHUB_ACTIONS") != "true"
            or environ.get("GITHUB_REPOSITORY") != REPOSITORY
            or environ.get("GITHUB_REF") != "refs/heads/main"
            or environ.get("GITHUB_WORKFLOW_REF") != REPOSITORY + "/" + WORKFLOW_PATH + "@refs/heads/main"
            or environ.get("GITHUB_RUN_ATTEMPT") != "1"
            or environ.get("GITHUB_EVENT_NAME") not in ALLOWED_EVENTS
            or not re.fullmatch(r"[1-9][0-9]*", environ.get("GITHUB_RUN_ID", ""))
            or not re.fullmatch(r"[0-9a-f]{40}", environ.get("GITHUB_SHA", ""))):
        raise QueueNextError("Only this workflow's first main-branch run may queue its successor")
    return int(environ["GITHUB_RUN_ID"])


def _validate_own_run(run, run_id, environ):
    if (run.get("id") != run_id or type(run.get("id")) is not int
            or run.get("run_attempt") != 1
            or run.get("repository", {}).get("full_name") != REPOSITORY
            or run.get("path") != WORKFLOW_PATH
            or run.get("head_branch") != "main"
            or run.get("head_sha") != environ["GITHUB_SHA"]
            or run.get("event") != environ["GITHUB_EVENT_NAME"]
            or run.get("status") != "in_progress"
            or type(run.get("workflow_id")) is not int):
        raise QueueNextError("Current automatic workflow identity/state is not verified")
    return _moment(run.get("run_started_at"))


def queue_next(environ=None, *, api=None, now=None, sleep=time.sleep):
    environ = os.environ if environ is None else environ
    run_id = _context(environ)
    api = api or GitHub(environ.get("GITHUB_TOKEN", ""))
    now = now or (lambda: datetime.now(timezone.utc))
    current = api.own_run(run_id)
    started = _validate_own_run(current, run_id, environ)
    moment = now()
    if started > moment + timedelta(seconds=30):
        raise QueueNextError("GitHub run start time is in the future")
    # Drain simultaneous MiniBench/season questions without an idle delay
    # after a verified success. Empty/failed/uncertain passes retain the
    # 15-minute cadence. Every successor still scans and selects at most one.
    verified_success = (environ.get("VRCHCAB_BOT_OUTCOME") == "success"
                        and environ.get("VRCHCAB_VERIFIED_FORECASTS") == "1")
    interval = 0 if environ["GITHUB_EVENT_NAME"] == "push" or verified_success else INTERVAL_SECONDS
    remaining = min(INTERVAL_SECONDS, max(0, (started + timedelta(seconds=interval) - moment).total_seconds()))
    while remaining > 0:
        seconds = min(30, remaining)
        sleep(seconds)
        remaining -= seconds

    # Recheck cancellation and identity after the wait, before any dispatch.
    latest_current = api.own_run(run_id)
    _validate_own_run(latest_current, run_id, environ)
    if latest_current.get("workflow_id") != current["workflow_id"]:
        raise QueueNextError("Current workflow identity changed")
    envelope = api.workflow_runs()
    runs = envelope.get("workflow_runs")
    if not isinstance(runs, list) or len(runs) > 100:
        raise QueueNextError("Next-run inventory is unavailable")
    for other in runs:
        if not isinstance(other, dict) or type(other.get("id")) is not int:
            raise QueueNextError("Malformed next-run inventory")
        if (other["id"] > run_id and other.get("workflow_id") == current["workflow_id"]
                and other.get("path") == WORKFLOW_PATH
                and other.get("repository", {}).get("full_name") == REPOSITORY
                and other.get("head_branch") == "main" and other.get("run_attempt") == 1
                and other.get("status") in {"queued", "in_progress", "waiting", "pending", "requested"}):
            return {"status": "newer_run_already_waiting", "run_id": other["id"]}
    # Exactly one request. A timeout may already have queued a successor;
    # never repeat this POST within the current run.
    api.dispatch()
    return {"status": "one_successor_requested", "workflow": WORKFLOW,
            "ref": "main", "cadence_seconds": INTERVAL_SECONDS,
            "immediate_after_verified_forecast": verified_success}


def main():
    try:
        result = queue_next()
    except QueueNextError as error:
        print(str(error))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

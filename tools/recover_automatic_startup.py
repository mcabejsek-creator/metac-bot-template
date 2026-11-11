"""Recover an automatic workflow that failed before any job existed.

This is one fresh workflow_dispatch, never a rerun or a publication retry.
It cannot recover an outage that also prevents GitHub Actions/webhook delivery.
Only the temporary GitHub token is used; no model or Metaculus access.
"""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request


REPOSITORY = "mcabejsek-creator/metac-bot-template"
WORKFLOW = "run_bot_automatic.yaml"
WORKFLOW_PATH = ".github/workflows/" + WORKFLOW
RECOVERY_PATH = ".github/workflows/recover_automatic_startup.yaml"
ROOT = "https://api.github.com/repos/" + REPOSITORY
WINDOW = timedelta(hours=1)
MAX_STARTUP_FAILURES = 3
MAX_BYTES = 5_000_000
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
SOURCE_EVENTS = {"push", "schedule", "workflow_dispatch"}


class RecoveryUnavailable(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise RecoveryUnavailable("GitHub recovery redirect refused")


class GitHub:
    """Three fixed GETs and at most one POST, with no retry or redirect."""

    def __init__(self, token, *, opener=None):
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise RecoveryUnavailable("Temporary GitHub credential unavailable")
        self._token = token
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    def _request(self, suffix, *, dispatch=False):
        request = urllib.request.Request(
            ROOT + suffix, method="POST" if dispatch else "GET",
            data=b'{"ref":"main"}' if dispatch else None,
            headers={"Authorization": "Bearer " + self._token,
                     "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        try:
            with self._opener.open(request, timeout=20) as response:
                if response.status != (204 if dispatch else 200):
                    raise RecoveryUnavailable("GitHub recovery request not acknowledged; no retry")
                if dispatch:
                    return None
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise RecoveryUnavailable("GitHub recovery response exceeds size limit")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise RecoveryUnavailable("GitHub recovery response is not an object")
                return result
        except (urllib.error.URLError, OSError, ValueError):
            raise RecoveryUnavailable("GitHub recovery outcome unavailable; no retry") from None

    def run(self, run_id):
        return self._request("/actions/runs/" + str(run_id))

    def jobs(self, run_id):
        return self._request("/actions/runs/" + str(run_id) + "/jobs?per_page=1")

    def recent_runs(self):
        return self._request("/actions/workflows/" + WORKFLOW + "/runs?branch=main&per_page=100")

    def dispatch(self):
        return self._request("/actions/workflows/" + WORKFLOW + "/dispatches", dispatch=True)


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise RecoveryUnavailable("GitHub recovery timestamp unavailable") from None


def _context(env):
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REPOSITORY") != REPOSITORY
            or env.get("GITHUB_REF") != "refs/heads/main" or env.get("GITHUB_RUN_ATTEMPT") != "1"
            or env.get("GITHUB_EVENT_NAME") != "workflow_run"
            or env.get("GITHUB_WORKFLOW_REF") != REPOSITORY + "/" + RECOVERY_PATH + "@refs/heads/main"
            or not re.fullmatch(r"[1-9][0-9]*", env.get("GITHUB_RUN_ID", ""))
            or not re.fullmatch(r"[a-f0-9]{40}", env.get("GITHUB_SHA", ""))):
        raise RecoveryUnavailable("Only this recovery workflow's first main-branch run may dispatch")


def _event_source(event):
    if (not isinstance(event, dict) or event.get("action") != "completed"
            or not isinstance(event.get("repository"), dict)
            or event["repository"].get("full_name") != REPOSITORY):
        raise RecoveryUnavailable("Recovery event repository or action differs")
    source = event.get("workflow_run")
    if (not isinstance(source, dict) or type(source.get("id")) is not int or source["id"] <= 0
            or source.get("head_branch") != "main" or source.get("run_attempt") != 1
            or source.get("status") != "completed" or source.get("conclusion") != "startup_failure"):
        raise RecoveryUnavailable("Recovery event is not a first-attempt main startup failure")
    return source["id"]


def _validate_run(run, workflow_id=None):
    if (not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] <= 0
            or type(run.get("workflow_id")) is not int or run["workflow_id"] <= 0
            or (workflow_id is not None and run["workflow_id"] != workflow_id)
            or run.get("path") != WORKFLOW_PATH or run.get("head_branch") != "main"
            or not isinstance(run.get("repository"), dict)
            or run["repository"].get("full_name") != REPOSITORY
            or not isinstance(run.get("head_repository"), dict)
            or run["head_repository"].get("full_name") != REPOSITORY
            or run.get("event") not in SOURCE_EVENTS
            or type(run.get("run_attempt")) is not int or run["run_attempt"] < 1
            or run.get("status") not in ACTIVE | {"completed"}):
        raise RecoveryUnavailable("Automatic workflow run identity is not verified")
    return _time(run.get("created_at")), _time(run.get("updated_at"))


def recover(environ=None, *, event=None, api=None, now=None):
    env = os.environ if environ is None else environ
    _context(env)
    if event is None:
        try:
            with Path(env["GITHUB_EVENT_PATH"]).open("rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError()
            event = json.loads(raw)
        except (KeyError, OSError, ValueError):
            raise RecoveryUnavailable("Recovery event file unavailable") from None
    source_id = _event_source(event)
    api = api or GitHub(env.get("GITHUB_TOKEN", ""))
    now = now or datetime.now(timezone.utc)
    cutoff = now - WINDOW
    source = api.run(source_id)
    created, updated = _validate_run(source)
    if (source["id"] != source_id or source["run_attempt"] != 1
            or source["status"] != "completed" or source.get("conclusion") != "startup_failure"
            or updated > now + timedelta(seconds=30) or created > updated):
        raise RecoveryUnavailable("Source startup failure no longer matches")
    jobs = api.jobs(source_id)
    if type(jobs.get("total_count")) is not int or jobs["total_count"] != 0 or jobs.get("jobs") != []:
        raise RecoveryUnavailable("Recovery requires proof that no source job existed")
    page = api.recent_runs()
    runs, total = page.get("workflow_runs"), page.get("total_count")
    if (not isinstance(runs, list) or not runs or len(runs) > 100
            or type(total) is not int or total < len(runs)):
        raise RecoveryUnavailable("Recent automatic run sample unavailable")
    checked, ids = [], set()
    for run in runs:
        started, changed = _validate_run(run, source["workflow_id"])
        if run["id"] in ids or started > changed or changed > now + timedelta(seconds=30):
            raise RecoveryUnavailable("Recent automatic run sample is inconsistent")
        ids.add(run["id"])
        checked.append((run, started, changed))
    observed = next((run for run, _, _ in checked if run["id"] == source_id), None)
    if (observed is None or observed["run_attempt"] != 1 or observed["status"] != "completed"
            or observed.get("conclusion") != "startup_failure"):
        raise RecoveryUnavailable("Source startup failure not confirmed in recent runs")
    if any(run["id"] > source_id for run, _, _ in checked):
        return {"status": "newer_automatic_run_exists", "source_run_id": source_id}
    # On 11 November the predecessor remained in_progress after dispatching
    # a child that had already startup_failed. Blocking on that older parent
    # would lose recovery forever when its delayed teardown finally finished.
    # Queue one replacement behind it; the publisher's existing account-wide
    # concurrency lock still serializes execution. A newer run blocks above.
    if total > len(runs) and min(started for _, started, _ in checked) > cutoff:
        raise RecoveryUnavailable("Bounded run sample does not cover the recovery hour")
    failures = sum(run.get("conclusion") == "startup_failure" and changed >= cutoff
                   for run, _, changed in checked)
    if failures >= MAX_STARTUP_FAILURES:
        return {"status": "startup_failure_hourly_limit", "source_run_id": source_id,
                "startup_failures_last_hour": failures}
    # A timeout may already have created the fresh run. Never repeat this POST.
    api.dispatch()
    return {"status": "one_fresh_automatic_run_requested", "source_run_id": source_id,
            "workflow": WORKFLOW, "ref": "main", "startup_failures_last_hour": failures}


def main():
    try:
        result = recover()
    except RecoveryUnavailable as error:
        print("STARTUP_RECOVERY " + json.dumps({"status": "unavailable", "reason": str(error)}))
        return 1
    print("STARTUP_RECOVERY " + json.dumps(result, sort_keys=True))
    if result["status"] == "startup_failure_hourly_limit":
        print("::warning::Automatic startup recovery stopped after three startup failures in one hour.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

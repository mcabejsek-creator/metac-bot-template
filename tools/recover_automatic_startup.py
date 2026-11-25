"""Recover a jobless startup failure or a demonstrably stalled automatic chain.

This is one fresh workflow_dispatch, never a rerun or a publication retry.
It cannot recover an outage that also prevents GitHub Actions/webhook delivery.
Only the temporary GitHub token is used; no model or Metaculus access.
"""

import argparse
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
STALE_AFTER = timedelta(minutes=45)
WAKE_PATHS = {
    ".github/workflows/readonly_backlog_hourly.yaml",
    ".github/workflows/review_bot.yaml",
}
MAX_BYTES = 5_000_000
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
SOURCE_EVENTS = {"push", "schedule", "workflow_dispatch"}


class RecoveryUnavailable(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise RecoveryUnavailable("GitHub recovery redirect refused")


class GitHub:
    """Fixed GitHub GETs and at most one POST per recovery, no retry or redirect."""

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

    def workflow(self):
        return self._request("/actions/workflows/" + WORKFLOW)

    def active_runs(self, status):
        if status not in ACTIVE:
            raise RecoveryUnavailable("Unknown active run filter")
        return self._request("/actions/workflows/" + WORKFLOW
                             + "/runs?branch=main&status=" + status + "&per_page=1")

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


def _context(env, allowed_events=("workflow_run",)):
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REPOSITORY") != REPOSITORY
            or env.get("GITHUB_REF") != "refs/heads/main" or env.get("GITHUB_RUN_ATTEMPT") != "1"
            or env.get("GITHUB_EVENT_NAME") not in allowed_events
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



def _wake_event(env, event):
    # Scheduled checks are authorized by the fixed main workflow context.
    # A monitoring completion is only a wake-up, never publication evidence.
    if env["GITHUB_EVENT_NAME"] == "schedule":
        return
    if event is None:
        try:
            with Path(env["GITHUB_EVENT_PATH"]).open("rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError()
            event = json.loads(raw)
        except (KeyError, OSError, ValueError):
            raise RecoveryUnavailable("Recovery wake event unavailable") from None
    if (not isinstance(event, dict) or event.get("action") != "completed"
            or not isinstance(event.get("repository"), dict)
            or event["repository"].get("full_name") != REPOSITORY):
        raise RecoveryUnavailable("Untrusted recovery wake event")
    source = event.get("workflow_run")
    if (not isinstance(source, dict) or type(source.get("id")) is not int
            or source["id"] <= 0 or source.get("path") not in WAKE_PATHS
            or source.get("event") != "schedule" or source.get("head_branch") != "main"
            or type(source.get("run_attempt")) is not int or source["run_attempt"] != 1
            or source.get("status") != "completed"
            or source.get("conclusion") not in {"success", "failure"}
            or not isinstance(source.get("repository"), dict)
            or source["repository"].get("full_name") != REPOSITORY
            or not isinstance(source.get("head_repository"), dict)
            or source["head_repository"].get("full_name") != REPOSITORY):
        raise RecoveryUnavailable("Wake source is not a trusted scheduled monitor")


def _workflow_id(workflow):
    if (not isinstance(workflow, dict) or workflow.get("path") != WORKFLOW_PATH
            or type(workflow.get("id")) is not int or workflow["id"] <= 0
            or not isinstance(workflow.get("state"), str)):
        raise RecoveryUnavailable("Automatic workflow identity unavailable")
    return workflow["id"] if workflow["state"] == "active" else None


def _checked_stale_record(run, workflow_id, now):
    created, updated = _validate_run(run, workflow_id)
    if (created > updated or updated > now + timedelta(seconds=30)
            or not isinstance(run.get("head_sha"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", run["head_sha"])):
        raise RecoveryUnavailable("Invalid automatic run timestamps or revision")
    return created, updated


def _stale_candidate(page, workflow_id, now):
    if not isinstance(page, dict):
        raise RecoveryUnavailable("Recent automatic inventory unavailable")
    runs, total = page.get("workflow_runs"), page.get("total_count")
    if (not isinstance(runs, list) or type(total) is not int or total < 0
            or len(runs) != min(total, 100)):
        raise RecoveryUnavailable("Recent automatic inventory incomplete")
    if not runs:
        return None, "no_previous_automatic_run"
    checked, ids = [], set()
    for run in runs:
        created, updated = _checked_stale_record(run, workflow_id, now)
        if run["id"] in ids:
            raise RecoveryUnavailable("Duplicate automatic run")
        ids.add(run["id"])
        checked.append((run, created, updated))
    if any(run["status"] in ACTIVE for run, _, _ in checked):
        return None, "automatic_run_active"
    latest, created, updated = max(checked, key=lambda item: (item[1], item[0]["id"]))
    if any(run["id"] > latest["id"] for run, _, _ in checked):
        raise RecoveryUnavailable("Latest automatic run identity ambiguous")
    if now - max(changed for _, _, changed in checked) < STALE_AFTER:
        return None, "automatic_run_recent"
    if (latest["run_attempt"] != 1 or latest.get("conclusion")
            not in {"success", "failure", "startup_failure"}):
        return None, "latest_automatic_run_not_recoverable"
    if latest.get("conclusion") == "startup_failure":
        cutoff = now - WINDOW
        if total > len(runs) and min(start for _, start, _ in checked) > cutoff:
            raise RecoveryUnavailable("Run sample does not cover startup failure window")
        if sum(run.get("conclusion") == "startup_failure" and changed >= cutoff
               for run, _, changed in checked) >= MAX_STARTUP_FAILURES:
            return None, "startup_failure_hourly_limit"
    return latest, "stale_automatic_run"


def _same_run(a, b):
    return all(a.get(key) == b.get(key) for key in
               ("id", "workflow_id", "head_sha", "run_attempt", "event",
                "status", "conclusion", "created_at", "updated_at"))


def _safe_finished_job(source, envelope):
    if not isinstance(envelope, dict):
        raise RecoveryUnavailable("Automatic job evidence unavailable")
    count, jobs = envelope.get("total_count"), envelope.get("jobs")
    if type(count) is not int or not isinstance(jobs, list):
        raise RecoveryUnavailable("Automatic job inventory malformed")
    if source["conclusion"] == "startup_failure":
        return count == 0 and jobs == []
    if count != 1 or len(jobs) != 1:
        return False
    job = jobs[0]
    if (not isinstance(job, dict) or type(job.get("run_id")) is not int
            or job["run_id"] != source["id"] or job.get("name") != "forecast_job"
            or job.get("status") != "completed"
            or job.get("conclusion") != source["conclusion"]):
        return False
    steps = job.get("steps")
    if not isinstance(steps, list):
        return False
    expected = {"Offline publication regressions": "success", "Run bot": "success",
                "Queue next automatic pass": source["conclusion"]}
    numbers = []
    for name, conclusion in expected.items():
        matching = [step for step in steps if isinstance(step, dict) and step.get("name") == name]
        if (len(matching) != 1 or matching[0].get("status") != "completed"
                or matching[0].get("conclusion") != conclusion
                or type(matching[0].get("number")) is not int or matching[0]["number"] <= 0):
            return False
        numbers.append(matching[0]["number"])
    return numbers == sorted(set(numbers))


def recover_stale(environ=None, *, event=None, api=None, now=None):
    """At most eleven fixed GETs, one fresh dispatch, no in-check retry.

    Repeated wakeups are fresh observations, not replay of a model or
    publication operation. The existing production lock/journal and budget
    still apply. GitHub's reads are not linearizable: we cannot promise a
    unique dispatch relative to native cron or across an API outage.
    """
    env = os.environ if environ is None else environ
    _context(env, ("schedule", "workflow_run"))
    if any(env.get(key) for key in ("METACULUS_TOKEN", "METACULUS_TOKEN_V2",
           "PERPLEXITY_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
           "VRCHCAB_OPENAI_TEST_KEY")):
        raise RecoveryUnavailable("Recovery credential isolation failed")
    _wake_event(env, event)
    api = api or GitHub(env.get("GITHUB_TOKEN", ""))
    clock = (lambda: datetime.now(timezone.utc)) if now is None else (lambda: now)
    workflow_id = _workflow_id(api.workflow())
    if workflow_id is None:
        return {"status": "automatic_workflow_not_active"}
    source, reason = _stale_candidate(api.recent_runs(), workflow_id, clock())
    if source is None:
        return {"status": reason}
    direct = api.run(source["id"])
    _checked_stale_record(direct, workflow_id, clock())
    if not _same_run(source, direct):
        return {"status": "automatic_run_changed"}
    # Do not automatically recover from a failed model/publication or offline
    # guard step. A successful inventory with a failed successor is safe to
    # replace with a new normal pass; no old run or artifact is replayed.
    if not _safe_finished_job(source, api.jobs(source["id"])):
        return {"status": "latest_job_not_safe_for_recovery"}
    # Check even older active runs outside the bounded recent-history page.
    for state in sorted(ACTIVE):
        page = api.active_runs(state)
        if not isinstance(page, dict):
            raise RecoveryUnavailable("Active run inventory unavailable")
        total, runs = page.get("total_count"), page.get("workflow_runs")
        if (type(total) is not int or total < 0 or not isinstance(runs, list)
                or len(runs) != min(total, 1)):
            raise RecoveryUnavailable("Active run inventory malformed")
        if total:
            return {"status": "automatic_run_active"}
    final, reason = _stale_candidate(api.recent_runs(), workflow_id, clock())
    if final is None:
        return {"status": reason}
    if not _same_run(source, final):
        return {"status": "automatic_run_changed"}
    if _workflow_id(api.workflow()) != workflow_id:
        return {"status": "automatic_workflow_changed"}
    # Single new dispatch; an ambiguous result ends this check immediately.
    api.dispatch()
    return {"status": "one_fresh_automatic_run_requested",
            "source_run_id": source["id"], "workflow": WORKFLOW, "ref": "main"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stale", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = recover_stale() if args.stale else recover()
    except RecoveryUnavailable as error:
        print("STARTUP_RECOVERY " + json.dumps({"status": "unavailable", "reason": str(error)}))
        return 1
    print("STARTUP_RECOVERY " + json.dumps(result, sort_keys=True))
    if result["status"] == "startup_failure_hourly_limit":
        print("::warning::Automatic startup recovery stopped after three startup failures in one hour.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

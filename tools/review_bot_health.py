"""Daily bounded GitHub GET-only health snapshot; never dispatches or forecasts."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import urllib.error
import urllib.request


REPOSITORY = "mcabejsek-creator/metac-bot-template"
WORKFLOW_PATH = ".github/workflows/run_bot_automatic.yaml"
REVIEW_PATH = ".github/workflows/review_bot.yaml"
ROOT = "https://api.github.com/repos/" + REPOSITORY
WORKFLOW_URL = ROOT + "/actions/workflows/run_bot_automatic.yaml"
RUNS_URL = WORKFLOW_URL + "/runs?branch=main&per_page=100&page=1"
MAX_GETS = 2
MAX_BYTES = 5_000_000
ACTIVE_STATES = {"in_progress", "queued", "requested", "waiting", "pending"}
FAILED_CONCLUSIONS = {"failure", "timed_out", "action_required", "startup_failure", "stale"}


class HealthUnavailable(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise HealthUnavailable("GitHub health redirect refused")


class HealthReader:
    def __init__(self, token, *, opener=None):
        if not isinstance(token, str) or not token or token.strip() != token:
            raise HealthUnavailable("GitHub read credential unavailable")
        self._token = token
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self.attempts = 0

    def get(self, url):
        if url not in {WORKFLOW_URL, RUNS_URL} or self.attempts >= MAX_GETS:
            raise HealthUnavailable("GitHub health GET boundary exceeded")
        self.attempts += 1
        request = urllib.request.Request(url, method="GET", headers={
            "Authorization": "Bearer " + self._token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with self._opener.open(request, timeout=20) as response:
                if response.status != 200:
                    raise HealthUnavailable("GitHub health response unavailable")
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise HealthUnavailable("GitHub health response exceeds size limit")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise HealthUnavailable("GitHub health response must be an object")
                return data
        except (urllib.error.URLError, OSError, ValueError):
            raise HealthUnavailable("GitHub health GET failed; no retry") from None


def moment(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise HealthUnavailable("GitHub health timestamp unavailable") from None


def summarize(workflow, envelope, *, now):
    if (workflow.get("path") != WORKFLOW_PATH or type(workflow.get("id")) is not int
            or workflow["id"] <= 0 or not isinstance(workflow.get("state"), str)):
        raise HealthUnavailable("Automatic workflow identity unavailable")
    runs, total = envelope.get("workflow_runs"), envelope.get("total_count")
    if (not isinstance(runs, list) or len(runs) > 100
            or type(total) is not int or total < len(runs)):
        raise HealthUnavailable("GitHub health run sample malformed")
    checked, ids = [], set()
    for run in runs:
        if (not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] <= 0
                or run["id"] in ids or run.get("workflow_id") != workflow["id"]
                or run.get("path") != WORKFLOW_PATH or run.get("head_branch") != "main"
                or not isinstance(run.get("repository"), dict)
                or run["repository"].get("full_name") != REPOSITORY
                or not isinstance(run.get("status"), str)
                or run.get("status") not in ACTIVE_STATES | {"completed"}):
            raise HealthUnavailable("GitHub health sample contains an invalid run")
        created = moment(run.get("created_at"))
        updated = moment(run.get("updated_at"))
        conclusion = run.get("conclusion")
        if conclusion is not None and not isinstance(conclusion, str):
            raise HealthUnavailable("GitHub health completion outcome malformed")
        actually_started = (run["status"] == "in_progress" or
                            run["status"] == "completed" and conclusion not in {"cancelled", "skipped"})
        started = moment(run.get("run_started_at")) if actually_started else created
        if max(created, updated, started) > now + timedelta(minutes=2):
            raise HealthUnavailable("GitHub health run timestamp is in the future")
        if (run["status"] == "completed"
                and conclusion not in FAILED_CONCLUSIONS | {"success", "cancelled", "skipped", "neutral"}):
            raise HealthUnavailable("GitHub health completion outcome unavailable")
        ids.add(run["id"])
        checked.append({"id": run["id"], "status": run["status"], "conclusion": conclusion,
                        "created": created, "updated": updated, "started": started,
                        "actually_started": actually_started})
    checked.sort(key=lambda item: item["created"], reverse=True)
    started_runs = [item for item in checked if item["actually_started"]]
    successes = [item for item in checked if item["conclusion"] == "success"]
    meaningful_completed = [item for item in checked if item["status"] == "completed"
                            and item["conclusion"] not in {"cancelled", "skipped"}]
    last_started = max(started_runs, key=lambda item: item["started"], default=None)
    last_success = max(successes, key=lambda item: item["updated"], default=None)
    last_completed = max(meaningful_completed, key=lambda item: item["updated"], default=None)
    warnings = []
    if workflow["state"] != "active":
        warnings.append("automatic_workflow_not_active")
    if last_started is None or now - last_started["started"] > timedelta(minutes=90):
        warnings.append("no_started_run_within_90_minutes")
    if last_success is None or now - last_success["updated"] > timedelta(hours=3):
        warnings.append("no_successful_run_within_3_hours_in_sample")
    if last_completed is not None and last_completed["conclusion"] in FAILED_CONCLUSIONS:
        warnings.append("latest_completed_run_failed")
    if any(item["status"] == "in_progress" and now - item["started"] > timedelta(minutes=75)
           for item in checked):
        warnings.append("run_in_progress_over_75_minutes")

    def public_run(item):
        if item is None:
            return None
        return {"run_id": item["id"], "status": item["status"], "conclusion": item["conclusion"],
                "started_at": item["started"].isoformat(), "updated_at": item["updated"].isoformat()}

    return {
        "mode": "automatic_workflow_health_get_only",
        "checked_at_utc": now.isoformat(), "status": "warning" if warnings else "healthy",
        "workflow_state": workflow["state"], "warnings": warnings,
        "last_started_run": public_run(last_started), "last_successful_run": public_run(last_success),
        "sample": {"limit": 100, "observed": len(checked), "total_available": total,
                   "truncated": total > len(checked)},
        "sample_counts": {"successful": len(successes),
            "failed": sum(item["conclusion"] in FAILED_CONCLUSIONS for item in checked),
            "cancelled": sum(item["conclusion"] == "cancelled" for item in checked),
            "in_progress": sum(item["status"] == "in_progress" for item in checked),
            "queued_or_waiting": sum(item["status"] in ACTIVE_STATES - {"in_progress"} for item in checked)},
        "limitation": "A successful workflow does not prove a new forecast or forecast accuracy. Counts describe only the latest bounded run sample.",
    }


def review(environ=None, *, opener=None, now=None):
    env = os.environ if environ is None else environ
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REPOSITORY") != REPOSITORY
            or env.get("GITHUB_REF") != "refs/heads/main" or env.get("GITHUB_RUN_ATTEMPT") != "1"
            or env.get("GITHUB_EVENT_NAME") not in {"push", "schedule", "workflow_dispatch"}
            or env.get("GITHUB_WORKFLOW_REF") != REPOSITORY + "/" + REVIEW_PATH + "@refs/heads/main"):
        raise HealthUnavailable("Health reads require this review workflow's first main-branch run")
    reader = HealthReader(env.get("GITHUB_TOKEN"), opener=opener)
    result = summarize(reader.get(WORKFLOW_URL), reader.get(RUNS_URL),
                       now=now or datetime.now(timezone.utc))
    result["github_get_attempts"] = reader.attempts
    return result


def markdown(result):
    lines = ["## Automatic forecast operation", "", "Status: **" + result["status"] + "**", ""]
    for key in ("workflow_state", "warnings", "sample", "sample_counts", "last_started_run", "last_successful_run"):
        if key in result:
            lines.append("- " + key.replace("_", " ") + ": " + json.dumps(result[key], ensure_ascii=False))
    lines.extend(["", result.get("limitation", "Health evidence unavailable; no corrective action was taken."), ""])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("bot-health.json"))
    parser.add_argument("--summary", type=Path, default=Path("bot-health.md"))
    args = parser.parse_args(argv)
    try:
        result = review()
    except HealthUnavailable as error:
        result = {"mode": "automatic_workflow_health_get_only", "status": "unavailable", "reason": str(error)}
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    args.summary.write_text(markdown(result), encoding="utf-8")
    print("BOT_HEALTH_SUMMARY " + json.dumps(result, sort_keys=True))
    if result["status"] != "healthy":
        print("::warning::Automatic forecast health needs attention; see the bounded review summary.")
    return 0 if result["status"] == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())

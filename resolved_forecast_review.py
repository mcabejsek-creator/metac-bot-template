"""Read-only evaluation of Vrchcab's actual resolved forecasts; no model calls.

Uses authenticated own history and official score_data, rather than rerunning a
model on known outcomes. Calibration uses the forecast active at the official
spot_scoring_time; latest forecasts are never silently substituted for it.
Only aggregate metrics are printed. The optional JSON contains resolved-only
numeric records, without question text, comments, research, or credentials.
"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request


EXPECTED_USER_ID = 307558
EXPECTED_USERNAME = "Vrchcab"
API_BASE = "https://www.metaculus.com/api"
VERSION_BOUNDARY = datetime(2026, 11, 10, tzinfo=timezone.utc)
PAGE_SIZE, MAX_PAGES = 100, 10
MAX_RESPONSE_BYTES, GET_TIMEOUT = 15_000_000, 18
GET_SPACING_SECONDS = 6
SCORE_FIELDS = ("peer_score", "spot_peer_score", "baseline_score", "spot_baseline_score")
SHRINKAGE_GRID = (1.0, 0.9, 0.75, 0.5)
MIN_TRAIN, MIN_TEST = 20, 10


class ReviewUnavailable(RuntimeError):
    """No credentials, raw provider payloads, or private texts in error messages."""


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _time(value):
    try:
        if _number(value):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str):
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is not None and stamp.utcoffset() is not None:
                return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z") if value else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ReviewUnavailable("Metaculus review redirect rejected")


class ResolvedReviewReader:
    """At most one identity GET and ten bounded pages, without retry."""

    def __init__(self, token, *, opener=None, clock=time.monotonic, sleep=time.sleep):
        if not isinstance(token, str) or not token or any(ch.isspace() for ch in token):
            raise ReviewUnavailable("A valid Metaculus V2 token is required")
        self._token = token
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self._clock, self._sleep, self._last = clock, sleep, None
        self.get_attempts = 0

    def _get(self, path, params=None):
        if self.get_attempts >= MAX_PAGES + 1:
            raise ReviewUnavailable("Read-only review request bound reached")
        if self._last is not None:
            delay = GET_SPACING_SECONDS - (self._clock() - self._last)
            if delay > 0:
                self._sleep(delay)
        self._last = self._clock()
        self.get_attempts += 1
        url = API_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(url, method="GET", headers={
            "Authorization": "Token " + self._token, "Accept": "application/json",
            "Cache-Control": "no-cache", "User-Agent": "vrchcab-resolved-review-readonly"})
        try:
            with self._opener.open(request, timeout=GET_TIMEOUT) as response:
                if response.status != 200:
                    raise ReviewUnavailable("Metaculus review GET was not successful")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ReviewUnavailable("Metaculus review response is too large")
            return json.loads(raw)
        except ReviewUnavailable:
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError, UnicodeError):
            raise ReviewUnavailable("Metaculus review GET or JSON failed; no automatic retry") from None

    def read(self):
        user = self._get("/users/me/")
        if (not isinstance(user, dict) or type(user.get("id")) is not int
                or user["id"] != EXPECTED_USER_ID or user.get("username") != EXPECTED_USERNAME):
            raise ReviewUnavailable("The review token does not identify Vrchcab")
        posts, empty_pages = [], 0
        for page in range(MAX_PAGES):
            data = self._get("/posts/", {"statuses": "resolved", "forecaster_id": EXPECTED_USER_ID,
                "limit": PAGE_SIZE, "offset": page * PAGE_SIZE, "order_by": "-published_at",
                "with_cp": "true", "include_descriptions": "false"})
            if (not isinstance(data, dict) or "next" not in data
                    or (data["next"] is not None and not isinstance(data["next"], str))):
                raise ReviewUnavailable("Malformed resolved inventory pagination")
            rows = data.get("results")
            if not isinstance(rows, list) or len(rows) > PAGE_SIZE or any(not isinstance(row, dict) for row in rows):
                raise ReviewUnavailable("Malformed resolved inventory page")
            posts.extend(rows)
            empty_pages = empty_pages + 1 if not rows else 0
            # The API can keep advertising next after the end (count=infinity).
            if data["next"] is None or empty_pages >= 2:
                return posts, True
        return posts, False


def _leaves(post):
    if post.get("notebook") is not None:
        return []
    if isinstance(post.get("question"), dict):
        return [post["question"]]
    group = post.get("group_of_questions")
    if isinstance(group, dict) and isinstance(group.get("questions"), list):
        return group["questions"]
    conditional = post.get("conditional")
    if isinstance(conditional, dict):
        return [conditional[k] for k in ("question_yes", "question_no") if conditional.get(k) is not None]
    return []


def _forecast_history(question, user_id):
    own = question.get("my_forecasts")
    history = own.get("history") if isinstance(own, dict) else None
    if not isinstance(history, list):
        raise ReviewUnavailable("missing_own_history")
    out = []
    for forecast in history:
        if not isinstance(forecast, dict):
            raise ReviewUnavailable("malformed_own_history")
        if forecast.get("question_id") is not None and (
                type(forecast["question_id"]) is not int or forecast["question_id"] != question["id"]):
            raise ReviewUnavailable("cross_question_history")
        if forecast.get("author_id") is not None and (
                type(forecast["author_id"]) is not int or forecast["author_id"] != user_id):
            raise ReviewUnavailable("other_user_history")
        start, end = _time(forecast.get("start_time")), _time(forecast.get("end_time"))
        if start is None or (forecast.get("end_time") is not None and end is None) or (end and end <= start):
            raise ReviewUnavailable("malformed_forecast_time")
        out.append((start, end, forecast))
    out.sort(key=lambda row: row[0])
    if len({row[0] for row in out}) != len(out):
        raise ReviewUnavailable("ambiguous_equal_time_forecasts")
    return own, out


def _binary_probability(forecast):
    values = forecast.get("forecast_values")
    if not isinstance(values, list) or len(values) not in (1, 2):
        return None
    if not all(_number(value) and 0 <= value <= 1 for value in values):
        return None
    if len(values) == 2 and abs(sum(values) - 1) > 1e-6:
        return None
    return float(values[-1])


def extract_records(posts, *, now=None):
    """Return resolved-only numeric evidence; exclude malformed/unknown history."""
    now = now or datetime.now(timezone.utc)
    records, skipped = {}, Counter()
    for post in posts:
        post_id = post.get("id")
        if type(post_id) is not int or post_id <= 0:
            skipped["invalid_post_id"] += 1
            continue
        projects = post.get("projects")
        project = (projects.get("default_project") or {}) if isinstance(projects, dict) else {}
        project_id = project.get("id") if isinstance(project, dict) else None
        for question in _leaves(post):
            if not isinstance(question, dict) or type(question.get("id")) is not int or question["id"] <= 0:
                skipped["invalid_question_id"] += 1
                continue
            if question.get("status", post.get("status")) != "resolved":
                skipped["not_resolved"] += 1
                continue
            resolution = question.get("resolution")
            resolved_at = _time(question.get("actual_resolve_time"))
            if resolution in (None, "", "annulled", "ambiguous") or resolved_at is None or resolved_at > now:
                skipped["resolution_unavailable_or_unscored"] += 1
                continue
            try:
                own, history = _forecast_history(question, EXPECTED_USER_ID)
            except ReviewUnavailable as exc:
                skipped[str(exc)] += 1
                continue
            if not history:
                skipped["not_forecasted_by_vrchcab"] += 1
                continue
            spot = _time(question.get("spot_scoring_time"))
            close = _time(question.get("actual_close_time")) or _time(question.get("scheduled_close_time"))
            # Metaculus scoring/score_math.py clips spot time to actual close.
            effective_spot = min(spot, close) if spot and close else spot
            final_bound = min(t for t in (close, resolved_at) if t is not None)
            before_outcome = [row for row in history if row[0] <= final_bound and row[0] < resolved_at]
            if not before_outcome:
                skipped["no_forecast_before_resolution"] += 1
                continue
            selected, basis = before_outcome[-1], "last_before_close_for_cohort_only"
            exclusion = "missing_official_spot_time"
            if effective_spot is not None:
                if effective_spot >= resolved_at:
                    exclusion = "spot_not_before_outcome"
                else:
                    active = [row for row in history if row[0] <= effective_spot and (row[1] is None or row[1] > effective_spot)]
                    if len(active) == 1:
                        selected, basis, exclusion = active[0], "official_spot_time", None
                    else:
                        exclusion = "no_unique_forecast_active_at_spot"
            start, _, forecast = selected
            cohort = "before_2026_11_10" if start < VERSION_BOUNDARY else "from_2026_11_10"
            score_data = own.get("score_data")
            score_data = score_data if isinstance(score_data, dict) else {}
            scores = {key: float(score_data[key]) for key in SCORE_FIELDS if _number(score_data.get(key))}
            probability, outcome = None, None
            if question.get("type") == "binary" and resolution in ("yes", "no") and exclusion is None:
                probability = _binary_probability(forecast)
                if probability is None:
                    exclusion = "missing_or_invalid_spot_probability"
                else:
                    outcome = int(resolution == "yes")
            elif exclusion is None:
                exclusion = "nonbinary_calibration_not_implemented"
            row = {"post_id": post_id, "question_id": question["id"], "question_type": question.get("type"),
                "project_id": project_id if type(project_id) is int else None,
                "forecast_at": _iso(start), "resolved_at": _iso(resolved_at), "spot_scoring_time": _iso(spot),
                "effective_spot_scoring_time": _iso(effective_spot),
                "forecast_basis": basis, "cohort": cohort, "official_scores": scores,
                "probability_yes": probability, "outcome_yes": outcome, "calibration_excluded": exclusion}
            previous = records.get(question["id"])
            if previous is not None and previous != row:
                raise ReviewUnavailable("Conflicting resolved records across inventory pages")
            records[question["id"]] = row
    return sorted(records.values(), key=lambda row: (row["forecast_at"], row["question_id"])), dict(skipped)


def binary_metrics(rows, alpha=1.0):
    if not rows:
        return {"count": 0, "brier": None, "log_loss": None}
    probabilities = [0.5 + alpha * (row["probability_yes"] - 0.5) for row in rows]
    brier = math.fsum((p - row["outcome_yes"]) ** 2 for p, row in zip(probabilities, rows)) / len(rows)
    loss = math.fsum(-math.log(max(1e-12, p if row["outcome_yes"] else 1 - p))
                    for p, row in zip(probabilities, rows)) / len(rows)
    return {"count": len(rows), "brier": round(brier, 8), "log_loss": round(loss, 8),
            "mean_probability_yes": round(math.fsum(probabilities) / len(rows), 6),
            "observed_yes_fraction": round(math.fsum(row["outcome_yes"] for row in rows) / len(rows), 6)}


def calibration_experiment(rows, *, inventory_complete):
    """Chronological holdout; training outcomes must precede the first test forecast."""
    rows = sorted((row for row in rows if row["probability_yes"] is not None),
                  key=lambda row: (row["forecast_at"], row["question_id"]))
    result = {"production_calibration_changed": False, "exploratory_only": True,
              "method": "p_new=0.5+alpha*(p_original-0.5)", "alpha_grid": list(SHRINKAGE_GRID),
              "available_binary_spot_forecasts": len(rows)}
    if not inventory_complete:
        return {**result, "status": "withheld_incomplete_inventory"}
    if len(rows) < MIN_TRAIN + MIN_TEST:
        return {**result, "status": "insufficient_resolved_binary_forecasts"}
    cutoff = rows[int(len(rows) * 0.7)]["forecast_at"]
    test = [row for row in rows if row["forecast_at"] >= cutoff]
    test_posts = {row["post_id"] for row in test}
    earlier = [row for row in rows if row["forecast_at"] < cutoff]
    train = [row for row in earlier if row["resolved_at"] < cutoff and row["post_id"] not in test_posts]
    result.update({"test_begins_at": cutoff, "train_count": len(train), "test_count": len(test),
                   "training_excluded_for_outcome_timing_or_shared_post": len(earlier) - len(train)})
    if len(train) < MIN_TRAIN or len(test) < MIN_TEST:
        return {**result, "status": "insufficient_chronologically_available_training_data"}
    # Ties prefer unchanged probabilities. Test outcomes never select alpha.
    alpha = min(SHRINKAGE_GRID, key=lambda a: (binary_metrics(train, a)["log_loss"], -a))
    original, candidate = binary_metrics(test), binary_metrics(test, alpha)
    improved = (alpha != 1 and candidate["log_loss"] < original["log_loss"]
                and candidate["brier"] < original["brier"])
    return {**result, "selected_alpha_on_training_only": alpha,
            "holdout_original": original, "holdout_candidate": candidate,
            "status": "candidate_improved_on_holdout" if improved else "no_holdout_improvement",
            "deployment_decision": "not_deployed"}


def _aggregate(rows):
    scores = {}
    for key in SCORE_FIELDS:
        values = [row["official_scores"][key] for row in rows if key in row["official_scores"]]
        scores[key] = {"count": len(values), "sum": round(math.fsum(values), 6),
                       "mean": round(math.fsum(values) / len(values), 6) if values else None}
    binary = [row for row in rows if row["probability_yes"] is not None]
    bins = []
    for index in range(5):
        lower = index / 5
        selected = [row for row in binary if min(4, int(row["probability_yes"] * 5)) == index]
        if selected:
            bins.append({"range": [lower, round(lower + 0.2, 1)], **binary_metrics(selected)})
    return {"resolved_forecasted_questions": len(rows), "official_scores": scores,
            "binary_spot_metrics": binary_metrics(binary), "binary_reliability_bins": bins,
            "calibration_exclusions": dict(Counter(row["calibration_excluded"] for row in rows if row["calibration_excluded"]))}


def build_report(posts, *, inventory_complete=True, get_attempts=0, now=None):
    now = now or datetime.now(timezone.utc)
    records, skipped = extract_records(posts, now=now)
    cohorts = {}
    for label in ("before_2026_11_10", "from_2026_11_10"):
        subset = [row for row in records if row["cohort"] == label]
        cohorts[label] = {**_aggregate(subset),
                         "calibration_experiment": calibration_experiment(subset, inventory_complete=inventory_complete)}
    by_project = defaultdict(list)
    for row in records:
        by_project[str(row["project_id"])].append(row)
    summary = {"mode": "resolved_own_forecasts_readonly", "generated_at": _iso(now),
        "user_id": EXPECTED_USER_ID, "username": EXPECTED_USERNAME,
        "inventory_complete_within_bound": inventory_complete, "authenticated_get_attempts": get_attempts,
        "paid_requests": 0, "publication_requests": 0, "production_calibration_changed": False,
        "version_boundary_utc": _iso(VERSION_BOUNDARY),
        "version_boundary_is_date_proxy_not_code_attestation": True,
        "metrics_note": "Binary metrics use actual forecasts active at official spot time. Official score totals describe retrieved records, not necessarily an entire leaderboard.",
        "all_periods": _aggregate(records), "cohorts": cohorts,
        "by_project": {project: _aggregate(rows) for project, rows in sorted(by_project.items())},
        "excluded_records": skipped}
    return {"schema_version": 1, "summary": summary, "resolved_records": records}


def markdown_summary(summary):
    lines = ["# Vrchcab — resolved forecast review", "", "Read-only; no paid calls, forecasts, or calibration deployment.", "",
             f"Generated: {summary['generated_at']}",
             f"Inventory complete within request bound: {summary['inventory_complete_within_bound']}", "",
             "| Forecast period | Resolved questions | Official spot-peer count | Spot-peer sum | Binary spot count | Brier ↓ | Log loss ↓ |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for label, item in summary["cohorts"].items():
        score, binary = item["official_scores"]["spot_peer_score"], item["binary_spot_metrics"]
        lines.append(f"| {label} | {item['resolved_forecasted_questions']} | {score['count']} | {score['sum']} | {binary['count']} | {binary['brier']} | {binary['log_loss']} |")
    lines += ["", "The 10 November boundary describes forecast dates, not verified code versions. Earlier results do not measure the new research path.",
              "Official score totals cover retrieved own records. Binary Brier/log-loss are unweighted diagnostics, not the official tournament score.", "",
              "## Calibration experiment", ""]
    for label, item in summary["cohorts"].items():
        experiment = item["calibration_experiment"]
        lines.append(f"- {label}: {experiment['status']}; selected alpha: {experiment.get('selected_alpha_on_training_only', 'not selected')}; production unchanged.")
    lines += ["", "The holdout is chronological. Training uses only outcomes available before the first held-out forecast and excludes posts shared with the test set. Results are exploratory; no automatic activation."]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="resolved-review.json")
    parser.add_argument("--summary", default="resolved-review.md")
    args = parser.parse_args()
    try:
        reader = ResolvedReviewReader(os.environ.get("METACULUS_TOKEN", ""))
        posts, complete = reader.read()
        report = build_report(posts, inventory_complete=complete, get_attempts=reader.get_attempts)
        text = markdown_summary(report["summary"])
        Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        Path(args.summary).write_text(text, encoding="utf-8")
        print("RESOLVED_REVIEW_SUMMARY " + json.dumps(report["summary"], sort_keys=True, separators=(",", ":")))
        print(text)
    except ReviewUnavailable as exc:
        print("RESOLVED_REVIEW_SUMMARY " + json.dumps({"status": "unavailable", "reason": str(exc),
            "paid_requests": 0, "publication_requests": 0, "production_calibration_changed": False}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

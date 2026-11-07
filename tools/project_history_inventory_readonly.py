"""Prepared (NOT scheduled) project-scoped Vrchcab forecast inventory.

The documented Metaculus GET /api/questions/bulk-forecast-read/ accepts
?project=<id-or-slug>&acting_user=<bot-id>, which avoids a full /posts/ crawl.
This tool only READS. It cannot publish or request an LLM. Raw forecasts and
question IDs are never saved or printed. Running against Metaculus requires
a separate explicit approval and --run-two-project-gets.

This is *not* proof of full-history coverage or reconciliation of uncertain
previous POSTs. Projects omit any other competitions and inaccessible posts.
"""
import argparse
from datetime import datetime, timezone
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = "https://www.metaculus.com"
ENDPOINT = "/api/questions/bulk-forecast-read/"
BOT_ID = 307558
PROJECTS = (
    ("Fall FutureEval 2026", "33121"),
    ("MiniBench", "minibench"),
)
MAX_REQUESTS = 2
MAX_RESPONSE_BYTES = 15_000_000
REQUEST_SPACING_SECONDS = 6.0


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise RuntimeError("Redirect refused; token not forwarded")


def build_request(project, token):
    token = token.strip()
    if token.startswith("Token "):
        token = token[6:].strip()
    if len(token) < 32:
        raise RuntimeError("Missing or malformed Metaculus token; zero requests sent")
    if project not in {key for _, key in PROJECTS}:
        raise RuntimeError("Unexpected project; request refused")
    query = urllib.parse.urlencode({"acting_user": BOT_ID, "project": project})
    req = urllib.request.Request(
        HOST + ENDPOINT + "?" + query,
        headers={
            "Authorization": "Token " + token,
            "Accept": "application/json",
            "User-Agent": "Vrchcab-readonly-aggregate-audit/1",
        },
        method="GET",
    )
    assert req.get_method() == "GET"
    return req


def as_timestamp(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RuntimeError("Invalid/missing forecast timestamp; refusing summary")
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        raise RuntimeError("Out-of-range timestamp; refusing summary") from None


def summarize_response(data):
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise RuntimeError("Unexpected forecast inventory API shape")
    if data.get("next") is not None:
        raise RuntimeError("Pagination indicated; incomplete inventory")
    seen = set()
    total = 0
    active = 0
    observed_dates = []
    for item in data["results"]:
        if not isinstance(item, dict):
            raise RuntimeError("Malformed inventory row")
        question_id = item.get("question_id")
        if not isinstance(question_id, int) or isinstance(question_id, bool) or question_id <= 0 or question_id in seen:
            raise RuntimeError("Duplicate/invalid question ID in API response")
        seen.add(question_id)  # Ephemeral memory only; never saved or logged.
        forecasts = item.get("forecasts")
        if not isinstance(forecasts, list):
            raise RuntimeError("Missing forecast array; no report")
        if forecasts:
            active += 1
        for forecast in forecasts:
            if not isinstance(forecast, dict):
                raise RuntimeError("Malformed forecast record")
            if forecast.get("author_id") != BOT_ID or forecast.get("question_id") != question_id:
                raise RuntimeError("Forecast author/question mismatch; no report")
            observed_dates.append(as_timestamp(forecast.get("start_time")))
            total += 1
    return {
        "questions_returned": len(seen),
        "questions_with_history": active,
        "forecast_records": total,
        "earliest_record_utc": min(observed_dates) if observed_dates else None,
        "latest_record_utc": max(observed_dates) if observed_dates else None,
        "coverage": "Only this project, as visible to the acting user; not comprehensive",
        "publication_reconciled": False,
        "contains_question_ids_or_forecast_values": False,
    }


def fetch_one(opener, request):
    try:
        with opener.open(request, timeout=25) as response:
            if response.status != 200:
                raise RuntimeError("Unexpected HTTP response; stopped")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError("Response exceeds conservative size cap")
            return summarize_response(json.loads(raw))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError("HTTP 429: stop immediately, respect server cooldown") from None
        raise RuntimeError(f"HTTP {exc.code}: stop without retry") from None
    except urllib.error.URLError:
        raise RuntimeError("API network/TLS error; stop without retry") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Invalid JSON response; stop without retry") from None


def run_live():
    token = os.environ.get("METACULUS_TOKEN", "")
    if len(token.strip()) < 32:
        raise RuntimeError("Missing METACULUS_TOKEN; zero requests sent")
    opener = urllib.request.build_opener(NoRedirect())
    summaries = []
    last = None
    attempts = 0
    for label, project in PROJECTS:
        request = build_request(project, token)
        if last is not None:
            wait = REQUEST_SPACING_SECONDS - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        last = time.monotonic()
        attempts += 1
        if attempts > MAX_REQUESTS:
            raise RuntimeError("Request budget reached")
        summaries.append({"project": label, **fetch_one(opener, request)})
    print(json.dumps({
        "audit": "scoped_read_only_aggregate_only",
        "acting_user_id": BOT_ID,
        "requests_sent": attempts,
        "project_summaries": summaries,
        "reconciled": False,
        "note": "Never treats zero rows as proof of no historic forecasts; project-scoped only.",
    }, ensure_ascii=False))


def self_check():
    reqs = [build_request(project, "a" * 48) for _, project in PROJECTS]
    for req, (_, project) in zip(reqs, PROJECTS):
        parsed = urllib.parse.urlsplit(req.full_url)
        params = urllib.parse.parse_qs(parsed.query)
        assert parsed.scheme == "https" and parsed.netloc == "www.metaculus.com"
        assert parsed.path == ENDPOINT
        assert params == {"acting_user": [str(BOT_ID)], "project": [project]}
        assert req.get_method() == "GET" and req.has_header("Authorization")
    print("Offline self-check passed: two authorized GET shapes, no network attempted.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-check", action="store_true")
    mode.add_argument("--run-two-project-gets", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_check:
            self_check()
        else:
            run_live()
    except RuntimeError as exc:
        print("Inventory stopped safely: " + str(exc), file=sys.stderr)
        sys.exit(1)

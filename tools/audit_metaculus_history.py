"""Read-only Metaculus bot history audit; never stores raw forecasts or question IDs.

Posts feed discovers candidate question IDs; documented GET bulk-forecast-read
retrieves the acting bot's forecast history. This is NOT publication reconciliation.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOST = "https://www.metaculus.com"
BOT_ID = 307558
PAGE_SIZE = 100
MAX_PAGES = 200
BATCH_SIZE = 40
MIN_REQUEST_INTERVAL = 5.0  # Conservative spacing between GET requests
MAX_GET_REQUESTS = 100        # Fail closed instead of hammering an API
_LAST_REQUEST = None
_REQUEST_COUNT = 0
QUESTION_KINDS = {"binary", "multiple_choice", "numeric", "discrete", "date"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise RuntimeError("Redirect refused; credentials were not forwarded")


def get_json(opener, token, path, params=()):
    global _LAST_REQUEST, _REQUEST_COUNT
    if _REQUEST_COUNT >= MAX_GET_REQUESTS:
        raise RuntimeError("Conservative GET request budget reached; history not exported")
    if _LAST_REQUEST is not None:
        pause = MIN_REQUEST_INTERVAL - (time.monotonic() - _LAST_REQUEST)
        if pause > 0:
            time.sleep(pause)
    _LAST_REQUEST = time.monotonic()
    _REQUEST_COUNT += 1
    query = urllib.parse.urlencode(params)
    url = HOST + path + (("?" + query) if query else "")
    request = urllib.request.Request(
        url, headers={"Authorization": "Token " + token, "Accept": "application/json"},
        method="GET",
    )
    try:
        with opener.open(request, timeout=25) as response:
            if response.status != 200:
                raise RuntimeError("Unexpected Metaculus API response")
            raw = response.read(15_000_001)
            if len(raw) > 15_000_000:
                raise RuntimeError("API response exceeds safe size limit")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            # Never automatically retry a throttled request.
            raise RuntimeError("Metaculus rate limit HTTP 429; stop audit and honor server cooldown") from None
        raise RuntimeError(f"Metaculus API HTTP {exc.code}; no data exported") from None
    except urllib.error.URLError:
        raise RuntimeError("Metaculus API network/TLS failure; no data exported") from None


def timestamp(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        except ValueError:
            return None
    return None


def question_ids(post):
    """Read ID fields only; do not process titles or forecast values."""
    found = set()

    def add(q):
        if isinstance(q, dict):
            qid = q.get("id")
            if (isinstance(qid, int) and not isinstance(qid, bool) and qid > 0
                    and q.get("type") in QUESTION_KINDS):
                found.add(qid)

    add(post.get("question"))
    group = post.get("group_of_questions")
    if isinstance(group, dict):
        for q in group.get("questions") or []:
            add(q)
    conditional = post.get("conditional")
    if isinstance(conditional, dict):
        for key in ("condition", "condition_child", "question_yes", "question_no"):
            add(conditional.get(key))
    return found


def main():
    parser = argparse.ArgumentParser(description="Aggregate-only audit, no forecasts posted")
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--output", default="metaculus-audit-summary.json")
    args = parser.parse_args()
    if args.user_id != BOT_ID:
        raise RuntimeError("Unexpected bot account")
    token = os.environ.get("METACULUS_TOKEN", "").strip()
    if token.startswith("Token "):
        token = token[6:].strip()
    if len(token) < 32:
        raise RuntimeError("METACULUS_TOKEN is missing or invalid in shape")
    opener = urllib.request.build_opener(NoRedirect())
    identity = get_json(opener, token, f"/api/users/{BOT_ID}/", (("acting_user", BOT_ID),))
    if not isinstance(identity, dict) or identity.get("id") != BOT_ID:
        raise RuntimeError("Unable to verify acting bot identity")

    ids = set()
    seen_posts = set()
    pages = 0
    # The posts feed identifies questions forecast by Vrchcab, including closed ones.
    for page in range(MAX_PAGES):
        data = get_json(opener, token, "/api/posts/", (
            ("acting_user", BOT_ID),
            ("forecaster_id", BOT_ID),
            ("limit", PAGE_SIZE),
            ("offset", page * PAGE_SIZE),
        ))
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise RuntimeError("Unexpected posts-feed response structure")
        posts = data["results"]
        pages += 1
        for post in posts:
            if not isinstance(post, dict) or not isinstance(post.get("id"), int):
                raise RuntimeError("Unexpected posts-feed item")
            if post["id"] not in seen_posts:
                seen_posts.add(post["id"])
                ids.update(question_ids(post))
        # Metaculus uses CountlessLimitOffsetPagination: "next" can stay
        # non-null even after the last record (the count is infinite).
        # An empty results page is therefore the reliable end marker.
        if not posts:
            break
        if data.get("next") is None:
            break
    else:
        raise RuntimeError("Posts-feed exceeded safe page limit")

    if not seen_posts:
        raise RuntimeError("Posts API returned no bot-forecasted posts; refusing a zero-history claim")
    if not ids:
        raise RuntimeError("Posts API did not identify any question IDs to audit")
    if len(ids) > 20000:
        raise RuntimeError("Too many question IDs for safe auditing")

    question_count = 0
    forecast_count = 0
    oldest = None
    newest = None
    ordered_ids = sorted(ids)
    # GET bulk-forecast-read returns the acting user's full history by question ID.
    for index in range(0, len(ordered_ids), BATCH_SIZE):
        batch = ordered_ids[index:index + BATCH_SIZE]
        params = [("acting_user", BOT_ID)] + [("question_ids", qid) for qid in batch]
        result = get_json(opener, token, "/api/questions/bulk-forecast-read/", params)
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise RuntimeError("Unexpected bulk forecast response structure")
        returned = set()
        for item in result["results"]:
            if not isinstance(item, dict) or not isinstance(item.get("question_id"), int):
                raise RuntimeError("Unexpected bulk forecast item")
            qid = item["question_id"]
            if qid not in batch or qid in returned:
                raise RuntimeError("Bulk forecast returned unexpected question")
            returned.add(qid)
            forecasts = item.get("forecasts")
            if not isinstance(forecasts, list):
                raise RuntimeError("Missing bulk forecast history")
            if forecasts:
                question_count += 1
            forecast_count += len(forecasts)
            for forecast in forecasts:
                if not isinstance(forecast, dict):
                    raise RuntimeError("Unexpected forecast record")
                author = forecast.get("author_id")
                if author is not None and author != BOT_ID:
                    raise RuntimeError("Returned forecast did not belong to acting bot")
                at = timestamp(forecast.get("start_time"))
                if at is not None:
                    oldest = at if oldest is None or at < oldest else oldest
                    newest = at if newest is None or at > newest else newest

    if forecast_count == 0:
        raise RuntimeError("Bulk API returned no forecasts for bot-filtered posts; no report saved")
    summary = {
        "audit_type": "read_only_bot_forecast_inventory",
        "bot_user_id": BOT_ID,
        "scanned_at_utc": datetime.now(timezone.utc).isoformat(),
        "posts_returned": len(seen_posts),
        "question_ids_examined": len(ids),
        "questions_with_history": question_count,
        "forecast_records_found": forecast_count,
        "earliest_observed_forecast_utc": oldest,
        "latest_observed_forecast_utc": newest,
        "history_coverage_note": "Only questions in the filtered posts feed; missing or hidden posts may be omitted",
        "reconciliation_ready": False,
        "contains_forecast_values_or_question_ids": False,
    }
    path = Path(args.output)
    if path.name != str(path):
        raise RuntimeError("Output must be a local filename")
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Read-only audit complete: {question_count} questions, {forecast_count} historical records.")
    print("This inventory is not proof that any uncertain earlier publication attempt is resolved.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Audit stopped safely: {exc}", file=sys.stderr)
        sys.exit(1)

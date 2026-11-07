"""Read-only, aggregate-only audit of the acting Metaculus bot's own forecasts.

No forecast/comment values, private text, tokens, or per-question IDs are written.
This is an inventory, NOT a reliable reconciliation of pre-existing POST attempts.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOST = "https://www.metaculus.com"
PAGE_SIZE = 100
MAX_PAGES = 200


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise RuntimeError("Metaculus redirected the audit request; refusing to forward credentials")


def read_json(opener, token, path, params=None):
    url = HOST + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Token " + token, "Accept": "application/json"},
        method="GET",
    )
    try:
        with opener.open(request, timeout=25) as response:
            if response.status != 200:
                raise RuntimeError("Unexpected Metaculus API response")
            raw = response.read(15_000_000 + 1)
            if len(raw) > 15_000_000:
                raise RuntimeError("Response size exceeded safe limit")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        # Do not print response bodies, cookies, headers, or tokens.
        raise RuntimeError(f"Metaculus API returned HTTP {exc.code}; no history saved") from None
    except urllib.error.URLError:
        raise RuntimeError("Metaculus API network/TLS error; no history saved") from None


def to_utc(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        except (OverflowError, ValueError, OSError):
            return None
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        except ValueError:
            return None
    return None


def find_my_forecasts(node):
    """Visit nested group/conditional questions without copying their payloads."""
    if isinstance(node, dict):
        if "my_forecasts" in node:
            yield node["my_forecasts"]
        for key, child in node.items():
            if key != "my_forecasts":
                yield from find_my_forecasts(child)
    elif isinstance(node, list):
        for child in node:
            yield from find_my_forecasts(child)


def observed_times(node):
    """Read only available forecast timestamps; never claim complete history."""
    if isinstance(node, dict):
        if "start_time" in node:
            stamp = to_utc(node["start_time"])
            if stamp is not None:
                yield stamp
        for key, value in node.items():
            if key != "start_time":
                yield from observed_times(value)
    elif isinstance(node, list):
        for value in node:
            yield from observed_times(value)


def main():
    parser = argparse.ArgumentParser(description="Read-only aggregate Metaculus history audit")
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--output", default="metaculus-audit-summary.json")
    args = parser.parse_args()
    if args.user_id <= 0 or args.user_id != 307558:
        raise RuntimeError("Unexpected bot user ID; audit pinned to Vrchcab (307558)")
    token = os.environ.get("METACULUS_TOKEN", "").strip()
    if token.startswith("Token "):
        token = token[6:].strip()
    if len(token) < 32:
        raise RuntimeError("Missing or implausibly short METACULUS_TOKEN")
    opener = urllib.request.build_opener(NoRedirect())
    user = read_json(opener, token, f"/api/users/{args.user_id}/", {"acting_user": args.user_id})
    if not isinstance(user, dict) or user.get("id") != args.user_id:
        raise RuntimeError("API token did not verify the expected bot user ID")

    seen_posts = set()
    my_forecasts_fields = 0
    nonnull_fields = 0
    observations = 0
    latest = None
    page_count = 0
    complete_pagination = False
    for page in range(MAX_PAGES):
        data = read_json(
            opener, token, "/api/posts/",
            {"acting_user": args.user_id, "forecaster_id": args.user_id, "limit": PAGE_SIZE, "offset": page * PAGE_SIZE},
        )
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise RuntimeError("Unexpected posts API shape; refusing to imply an empty history")
        items = data["results"]
        page_count += 1
        for post in items:
            if not isinstance(post, dict):
                raise RuntimeError("Unexpected post type")
            post_id = post.get("id")
            if not isinstance(post_id, int):
                raise RuntimeError("Post ID missing")
            if post_id in seen_posts:
                continue
            seen_posts.add(post_id)
            for field in find_my_forecasts(post):
                my_forecasts_fields += 1
                if field is not None:
                    nonnull_fields += 1
                    stamps = list(observed_times(field))
                    observations += len(stamps)
                    for stamp in stamps:
                        if latest is None or stamp > latest:
                            latest = stamp
        count = data.get("count")
        if isinstance(count, int) and page * PAGE_SIZE + len(items) >= count:
            complete_pagination = True
            break
        if not items or (count is None and len(items) < PAGE_SIZE):
            complete_pagination = True
            break

    if not seen_posts or my_forecasts_fields == 0 or nonnull_fields == 0:
        raise RuntimeError("Metaculus did not supply verifiable bot forecast fields; refusing an empty-history conclusion")

    summary = {
        "audit_type": "read_only_account_forecast_inventory",
        "bot_user_id": args.user_id,
        "scanned_at_utc": datetime.now(timezone.utc).isoformat(),
        "pages_read": page_count,
        "unique_forecasted_posts_returned": len(seen_posts),
        "my_forecasts_fields_seen": my_forecasts_fields,
        "nonnull_my_forecasts_fields": nonnull_fields,
        "forecast_timestamps_observed": observations,
        "latest_observed_forecast_utc": latest,
        "pagination_complete": complete_pagination,
        "historical_coverage": "unknown (API may return latest forecast only, omit older history, or null restricted fields)",
        "reconciliation_ready": False,
        "contains_forecast_values_or_question_ids": False,
    }
    if not complete_pagination:
        raise RuntimeError("Page cap reached; no report saved because inventory is incomplete")
    output = Path(args.output)
    if output.is_absolute() or output.name != str(output):
        raise RuntimeError("Output must be a filename in the working directory")
    output.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Metaculus audit completed; aggregate-only report saved.")
    print(f"Forecasted posts: {len(seen_posts)}; last visible forecast: {latest or 'unavailable'}")
    print("Do not use this report alone to declare old publication attempts reconciled.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # No response bodies, token, or raw forecast data in the logs.
        print(f"Audit failed safely: {exc}", file=sys.stderr)
        sys.exit(1)

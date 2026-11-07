"""Public, read-only first-page watch for currently open Metaculus tournament posts.

A candidate means an open post returned by Metaculus, NOT proof that the bot
has never forecast it or that an eligible question is currently available.
Exactly 0-2 authenticated READ-ONLY HTTPS GETs. No POSTs, models, or publication.
The Vrchcab token is read only from the GitHub secret environment, never logged.
Unauthenticated GET returned HTTP 403 from GitHub Actions on 2026-11-07.
On pagination ambiguity, HTTP 429, unexpected data or network errors: fail
closed and do not report "zero questions".
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = "www.metaculus.com"
API_PATH = "/api/posts/"
TARGETS = (("Fall FutureEval 2026", "33121"), ("MiniBench", "minibench"))
FIRST_PAGE_LIMIT = 100
MAX_BODY_BYTES = 15_000_000
MIN_REQUEST_SPACING = 6.0


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise RuntimeError("Redirect refused; no second host contacted")


def normalized_token(value):
    token = value.strip()
    if token.startswith("Token "):
        token = token[6:].strip()
    if len(token) < 32 or any(c.isspace() for c in token):
        raise RuntimeError("Missing/malformed read-only Metaculus credential; zero requests")
    return token


def make_request(project, token):
    if project not in {project_id for _, project_id in TARGETS}:
        raise RuntimeError("Unexpected project; refusing request")
    params = [
        ("limit", str(FIRST_PAGE_LIMIT)),
        ("offset", "0"),
        ("order_by", "-published_time"),
        ("with_cp", "true"),
        ("include_conditional_cps", "true"),
        ("statuses", "open"),
        ("tournaments", project),
    ]
    # Match SDK get_all_open_questions_from_tournament: allowed_types is None,
    # so DO NOT pass a forecast_type filter or change server search semantics.
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        "https://" + HOST + API_PATH + "?" + query,
        headers={"Accept": "application/json",
                 "Authorization": "Token " + normalized_token(token),
                 "User-Agent": "Vrchcab-authenticated-readonly-open-post-watch/1"},
        method="GET",
    )
    if req.get_method() != "GET" or not req.has_header("Authorization"):
        raise RuntimeError("Unauthenticated or non-GET request rejected")
    return req


def summarize(data):
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise RuntimeError("Unexpected response schema; no conclusion")
    rows = data["results"]
    if len(rows) > FIRST_PAGE_LIMIT or not all(isinstance(p, dict) for p in rows):
        raise RuntimeError("Unexpected post data; no conclusion")
    if data.get("next") is not None or len(rows) == FIRST_PAGE_LIMIT:
        raise RuntimeError("Potential pagination; do not report incomplete count")
    # Deliberately neither inspect nor disclose post IDs, titles, question
    # IDs, descriptions, outcomes or user-specific forecast history.
    return len(rows)


def fetch_one(opener, project, token):
    request = make_request(project, token)
    try:
        with opener.open(request, timeout=18) as response:
            if response.status != 200:
                raise RuntimeError("Unexpected HTTP status; no conclusion")
            raw = response.read(MAX_BODY_BYTES + 1)
            if len(raw) > MAX_BODY_BYTES:
                raise RuntimeError("Response exceeds byte cap; no conclusion")
            return summarize(json.loads(raw))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError("HTTP 429; stop immediately without retry") from None
        raise RuntimeError("HTTP " + str(exc.code) + "; stop without retry") from None
    except urllib.error.URLError:
        raise RuntimeError("Network/TLS error; stop without retry") from None
    except (json.JSONDecodeError, UnicodeError):
        raise RuntimeError("Malformed JSON; no conclusion") from None


def check():
    token = normalized_token(os.environ.get("METACULUS_TOKEN", ""))
    opener = urllib.request.build_opener(NoRedirect())
    rows = []
    before = None
    for label, project in TARGETS:
        if before is not None:
            delay = MIN_REQUEST_SPACING - (time.monotonic() - before)
            if delay > 0:
                time.sleep(delay)
        before = time.monotonic()
        rows.append((label, fetch_one(opener, project, token)))
    total = sum(n for _, n in rows)
    report = {
        "watch": "public_read_only_first_page",
        "projects": [{"project": name, "open_posts": count} for name, count in rows],
        "open_candidates_found": total > 0,
        "requests": len(TARGETS),
        "bot_forecast_status_verified": False,
        "publishes_forecasts": False,
        "authenticated_read_only": True,
        "note": "Open public posts are candidates only; verify prior Vrchcab forecasts and eligibility before any live run.",
    }
    print(json.dumps(report, ensure_ascii=False), flush=True)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as out:
            out.write("### Vrchcab read-only Metaculus monitor\n\n")
            for name, count in rows:
                out.write("- " + name + ": **" + str(count) + "** public open posts\n")
            out.write("\nOpen posts are **not** proof of new, unforecasted questions. Authenticated GET only; no forecast, token value disclosure or paid models.\n")
    if total:
        print("::warning::Publicly open Metaculus posts detected; manually check Vrchcab history before forecasting.", flush=True)
    else:
        print("No currently open posts returned by the two server-filtered first-page queries.", flush=True)


def self_check():
    for _, project in TARGETS:
        r = make_request(project, "a" * 48)
        u = urllib.parse.urlsplit(r.full_url)
        q = urllib.parse.parse_qs(u.query)
        assert (u.scheme, u.netloc, u.path) == ("https", HOST, API_PATH)
        assert q["tournaments"] == [project]
        assert q["statuses"] == ["open"]
        assert q["offset"] == ["0"] and q["limit"] == [str(FIRST_PAGE_LIMIT)]
        assert "forecast_type" not in q
        assert r.get_method() == "GET"
        assert r.get_header("Authorization") == "Token " + "a" * 48
    assert summarize({"results": []}) == 0
    assert summarize({"results": [{}]}) == 1
    print("Offline request contract OK; no network used.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-check", action="store_true")
    mode.add_argument("--check-public-open-posts", action="store_true")
    args = parser.parse_args()
    try:
        self_check() if args.self_check else check()
    except (RuntimeError, OSError) as exc:
        print("READ_ONLY_WATCH_STOPPED: " + str(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

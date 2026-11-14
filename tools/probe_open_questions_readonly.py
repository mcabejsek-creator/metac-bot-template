"""Two-public-GET Metaculus inventory probe. NOT a forecast bot.

Requires explicit command-line opt-in to contact Metaculus. Never imports main.py,
loads account credentials, uses POST, reads personal forecast history, or prints
question IDs / titles / forecasts. Stop immediately on HTTP 429; never retry.
This file must not run automatically in CI with its live flag.
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.metaculus.com/api/posts/"
TARGETS = (("Fall FutureEval 2026", "33121"), ("MiniBench", "minibench"))
MAX_POSTS = 100
MAX_BYTES = 15_000_000
TYPES = (
    "binary", "numeric", "multiple_choice", "date", "discrete",
    "conditional", "group_of_questions",
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise RuntimeError("Redirect refused; public GET not followed")


def make_request(tournament):
    # Mirrors the SDK's FIRST PAGE search fields, without any credentials.
    params = [
        ("limit", str(MAX_POSTS)),
        ("offset", "0"),
        ("order_by", "-published_time"),
        ("with_cp", "true"),
        ("include_conditional_cps", "true"),
        ("statuses", "open"),
        ("tournaments", str(tournament)),
    ] + [("forecast_type", kind) for kind in TYPES]
    request = urllib.request.Request(
        BASE + "?" + urllib.parse.urlencode(params),
        headers={"Accept": "application/json", "User-Agent": "Vrchcab-readonly-diagnostic/1"},
        method="GET",
    )
    assert request.get_method() == "GET"
    assert not request.has_header("Authorization")
    return request


def summarize(data):
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise RuntimeError("Unexpected public API response; no result reported")
    posts = data["results"]
    if len(posts) > MAX_POSTS:
        raise RuntimeError("API returned more posts than requested")
    # Deliberately do not expose or retain post IDs, question names or forecasts.
    return {
        "open_posts_first_page": len(posts),
        "first_page_limit": MAX_POSTS,
        "additional_pages_possible": len(posts) == MAX_POSTS,
        "inference": (
            "No posts returned by this public server-filtered first-page query"
            if not posts else
            "Public open posts exist; this does not show whether Vrchcab forecast them"
        ),
    }


def run_live():
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect()
    )
    for label, target in TARGETS:
        request = make_request(target)
        try:
            with opener.open(request, timeout=15) as response:
                if response.status != 200:
                    raise RuntimeError("Unexpected HTTP status; no result reported")
                payload = response.read(MAX_BYTES + 1)
                if len(payload) > MAX_BYTES:
                    raise RuntimeError("Response too large; no result reported")
                summary = summarize(json.loads(payload))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RuntimeError(
                    "HTTP 429 rate limit: stopped immediately; DO NOT retry"
                ) from None
            raise RuntimeError(f"HTTP {exc.code}: stopped without retries") from None
        except urllib.error.URLError:
            raise RuntimeError("Network/TLS error: stopped without retries") from None
        print(json.dumps({"tournament": label, **summary}, ensure_ascii=False))
    print("Read-only probe complete: exactly two public GET attempts at most, no posting.")


def self_check():
    for _, target in TARGETS:
        req = make_request(target)
        url = urllib.parse.urlsplit(req.full_url)
        query = urllib.parse.parse_qs(url.query)
        assert url.scheme == "https" and url.netloc == "www.metaculus.com"
        assert url.path == "/api/posts/"
        assert query["tournaments"] == [target]
        assert query["statuses"] == ["open"]
        assert query["limit"] == ["100"] and query["offset"] == ["0"]
        assert query["forecast_type"] == list(TYPES)
        assert req.get_method() == "GET"
        assert not req.has_header("Authorization")
    assert summarize({"results": []})["open_posts_first_page"] == 0
    assert summarize({"results": [{}] * 100})["additional_pages_possible"]
    print("Self-check OK: correct public GET filters, no credentials, no network requests.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-check", action="store_true")
    mode.add_argument(
        "--run-two-public-gets",
        action="store_true",
        help="Explicitly authorize up to two free public Metaculus GET requests, no retry",
    )
    args = parser.parse_args()
    try:
        if args.self_check:
            self_check()
        elif args.run_two_public_gets:
            run_live()
    except (RuntimeError, ValueError) as exc:
        print(f"Read-only probe stopped: {exc}", file=sys.stderr)
        sys.exit(1)

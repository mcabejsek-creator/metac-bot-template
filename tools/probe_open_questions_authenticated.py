"""Explicitly authorized TWO-GET inventory check for current tournament questions.

No forecast publishing; no model calls; no question IDs or forecast values logged.
Live requests require --run-two-authorized-gets. No redirects or retries.
The token is only read from the local environment on the runner and never printed.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from probe_open_questions_readonly import (
    MAX_BYTES,
    NoRedirect,
    TARGETS,
    make_request,
    summarize,
)

MIN_SECONDS_BETWEEN = 6.0


def make_authorized_request(target, token):
    request = make_request(target)
    if request.get_method() != "GET":
        raise RuntimeError("Refusing non-GET request")
    if not token or len(token) < 32:
        raise RuntimeError("Missing or malformed Metaculus token; no requests sent")
    request.add_header("Authorization", "Token " + token)
    return request


def self_check():
    synthetic = "x" * 48
    for name, target in TARGETS:
        req = make_authorized_request(target, synthetic)
        assert req.get_method() == "GET"
        assert req.has_header("Authorization")
        assert req.get_header("Authorization") == "Token " + synthetic
    assert summarize({"results": []})["open_posts_first_page"] == 0
    print("Self-check OK: two intended targets, authenticated GET only; no network.")


def run_two():
    token = os.environ.get("METACULUS_TOKEN", "").strip()
    if token.startswith("Token "):
        token = token[6:].strip()
    if not token or len(token) < 32:
        raise RuntimeError("Metaculus secret missing or invalid; zero requests sent")
    # Default proxy environment matches normal GitHub Actions egress.
    # NoRedirect blocks re-sending credentials to any redirect destination.
    opener = urllib.request.build_opener(NoRedirect())
    previous = None
    for label, target in TARGETS:
        request = make_authorized_request(target, token)
        if previous is not None:
            delay = MIN_SECONDS_BETWEEN - (time.monotonic() - previous)
            if delay > 0:
                time.sleep(delay)
        previous = time.monotonic()
        try:
            with opener.open(request, timeout=15) as response:
                if response.status != 200:
                    raise RuntimeError("Unexpected HTTP response; stopped without retries")
                payload = response.read(MAX_BYTES + 1)
                if len(payload) > MAX_BYTES:
                    raise RuntimeError("Response exceeds size cap; stopped")
                results = summarize(json.loads(payload))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RuntimeError("HTTP 429 rate limit; stopped immediately, do not retry") from None
            raise RuntimeError(f"HTTP {exc.code}; stopped without retries") from None
        except urllib.error.URLError:
            raise RuntimeError("Network or TLS failure; stopped without retries") from None
        print(json.dumps({"tournament": label, **results}, ensure_ascii=False), flush=True)
    print("Authorized read-only inventory completed; 2 GETs total; 0 forecast POSTs.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-check", action="store_true")
    mode.add_argument("--run-two-authorized-gets", action="store_true")
    args = parser.parse_args()
    try:
        self_check() if args.self_check else run_two()
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        # Only sanitized exceptions raised by this script reach output.
        print(f"Authorized probe stopped: {exc}", file=sys.stderr)
        sys.exit(1)

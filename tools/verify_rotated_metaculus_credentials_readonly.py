"""Single-use, strictly read-only post-rotation GitHub credential verification.

ONLY two HTTPS GET requests at most, to /api/users/me/:
  1. METACULUS_TOKEN_V2 must be the Vrchcab bot (Metaculus ID 307558).
  2. Legacy METACULUS_TOKEN must be rejected after rotation.
Never posts forecasts, changes any token, imports forecasting code, prints
tokens, raw account responses, IDs, usernames or HTTP response bodies.
No automatic retry, no redirect follow, no paid model calls.
"""
import argparse
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

URL = "https://www.metaculus.com/api/users/me/"
EXPECTED_BOT_ID = 307558
EXPECTED_BOT_USERNAME = "vrchcab"
MAX_BYTES = 16_384


class BlockRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise RuntimeError("Unexpected redirect: abort without following")


def normalize_token(value):
    if not isinstance(value, str):
        raise RuntimeError("Missing credential")
    token = value.strip()
    if token.startswith("Token "):
        token = token[6:].strip()
    if len(token) < 32 or any(x.isspace() for x in token):
        raise RuntimeError("Missing or malformed credential")
    return token


def authorized_get(opener, token):
    req = urllib.request.Request(
        URL,
        headers={
            "Authorization": "Token " + token,
            "Accept": "application/json",
            "User-Agent": "Vrchcab-single-use-credential-check/1",
        },
        method="GET",
    )
    if req.get_method() != "GET" or urllib.parse.urlparse(req.full_url).path != "/api/users/me/":
        raise RuntimeError("Unexpected HTTP request shape")
    try:
        with opener.open(req, timeout=15) as response:
            if response.status != 200:
                return "UNEXPECTED_STATUS", None
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return "OVERSIZED_RESPONSE", None
            try:
                parsed = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return "MALFORMED_RESPONSE", None
            if not isinstance(parsed, dict):
                return "MALFORMED_RESPONSE", None
            # Response stays in memory; only static status labels are printed.
            return "HTTP_200", parsed
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return "HTTP_401", None
        if exc.code == 403:
            return "HTTP_403", None
        if exc.code == 429:
            return "HTTP_429", None
        return "OTHER_HTTP_ERROR", None
    except urllib.error.URLError:
        return "NETWORK_ERROR", None


def check_new_identity(status, response):
    if status != "HTTP_200" or not isinstance(response, dict):
        return "NEW_SECRET_NOT_VERIFIED"
    bot_id = response.get("id")
    username = response.get("username")
    if bot_id == EXPECTED_BOT_ID and isinstance(username, str) and username.casefold() == EXPECTED_BOT_USERNAME:
        return "NEW_SECRET_VALID_BOT_VRCHCAB"
    return "NEW_SECRET_WRONG_OR_UNKNOWN_ACCOUNT"


def self_check():
    first = normalize_token("a" * 48)
    second = normalize_token("Token " + "b" * 48)
    assert first == "a" * 48 and second == "b" * 48
    assert not hmac.compare_digest(first, second)
    assert check_new_identity("HTTP_200", {"id": EXPECTED_BOT_ID, "username": "Vrchcab"}) == "NEW_SECRET_VALID_BOT_VRCHCAB"
    assert check_new_identity("HTTP_200", {"id": 42, "username": "Someone"}) == "NEW_SECRET_WRONG_OR_UNKNOWN_ACCOUNT"
    assert check_new_identity("HTTP_401", None) == "NEW_SECRET_NOT_VERIFIED"
    assert URL.endswith("/api/users/me/")
    print("Offline request-shape checks passed; no secrets used and no network called")


def verify():
    # Read only two pre-existing repository secrets, no GitHub or Metaculus writes.
    new_value = normalize_token(os.getenv("METACULUS_TOKEN_V2", ""))
    old_value = normalize_token(os.getenv("METACULUS_TOKEN", ""))
    if hmac.compare_digest(new_value, old_value):
        raise RuntimeError("ERROR_SECRETS_IDENTICAL; no API requests sent")

    opener = urllib.request.build_opener(BlockRedirect())
    status_new, details = authorized_get(opener, new_value)
    classification = check_new_identity(status_new, details)
    print("NEW_TOKEN=" + classification, flush=True)
    if classification != "NEW_SECRET_VALID_BOT_VRCHCAB":
        # On 429/403/etc stop immediately; do not issue second request.
        raise RuntimeError("NEW_TOKEN_CHECK_NOT_SUCCESSFUL; legacy secret not tested")

    # Keep requests apart to respect the site's limits.
    time.sleep(6)
    status_old, _ = authorized_get(opener, old_value)
    if status_old == "HTTP_401":
        print("OLD_TOKEN=REJECTED_HTTP_401", flush=True)
        print("ROTATION_CHECK=PASS: new bot credential works, old credential rejected")
    elif status_old == "HTTP_403":
        print("OLD_TOKEN=DENIED_HTTP_403_NOT_DEFINITIVE_REVOCATION", flush=True)
        print("ROTATION_CHECK=INCONCLUSIVE: server denied old token, cannot confirm revocation")
        raise RuntimeError("Old credential denied but revocation not conclusively proven")
    elif status_old == "HTTP_200":
        print("OLD_TOKEN=STILL_VALID", flush=True)
        print("ROTATION_CHECK=FAIL: legacy credential still accepted")
        raise RuntimeError("Legacy credential remains valid; do not re-enable publishing")
    else:
        print("OLD_TOKEN=" + (status_old if status_old in (
            "HTTP_429", "NETWORK_ERROR", "UNEXPECTED_STATUS",
            "OVERSIZED_RESPONSE", "MALFORMED_RESPONSE", "OTHER_HTTP_ERROR",
        ) else "UNKNOWN_FAILURE"), flush=True)
        raise RuntimeError("Old token revocation not verified; do not retry automatically")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--self-check", action="store_true")
    selector.add_argument("--verify-rotated-pair", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_check:
            self_check()
        else:
            verify()
    except Exception:
        # Fail closed, without exposing secret-bearing exceptions/response data.
        print("CREDENTIAL_VERIFICATION_STOPPED: see categorical status only", file=sys.stderr)
        sys.exit(1)

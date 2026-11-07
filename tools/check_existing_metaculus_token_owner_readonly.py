"""One explicitly approved authenticated GET identifying the GitHub Metaculus token.

Only GET /api/users/me.  Never publishes, changes a token, posts a comment,
accesses other endpoints, logs credentials or sends any model request.
--self-check uses no network. Live mode is opt-in and single-use in CI.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API_URL = "https://www.metaculus.com/api/users/me"
BOT_USER_ID = 307558
MAIN_USERNAME = "michalcabejsek"
BOT_USERNAME = "vrchcab"
MAX_BYTES = 32_768


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise RuntimeError("Unexpected redirect: stopped without a second request")


def request_for_token(token):
    raw = token.strip()
    if raw.startswith("Token "):
        raw = raw[6:].strip()
    if len(raw) < 32 or "\r" in raw or "\n" in raw:
        raise RuntimeError("Missing or malformed Metaculus credential; no request sent")
    request = urllib.request.Request(
        API_URL,
        headers={
            "Authorization": "Token " + raw,
            "Accept": "application/json",
            "User-Agent": "Vrchcab-identity-diagnostic/1",
        },
        method="GET",
    )
    assert request.get_method() == "GET"
    return request


def classify(json_data):
    if not isinstance(json_data, dict):
        raise RuntimeError("Unexpected API response")
    identity = json_data.get("id")
    username = json_data.get("username")
    if not isinstance(identity, int) or isinstance(identity, bool) or identity <= 0:
        raise RuntimeError("Missing or invalid account id")
    if username is not None and not isinstance(username, str):
        raise RuntimeError("Invalid account username")
    normalized = username.casefold() if username else None
    if identity == BOT_USER_ID:
        if normalized not in (None, BOT_USERNAME):
            raise RuntimeError("Conflicting bot identity; do not infer token ownership")
        return "BOT_VRCHCAB"
    if normalized == MAIN_USERNAME:
        return "MAIN_MICHALCABEJSEK"
    return "OTHER_OR_UNRECOGNIZED_ACCOUNT"


def self_check():
    fake_token = "x" * 48
    req = request_for_token(fake_token)
    assert req.full_url == API_URL
    assert req.get_method() == "GET"
    assert req.get_header("Authorization") == "Token " + fake_token
    assert classify({"id": BOT_USER_ID, "username": "Vrchcab"}) == "BOT_VRCHCAB"
    assert classify({"id": 42, "username": "MichalCabejsek"}) == "MAIN_MICHALCABEJSEK"
    assert classify({"id": 43, "username": "AnotherUser"}) == "OTHER_OR_UNRECOGNIZED_ACCOUNT"
    print("Offline self-check OK: exactly one authenticated GET to /api/users/me, no network")


def run():
    request = request_for_token(os.environ.get("METACULUS_TOKEN", ""))
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError("Unexpected HTTP status; stopped")
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise RuntimeError("Unexpectedly large identity response")
            data = json.loads(raw)
    except urllib.error.HTTPError as error:
        if error.code == 429:
            raise RuntimeError("HTTP 429: stopped without retry") from None
        raise RuntimeError(f"HTTP {error.code}: stopped without retry") from None
    except urllib.error.URLError:
        raise RuntimeError("Network or TLS failure: stopped without retry") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Invalid API response: stopped without retry") from None

    # No account IDs, usernames, raw JSON or secret values are logged.
    print("GITHUB_METACULUS_SECRET_OWNER=" + classify(data))
    print("Authenticated identity GET succeeded; zero forecasts and zero other API requests")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    options = parser.add_mutually_exclusive_group(required=True)
    options.add_argument("--self-check", action="store_true")
    options.add_argument("--run-one-authorized-get", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_check:
            self_check()
        else:
            run()
    except RuntimeError as error:
        print("Read-only identity check stopped: " + str(error), file=sys.stderr)
        sys.exit(1)

"""One-shot, credentialed, read-only metadata audit of 3 pages x 2 Metaculus projects.

No predictive models, POSTs, retries, redirections or network writes.
Aggregate counters ONLY: no identifiers, titles, forecast values, URLs or tokens logged.
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
PATH = "/api/posts/"
TARGETS = (("Fall FutureEval 2026", "33121"), ("MiniBench", "minibench"))
OFFSETS = (0, 100, 200)
MAX_REQUESTS = 6
MAX_BYTES = 15_000_000
SPACING_SECONDS = 6.0


class AuditStopped(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AuditStopped("Redirect blocked; no additional host contacted")


def normalize_token(raw):
    token = raw.strip()
    if token.startswith("Token "):
        token = token[6:].strip()
    if len(token) < 32 or any(c.isspace() for c in token):
        raise AuditStopped("Missing or malformed token; zero Metaculus GET requests")
    return token


def make_request(project, offset, token):
    if project not in dict((p, n) for n, p in TARGETS):
        raise AuditStopped("Unexpected tournament")
    if offset not in OFFSETS or type(offset) is not int:
        raise AuditStopped("Unexpected offset")
    query = urllib.parse.urlencode((
        ("limit", "100"),
        ("offset", str(offset)),
        ("order_by", "-published_time"),
        ("with_cp", "true"),
        ("include_conditional_cps", "true"),
        ("statuses", "open"),
        ("tournaments", project),
    ))
    req = urllib.request.Request(
        f"https://{HOST}{PATH}?{query}",
        headers={"Authorization": "Token " + token,
                 "Accept": "application/json",
                 "User-Agent": "Vrchcab-authorized-six-get-metadata-only/1"},
        method="GET",
    )
    assert req.get_method() == "GET"
    return req


def open_page(opener, request):
    try:
        with opener.open(request, timeout=18) as response:
            if response.status != 200:
                raise AuditStopped("Unexpected HTTP response status")
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise AuditStopped("JSON body exceeded size cap")
    except urllib.error.HTTPError as exc:
        raise AuditStopped("HTTP 429; stop without retry" if exc.code == 429
                           else f"HTTP {exc.code}; stop without retry") from None
    except urllib.error.URLError:
        raise AuditStopped("Network/TLS error; stop without retry") from None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise AuditStopped("Unparseable JSON; stop") from None
    if not isinstance(data, dict) or "next" not in data or not isinstance(data.get("results"), list):
        raise AuditStopped("Invalid API response envelope; stop")
    if len(data["results"]) > 100 or any(not isinstance(p, dict) for p in data["results"]):
        raise AuditStopped("Invalid API post list; stop")
    if data["next"] is not None and not isinstance(data["next"], str):
        raise AuditStopped("Unexpected pagination marker; stop")
    return data


def leaves(post):
    if "notebook" in post:
        return [], True
    if "group_of_questions" in post:
        group = post["group_of_questions"]
        if not isinstance(group, dict):
            raise AuditStopped("Malformed group metadata; stop")
        found = group.get("questions")
    elif "conditional" in post:
        conditional = post["conditional"]
        if not isinstance(conditional, dict):
            raise AuditStopped("Malformed conditional metadata; stop")
        found = [conditional.get(k) for k in
                 ("condition", "condition_child", "question_yes", "question_no")
                 if conditional.get(k) is not None]
    else:
        found = [post.get("question")]
    if not isinstance(found, list) or not found or any(not isinstance(x, dict) for x in found):
        raise AuditStopped("Unexpected question metadata; stop")
    return found, False


def summarize(data, offset):
    summary = {"offset": offset, "raw_posts": len(data["results"]),
               "notebooks": 0, "open_question_records": 0,
               "open_with_history_field": 0, "open_with_nonempty_history": 0,
               "open_missing_history_field": 0,
               "next_indicated": data["next"] is not None}
    for p in data["results"]:
        questions, notebook = leaves(p)
        if notebook:
            summary["notebooks"] += 1
        for question in questions:
            if question.get("status") != "open":
                continue
            summary["open_question_records"] += 1
            own = question.get("my_forecasts")
            history = own.get("history") if isinstance(own, dict) else None
            if isinstance(history, list):
                summary["open_with_history_field"] += 1
                if history:
                    summary["open_with_nonempty_history"] += 1
            else:
                summary["open_missing_history_field"] += 1
    return summary


def audit(*, token, opener=None, sleeper=time.sleep, clock=time.monotonic):
    opener = opener or urllib.request.build_opener(NoRedirect())
    reports, attempts, last_start = [], 0, None
    for label, project in TARGETS:
        pages = []
        for offset in OFFSETS:
            if attempts >= MAX_REQUESTS:
                raise AuditStopped("Request cap reached")
            if last_start is not None:
                remaining = SPACING_SECONDS - (clock() - last_start)
                if remaining > 0:
                    sleeper(remaining)
            last_start = clock()
            request = make_request(project, offset, token)
            attempts += 1  # budget consumed BEFORE network access
            page = open_page(opener, request)
            pages.append(summarize(page, offset))
        reports.append({"project": label, "pages": pages})
    return {"audit": "one-shot-authenticated-read-only-metadata-only",
            "max_gets": MAX_REQUESTS, "actual_get_attempts": attempts,
            "projects": reports, "post_requests": 0, "model_calls": 0,
            "ids_or_forecast_values_logged": False,
            "notes": "Fixed offsets 0,100,200 only; next is not proof of more records. Counts are token-account metadata, not proof of eligibility or complete inventory."}


def self_check():
    fake_token = "x" * 48
    requests = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, n):
            return json.dumps({"results": [], "next": "phantom-offset"}).encode()
    class Opener:
        def open(self, req, timeout):
            requests.append(req)
            return Response()
    t = [10.0]
    def sleep(seconds):
        t[0] += seconds
    report = audit(token=fake_token, opener=Opener(), sleeper=sleep, clock=lambda: t[0])
    assert report["actual_get_attempts"] == 6
    assert t[0] >= 40.0  # five 6-second gaps
    assert len(requests) == 6
    for index, req in enumerate(requests):
        url = urllib.parse.urlsplit(req.full_url)
        params = urllib.parse.parse_qs(url.query)
        assert (url.scheme, url.hostname, url.path) == ("https", HOST, PATH)
        assert req.get_method() == "GET"
        assert params["offset"] == [str(OFFSETS[index % 3])]
        assert params["tournaments"] == [TARGETS[index // 3][1]]
        assert params["statuses"] == ["open"]
        assert "forecast_type" not in params
    assert fake_token not in json.dumps(report)
    print("Offline safety check passed: exactly six synthetic GETs; no network or secrets")


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-check", action="store_true")
    mode.add_argument("--run-approved-once", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_check:
            self_check()
            return
        token = normalize_token(os.environ.get("METACULUS_TOKEN", ""))
        print("Starting explicitly approved, at-most-six-GET authenticated Metaculus metadata audit", flush=True)
        print(json.dumps(audit(token=token), sort_keys=True), flush=True)
    except (AuditStopped, OSError, AssertionError) as exc:
        # Exceptions above intentionally avoid raw URLs, headers, response bodies.
        print("READ_ONLY_AUDIT_STOPPED: " + str(exc), file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()

"""Temporary one-shot aggregate-only Metaculus API page-shape diagnostic.
Never prints question IDs, titles, content, forecasts, token, or API payload.
Exactly at most two GETs. No retries/redirects, no forecast API.
"""
import collections
import json
import os
import sys
import time
import urllib.error
import urllib.request

from watch_open_metaculus_questions_readonly import (
    TARGETS, MAX_BODY_BYTES, NoRedirect, make_request, normalized_token
)


def classify(value):
    if isinstance(value, str):
        value = value.lower()
        return value if value in ("open", "closed", "resolved", "pending", "scheduled", "deleted") else "other"
    return "missing"



def next_parts(value):
    import urllib.parse
    if not isinstance(value, str) or not value:
        return None
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("", "https") or parsed.netloc not in ("", "www.metaculus.com"):
        raise RuntimeError("Unexpected next link origin; stop")
    if parsed.path != "/api/posts/":
        raise RuntimeError("Unexpected next link path; stop")
    return urllib.parse.parse_qs(parsed.query)


def next_offset(value):
    parts = next_parts(value)
    if parts is None:
        return None
    try:
        return int(parts.get("offset", ["0"])[0])
    except ValueError:
        raise RuntimeError("Bad next offset") from None


def next_query_keys(value):
    parts = next_parts(value)
    return sorted(parts.keys()) if parts is not None else []


def next_preserves_target(value, project):
    parts = next_parts(value)
    return parts is not None and parts.get("tournaments") == [project] and parts.get("statuses") == ["open"]


def inspect():
    token = normalized_token(os.getenv("METACULUS_TOKEN", ""))
    opener = urllib.request.build_opener(NoRedirect())
    for i, (label, project) in enumerate(TARGETS):
        if i:
            time.sleep(6)
        req = make_request(project, token)
        try:
            with opener.open(req, timeout=18) as resp:
                if resp.status != 200:
                    raise RuntimeError("Non-200, stop")
                data = resp.read(MAX_BODY_BYTES + 1)
                if len(data) > MAX_BODY_BYTES:
                    raise RuntimeError("Oversize, stop")
                parsed = json.loads(data)
        except urllib.error.HTTPError as e:
            raise RuntimeError("HTTP " + str(e.code) + ", stop") from None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
            raise RuntimeError("Unexpected response shape")
        rows = parsed["results"]
        if not all(isinstance(x, dict) for x in rows):
            raise RuntimeError("Rows not object")
        outer_statuses = collections.Counter()
        inner_statuses = collections.Counter()
        inner_states = collections.Counter()
        visible_group = 0
        for row in rows:
            outer_statuses[classify(row.get("status"))] += 1
            question = row.get("question")
            if isinstance(question, dict):
                inner_statuses[classify(question.get("status"))] += 1
                inner_states[classify(question.get("state"))] += 1
            if "group_of_questions" in row:
                visible_group += 1
        print(json.dumps({
            "project": label,
            "first_page_rows": len(rows),
            "has_next": parsed.get("next") is not None,
            "next_type": type(parsed.get("next")).__name__,
            "next_truthy": bool(parsed.get("next")),
            "next_empty_string": parsed.get("next") == "",
            "next_offset": next_offset(parsed.get("next")),
            "next_query_keys": next_query_keys(parsed.get("next")),
            "next_preserves_target": next_preserves_target(parsed.get("next"), project),
            "outer_status_counts": dict(outer_statuses),
            "question_status_counts": dict(inner_statuses),
            "question_state_counts": dict(inner_states),
            "group_post_count": visible_group,
            "response_keys": sorted(k for k in parsed if k in ("count", "next", "previous", "results")),
            "first_post_schema_keys": sorted(k for k in rows[0] if k in (
                "status", "question", "tournaments", "projects", "group_of_questions",
                "id", "created_at", "published_at", "type", "notebook")) if rows else [],
            "token_exposed": False,
        }), flush=True)


if __name__ == "__main__":
    try:
        inspect()
    except (RuntimeError, urllib.error.URLError, ValueError) as exc:
        # Only sanitized errors (none contain secrets or payload data).
        print("AGGREGATE_API_INSPECTION_STOPPED", file=sys.stderr)
        sys.exit(1)

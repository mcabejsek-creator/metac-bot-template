"""Separate, finite shadow workflow. Never publishes to Metaculus.

Prepare and finish have explicitly scoped credentials. Inference receives only
its dedicated OpenAI key, one frozen request and a precommitted single-use slot.
"""
from __future__ import annotations
import base64
from datetime import datetime, timezone
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
import zipfile
from shadow_capture import ARTIFACT, REPO, cipher
from shadow_comparison import (EXPERIMENT_ID, MODEL, MAX_ATTEMPTS, MAX_PROMPT_BYTES,
    RESERVE_PER_ATTEMPT_USD, ENDS_AT, eligible_time, request_for, probabilities,
    payload_digest, estimated_cost, ShadowRejected)

BRANCH = "vrchcab-shadow-sol-20261117"
LEDGER_PATH = "shadow-ledger.json"
ROOT = "/repos/" + REPO
STATE_URL = ROOT + "/contents/" + LEDGER_PATH
WORK = Path(".shadow")
MAX_BYTES = 1_000_000
# The Contents API wraps the file in base64 plus JSON. Keep this allowance
# separate from model/artifact responses and keep the file itself below 1 MB.
MAX_LEDGER_RESPONSE_BYTES = 2_000_000
MAX_LEDGER_RAW_BYTES = 1_000_000
MAX_CAPTURE_CIPHER_BYTES = 33_000
MAX_RESULT_BYTES = 8_192
MAX_RESULT_CIPHER_BYTES = 11_200
MAX_SLOT_BYTES = 45_000  # Twenty complete slots stay below 901 KB raw.
WORKFLOW = ".github/workflows/openai_shadow.yaml"
SOURCE_WORKFLOW = ".github/workflows/run_bot_automatic.yaml"


def now():
    return datetime.now(timezone.utc)


def date(value):
    if not isinstance(value, str):
        raise ShadowRejected("invalid_date")
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ShadowRejected("naive_date")
    return d


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def network(host, path, *, method="GET", headers=None, body=None, timeout=20):
    allowed_download = (host.endswith(".blob.core.windows.net") or host.endswith(".actions.githubusercontent.com"))
    if host not in ("api.github.com", "api.openai.com", "www.metaculus.com") and not allowed_download:
        raise ShadowRejected("host_denied")
    if not path.startswith("/") or "\r" in path or "\n" in path:
        raise ShadowRejected("path_denied")
    if allowed_download and (headers or method != "GET" or body is not None):
        raise ShadowRejected("download_auth_denied")
    if host == "api.openai.com" and (path != "/v1/responses" or method != "POST"):
        raise ShadowRejected("model_endpoint_denied")
    if host == "www.metaculus.com" and (method != "GET" or not re.fullmatch(r"/api/posts/[1-9][0-9]*/", path)):
        raise ShadowRejected("metaculus_write_denied")
    if host == "api.github.com" and (not path.startswith(ROOT + "/") or method not in ("GET", "PUT")
            or (method == "PUT" and path != STATE_URL)):
        raise ShadowRejected("github_write_denied")
    conn = http.client.HTTPSConnection(host, timeout=timeout)
    response_limit = (MAX_LEDGER_RESPONSE_BYTES
        if host == "api.github.com" and method == "GET" and path == STATE_URL + "?ref=" + BRANCH
        else MAX_BYTES)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        reply = conn.getresponse()
        raw = reply.read(response_limit + 1)
        if len(raw) > response_limit:
            raise ShadowRejected("response_too_large")
        return reply.status, dict(reply.getheaders()), raw
    finally:
        conn.close()


def gh(path, *, method="GET", value=None):
    key = os.environ.get("GITHUB_TOKEN", "")
    if not key:
        raise ShadowRejected("github_key_unavailable")
    status, headers, raw = network("api.github.com", path, method=method,
        headers={"Authorization": "Bearer " + key, "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "vrchcab-shadow-v1"},
        body=None if value is None else canonical(value))
    if status != 200:
        raise ShadowRejected("github_http_" + str(status))
    return json.loads(raw)


def check_context(event_name="workflow_run"):
    env = os.environ
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REPOSITORY") != REPO
            or env.get("GITHUB_REF") != "refs/heads/main" or env.get("GITHUB_RUN_ATTEMPT") != "1"
            or env.get("GITHUB_EVENT_NAME") != event_name
            or env.get("GITHUB_WORKFLOW_REF") != REPO + "/" + WORKFLOW + "@refs/heads/main"):
        raise ShadowRejected("context_denied")


def read_state():
    item = gh(STATE_URL + "?ref=" + BRANCH)
    if item.get("encoding") != "base64" or item.get("path") != LEDGER_PATH:
        raise ShadowRejected("ledger_shape_invalid")
    raw = base64.b64decode(item["content"], validate=False)
    if len(raw) >= MAX_LEDGER_RAW_BYTES:
        raise ShadowRejected("ledger_file_too_large_no_reset")
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if item.get("sha") != blob:
        raise ShadowRejected("ledger_integrity_invalid")
    data = json.loads(raw)
    slots = data.get("slots")
    if (set(data) != {"experiment", "schema", "slots"} or data["experiment"] != EXPERIMENT_ID
            or data["schema"] != 1 or not isinstance(slots, list) or len(slots) > MAX_ATTEMPTS):
        raise ShadowRejected("ledger_invalid_no_reset")
    seen = set()
    for slot in slots:
        if (not isinstance(slot, dict) or type(slot.get("post_id")) is not int or slot["post_id"] in seen
                or slot.get("reserved_usd") != str(RESERVE_PER_ATTEMPT_USD)
                or slot.get("state") not in ("reserved", "success", "failed")):
            raise ShadowRejected("ledger_slot_invalid")
        seen.add(slot["post_id"])
    return data, blob


def write_state(data, sha):
    raw = canonical(data)
    if len(raw) >= MAX_LEDGER_RAW_BYTES:
        raise ShadowRejected("ledger_file_too_large_no_write")
    expected = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    ack = gh(STATE_URL, method="PUT", value={"branch": BRANCH, "sha": sha,
        "message": "Record bounded shadow attempt", "content": base64.b64encode(raw).decode()})
    content = ack.get("content", {})
    if content.get("sha") != expected or content.get("path") != LEDGER_PATH:
        raise ShadowRejected("ledger_ack_uncertain_no_send")
    return expected


def artifact(source_id):
    listing = gh(ROOT + f"/actions/runs/{source_id}/artifacts?per_page=100")
    if listing.get("total_count", 101) > 100:
        raise ShadowRejected("artifact_inventory_partial")
    found = [x for x in listing.get("artifacts", []) if x.get("name") == ARTIFACT and not x.get("expired")]
    if not found:
        return None
    if len(found) != 1 or type(found[0].get("id")) is not int or found[0].get("size_in_bytes", MAX_BYTES+1) > MAX_BYTES:
        raise ShadowRejected("artifact_ambiguous")
    token = os.environ["GITHUB_TOKEN"]
    status, headers, raw = network("api.github.com", ROOT + f"/actions/artifacts/{found[0]['id']}/zip",
        headers={"Authorization": "Bearer " + token, "User-Agent": "vrchcab-shadow-v1"})
    if status == 302:
        location = next((v for k,v in headers.items() if k.lower() == "location"), "")
        target = urlsplit(location)
        if (target.scheme != "https" or target.username or target.password or target.port not in (None,443)
                or not target.hostname or not (target.hostname.endswith(".blob.core.windows.net")
                                                or target.hostname.endswith(".actions.githubusercontent.com"))):
            raise ShadowRejected("artifact_redirect_denied")
        status, _, raw = network(target.hostname, target.path + ("?" + target.query if target.query else ""))
    if status != 200:
        raise ShadowRejected("artifact_download_failed")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        items = archive.infolist()
        if len(items) != 1 or items[0].filename != "capture.enc" or items[0].file_size > MAX_BYTES:
            raise ShadowRejected("artifact_members_invalid")
        return archive.read(items[0])


def validate_snapshot(s, source, current):
    if (s.get("experiment") != EXPERIMENT_ID or s.get("repository") != REPO
            or s.get("run_id") != str(source["id"]) or s.get("head_sha") != source["head_sha"]
            or s.get("question_type") != "binary" or s.get("options") != ["Yes", "No"]
            or s.get("inconsistent") is not False
            or any(type(s.get(k)) is not int or s[k] <= 0 for k in ("post_id", "question_id"))
            or date(s["opens_at"]) > date(s["captured_at"])
            or not date(s["captured_at"]) <= date(s["baseline_verified_at"]) <= current
            or not eligible_time(date(s["captured_at"]), date(s["closes_at"]), current)):
        raise ShadowRejected("snapshot_not_prospectively_eligible")
    probabilities(s["baseline"], len(s["options"]))
    return request_for(s["prompt"], s["options"])


def check_still_open(s):
    token = os.environ.get("METACULUS_TOKEN", "")
    if not token:
        raise ShadowRejected("read_only_key_unavailable")
    status, _, raw = network("www.metaculus.com", f"/api/posts/{s['post_id']}/",
        headers={"Authorization": "Token " + token, "Accept": "application/json",
                 "Cache-Control": "no-cache", "User-Agent": "vrchcab-shadow-readonly"})
    if status != 200:
        raise ShadowRejected("question_status_unavailable")
    p = json.loads(raw)
    if not isinstance(p, dict) or not isinstance(p.get("question"), dict):
        raise ShadowRejected("question_shape_invalid")
    q = p["question"]
    if (type(p.get("id")) is not int or p["id"] != s["post_id"]
            or type(q.get("id")) is not int or q["id"] != s["question_id"]
            or q.get("type") != "binary" or q.get("status") != "open"
            or q.get("resolution") is not None
            or any(p.get(k) is not None for k in ("conditional", "group_of_questions", "notebook"))):
        raise ShadowRejected("question_no_longer_open")
    current = now()
    if not date(q.get("open_time")) <= date(s["captured_at"]) <= current:
        raise ShadowRejected("question_not_open_at_capture")
    close = date(q.get("actual_close_time") or q.get("scheduled_close_time"))
    if close <= current:
        raise ShadowRejected("question_closed")
    return close


def prepare():
    check_context()
    if now() >= ENDS_AT:
        print("SHADOW expired_no_model_call")
        return
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    run = event.get("workflow_run") or {}
    source_id = run.get("id")
    if type(source_id) is not int or source_id <= 0:
        raise ShadowRejected("source_run_invalid")
    source = gh(ROOT + f"/actions/runs/{source_id}")
    if (source.get("repository", {}).get("full_name") != REPO or source.get("head_repository", {}).get("full_name") != REPO
            or source.get("head_branch") != "main" or source.get("run_attempt") != 1
            or source.get("path") != SOURCE_WORKFLOW or source.get("conclusion") != "success"
            or source.get("event") not in ("push", "schedule", "workflow_dispatch")
            or source.get("head_sha") != run.get("head_sha")):
        raise ShadowRejected("untrusted_source_run")
    data, sha = read_state()
    if len(data["slots"]) >= MAX_ATTEMPTS:
        print("SHADOW limit_reached_no_model_call")
        return
    raw = artifact(source_id)
    if raw is None:
        print("SHADOW no_new_eligible_capture")
        return
    if len(raw) > MAX_CAPTURE_CIPHER_BYTES:
        raise ShadowRejected("capture_too_large_no_truncation")
    s = json.loads(cipher(os.environ.get("METACULUS_TOKEN", "")).decrypt(raw))
    payload = validate_snapshot(s, source, now())
    if any(x["post_id"] == s["post_id"] for x in data["slots"]):
        print("SHADOW already_reserved_no_retry")
        return
    effective_close = min(date(s["closes_at"]), check_still_open(s))
    if not eligible_time(date(s["captured_at"]), effective_close, now()):
        raise ShadowRejected("snapshot_expired_before_reservation")
    slot = {"post_id": s["post_id"], "question_id": s["question_id"], "source_run": source_id,
        "worker_run": os.environ["GITHUB_RUN_ID"], "digest": payload_digest(payload), "state": "reserved",
        "reserved_usd": str(RESERVE_PER_ATTEMPT_USD), "reserved_at": now().isoformat(),
        "snapshot": raw.decode(), "result": None}
    # Reserve storage for the largest allowed encrypted result before spending
    # this attempt, so finishing the twentieth slot cannot strand the ledger.
    if len(canonical(slot)) + MAX_RESULT_CIPHER_BYTES > MAX_SLOT_BYTES:
        raise ShadowRejected("slot_metadata_too_large_no_claim")
    data["slots"].append(slot)
    write_state(data, sha)
    WORK.mkdir(exist_ok=True)
    context = {"payload": payload, "digest": slot["digest"], "post_id": s["post_id"],
        "worker_run": slot["worker_run"], "captured_at": s["captured_at"], "closes_at": effective_close.isoformat(),
        "option_count": len(s["options"]), "prepared_at": now().isoformat()}
    (WORK / "request.json").write_bytes(canonical(context))
    with open(os.environ["GITHUB_OUTPUT"], "a") as out:
        out.write("ready=true\n")
    print("SHADOW reserved_attempt_count=" + str(len(data["slots"])))


def response_result(response, option_count):
    usage = response.get("usage") or {}
    cost = estimated_cost(usage.get("input_tokens"), usage.get("output_tokens"))
    if cost > RESERVE_PER_ATTEMPT_USD:
        raise ShadowRejected("usage_exceeds_reserved_estimate")
    if response.get("status") != "completed" or response.get("error") or response.get("incomplete_details"):
        raise ShadowRejected("incomplete_model_response_no_retry")
    if response.get("model") not in (MODEL,) and not str(response.get("model", "")).startswith(MODEL + "-"):
        raise ShadowRejected("wrong_response_model")
    messages = []
    for item in response.get("output", []):
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("status") != "completed":
            raise ShadowRejected("unexpected_response_item")
        for part in item.get("content", []):
            if part.get("type") != "output_text":
                raise ShadowRejected("nontext_response")
            messages.append(part.get("text", ""))
    if len(messages) != 1:
        raise ShadowRejected("ambiguous_response_text")
    result = json.loads(messages[0])
    if set(result) != {"probabilities", "rationale"} or not isinstance(result["rationale"], str):
        raise ShadowRejected("invalid_response_schema")
    result["probabilities"] = probabilities(result["probabilities"], option_count)
    return dict(result, state="success", model=response["model"], input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"], estimated_usd=str(cost))


def infer():
    check_context()
    for name in ("METACULUS_TOKEN", "GITHUB_TOKEN", "OPENROUTER_API_KEY", "PERPLEXITY_API_KEY"):
        if os.environ.get(name):
            raise ShadowRejected("inference_credential_isolation_failed")
    ctx = json.loads((WORK / "request.json").read_bytes())
    started_at = now()
    if (ctx["worker_run"] != os.environ["GITHUB_RUN_ID"] or payload_digest(ctx["payload"]) != ctx["digest"]
            or not eligible_time(date(ctx["captured_at"]), date(ctx["closes_at"]), started_at)
            or not 0 <= (started_at - date(ctx["prepared_at"])).total_seconds() <= 60):
        raise ShadowRejected("inference_permit_stale")
    key = os.environ.get("VRCHCAB_OPENAI_TEST_KEY", "")
    if not key or any(x.isspace() for x in key):
        raise ShadowRejected("dedicated_model_key_missing")
    with (WORK / "sent").open("x") as marker:
        marker.write(ctx["digest"])
    result = {"state": "failed", "reason": "unknown_request_outcome", "estimated_usd": None}
    started = time.monotonic()
    try:
        status, _, raw = network("api.openai.com", "/v1/responses", method="POST",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            body=canonical(ctx["payload"]), timeout=180)
        if status != 200:
            raise ShadowRejected("model_http_" + str(status))
        result = response_result(json.loads(raw), ctx["option_count"])
    except Exception:
        result = {"state": "failed", "reason": "request_or_validation_failed_no_retry", "estimated_usd": None}
    completed_at = now()
    result.update(started_at=started_at.isoformat(), completed_at=completed_at.isoformat(),
                  effective_closes_at=ctx["closes_at"], elapsed_seconds=round(time.monotonic()-started,3))
    if completed_at >= date(ctx["closes_at"]):
        result["state"] = "failed"
        result["reason"] = "candidate_completed_after_close"
    if len(canonical(result)) > MAX_RESULT_BYTES:
        # The request has already been spent. Retain validated accounting and
        # our timestamps, never retry or save an unbounded model rationale.
        result = {key: result[key] for key in (
            "input_tokens", "output_tokens", "estimated_usd", "started_at",
            "completed_at", "effective_closes_at", "elapsed_seconds") if key in result}
        result.update(state="failed", reason="result_too_large_no_retry", model=MODEL)
    (WORK / "result.json").write_bytes(canonical(result))
    print("SHADOW inference_status=" + result["state"])


def finish():
    check_context()
    ctx = json.loads((WORK / "request.json").read_bytes())
    data, sha = read_state()
    matches = [s for s in data["slots"] if s["post_id"] == ctx["post_id"]]
    if (len(matches) != 1 or matches[0]["state"] != "reserved"
            or matches[0]["worker_run"] != os.environ["GITHUB_RUN_ID"] or matches[0]["digest"] != ctx["digest"]):
        raise ShadowRejected("result_claim_mismatch")
    path = WORK / "result.json"
    result = json.loads(path.read_bytes()) if path.exists() else {"state":"failed", "reason":"no_result_no_retry"}
    entry = matches[0]
    raw_result = canonical(result)
    if len(raw_result) > MAX_RESULT_BYTES:
        raise ShadowRejected("result_file_too_large_no_retry")
    encrypted_result = cipher(os.environ.get("METACULUS_TOKEN", "")).encrypt(raw_result)
    if len(encrypted_result) > MAX_RESULT_CIPHER_BYTES:
        raise ShadowRejected("result_cipher_too_large_no_retry")
    entry["result"] = encrypted_result.decode()
    entry["state"] = result["state"] if result["state"] in ("success", "failed") else "failed"
    write_state(data, sha)
    print("SHADOW_SUMMARY " + json.dumps({"attempts":len(data["slots"]),
        "completed":sum(s["state"]=="success" for s in data["slots"]),
        "reserved_usd":str(len(data["slots"])*RESERVE_PER_ATTEMPT_USD), "last_state":entry["state"], "publishes":0}))
    for name in ("request.json", "result.json"):
        (WORK / name).unlink(missing_ok=True)


def main():
    command = sys.argv[1:]
    try:
        if command == ["prepare"]: prepare()
        elif command == ["infer"]: infer()
        elif command == ["finish"]: finish()
        else: raise ShadowRejected("invalid_command")
    except ShadowRejected as error:
        # Closing/stale questions are expected exclusions, not broken bot runs.
        # All diagnostic values below are fixed codes, never provider content.
        expected = {"snapshot_not_prospectively_eligible", "question_no_longer_open",
                    "question_closed", "question_not_open_at_capture",
                    "snapshot_expired_before_reservation", "inference_permit_stale",
                    "capture_too_large_no_truncation", "slot_metadata_too_large_no_claim",
                    "prompt_too_large_no_truncation", "request_too_large_no_truncation"}
        if str(error) in expected:
            print("SHADOW skipped_no_retry reason=" + str(error))
            return 0
        print("SHADOW stopped_without_retry; inspect scoped offline diagnostics")
        return 1
    except Exception:
        print("SHADOW stopped_without_retry; inspect scoped offline diagnostics")
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())


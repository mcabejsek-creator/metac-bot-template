"""Read-only daily report for the finite shadow experiment.

The ledger is never modified. Only already recorded candidate successes need a
bounded question-status GET; scoring uses the separate offline evaluator.
Plaintext evidence and predictions stay in memory and are never printed/saved.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time

from shadow_capture import REPO, cipher
from shadow_comparison import EXPERIMENT_ID, MAX_ATTEMPTS, RESERVE_PER_ATTEMPT_USD, ShadowRejected
from shadow_evaluate import evaluate_pairs
from shadow_runner import network, read_state

WORKFLOW = ".github/workflows/shadow_report.yaml"
REPORT_PATH = Path("shadow-report.json")
GET_SPACING_SECONDS = 6
READ_COUNTS = ("resolved", "unresolved", "missing_timing", "unscored", "unavailable", "invalid_response")


def utc_time(value):
    """Accept the timestamp forms used by the existing Metaculus review."""
    try:
        if type(value) in (int, float) and math.isfinite(value):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def check_context(environ=None):
    env = os.environ if environ is None else environ
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REPOSITORY") != REPO
            or env.get("GITHUB_REF") != "refs/heads/main" or env.get("GITHUB_RUN_ATTEMPT") != "1"
            or env.get("GITHUB_EVENT_NAME") not in ("schedule", "workflow_dispatch", "push")
            or env.get("GITHUB_WORKFLOW_REF") != REPO + "/" + WORKFLOW + "@refs/heads/main"):
        raise ShadowRejected("report_context_denied")
    if any(env.get(name) for name in (
            "VRCHCAB_OPENAI_TEST_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "PERPLEXITY_API_KEY")):
        raise ShadowRejected("report_model_credentials_denied")


class OutcomeReader:
    """At most 20 fixed GETs, with no retry or publication path."""

    def __init__(self, token, *, transport=None, clock=time.monotonic, sleep=time.sleep):
        if not isinstance(token, str) or len(token) < 20 or any(ch.isspace() for ch in token):
            raise ShadowRejected("report_read_only_key_unavailable")
        self._token = token
        self._transport = transport or network
        self._clock, self._sleep, self._last = clock, sleep, None
        self.get_attempts = 0
        self.counts = Counter({key: 0 for key in READ_COUNTS})

    def read(self, post_id, question_id, current):
        if (type(post_id) is not int or post_id <= 0 or type(question_id) is not int or question_id <= 0
                or self.get_attempts >= MAX_ATTEMPTS):
            raise ShadowRejected("report_request_bound")
        if self._last is not None:
            delay = GET_SPACING_SECONDS - (self._clock() - self._last)
            if delay > 0:
                self._sleep(delay)
        self._last = self._clock()
        self.get_attempts += 1
        try:
            status, _, raw = self._transport("www.metaculus.com", f"/api/posts/{post_id}/", method="GET",
                headers={"Authorization": "Token " + self._token, "Accept": "application/json",
                         "Cache-Control": "no-cache", "User-Agent": "vrchcab-shadow-report-readonly"})
            if status != 200:
                self.counts["unavailable"] += 1
                return None
            post = json.loads(raw)
        except Exception:
            self.counts["unavailable"] += 1
            return None
        question = post.get("question") if isinstance(post, dict) else None
        if (not isinstance(question, dict) or type(post.get("id")) is not int or post["id"] != post_id
                or type(question.get("id")) is not int or question["id"] != question_id
                or question.get("type") != "binary"
                or any(post.get(k) is not None for k in ("conditional", "group_of_questions", "notebook"))):
            self.counts["invalid_response"] += 1
            return None
        if (question.get("status", post.get("status")) != "resolved"
                or post.get("status") not in (None, "resolved")):
            self.counts["unresolved"] += 1
            return None
        resolution = question.get("resolution")
        if resolution not in ("yes", "no"):
            self.counts["unscored"] += 1
            return None
        # Deliberately no fallback to scheduled_close_time: actual independent
        # timestamps are needed to exclude answers after an early closure.
        closed = utc_time(question.get("actual_close_time"))
        resolved = utc_time(question.get("actual_resolve_time"))
        if closed is None or resolved is None or closed > current or resolved > current:
            self.counts["missing_timing"] += 1
            return {"post_id": post_id, "question_id": question_id, "resolution": resolution,
                    "closed_at": None, "resolved_at": None}
        self.counts["resolved"] += 1
        return {"post_id": post_id, "question_id": question_id, "resolution": resolution,
                "closed_at": closed.isoformat(), "resolved_at": resolved.isoformat()}


def build_report(ledger, secret, reader, current):
    slots = ledger.get("slots") if isinstance(ledger, dict) else None
    if (not isinstance(ledger, dict) or ledger.get("experiment") != EXPERIMENT_ID or ledger.get("schema") != 1
            or not isinstance(slots, list) or len(slots) > MAX_ATTEMPTS):
        raise ShadowRejected("report_ledger_invalid")
    crypt = cipher(secret)
    pairs, outcomes, decrypt_failures, seen = [], [], 0, set()
    for slot in slots:
        if not isinstance(slot, dict) or slot.get("state") not in ("reserved", "success", "failed"):
            raise ShadowRejected("report_ledger_slot_invalid")
        post_id, question_id = slot.get("post_id"), slot.get("question_id")
        if (type(post_id) is not int or post_id <= 0 or type(question_id) is not int or question_id <= 0
                or post_id in seen or slot.get("reserved_usd") != str(RESERVE_PER_ATTEMPT_USD)):
            raise ShadowRejected("report_ledger_slot_invalid")
        seen.add(post_id)
        pair = {key: slot.get(key) for key in ("state", "post_id", "question_id", "digest", "source_run")}
        try:
            snapshot = json.loads(crypt.decrypt(slot["snapshot"].encode()))
            result = json.loads(crypt.decrypt(slot["result"].encode())) if slot.get("result") else None
            if (not isinstance(snapshot, dict) or snapshot.get("post_id") != post_id
                    or snapshot.get("question_id") != question_id or (result is not None and not isinstance(result, dict))):
                raise ValueError("invalid_decrypted_shape")
            pair.update(snapshot=snapshot, result=result)
        except Exception:
            decrypt_failures += 1
            pair.update(state="skipped", snapshot=None, result=None)
        pairs.append(pair)
        if (pair["state"] == "success" and isinstance(pair.get("result"), dict)
                and pair["result"].get("state") == "success"
                and pair["snapshot"].get("question_type") == "binary"):
            outcome = reader.read(post_id, question_id, current)
            if outcome is not None:
                outcomes.append(outcome)
    return {"experiment": EXPERIMENT_ID, "generated_at": current.isoformat(),
            "ledger_attempts": len(slots), "reserved_usd": str(len(slots) * RESERVE_PER_ATTEMPT_USD),
            "metaculus_get_attempts": reader.get_attempts, "outcome_read_counts": dict(reader.counts),
            "decrypt_failures": decrypt_failures, "evaluation": evaluate_pairs(pairs, outcomes)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", choices=[str(REPORT_PATH)], default=str(REPORT_PATH))
    args = parser.parse_args(argv)
    try:
        check_context()
        token = os.environ.get("METACULUS_TOKEN", "")
        reader = OutcomeReader(token)
        ledger, _ = read_state()
        report = build_report(ledger, token, reader, datetime.now(timezone.utc))
        text = json.dumps(report, sort_keys=True, allow_nan=False)
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print("SHADOW_REPORT " + text)
        return 0
    except Exception:
        print("SHADOW_REPORT unavailable; no inference, retry or ledger write")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

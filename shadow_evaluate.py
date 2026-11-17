"""Offline paired accuracy diagnostics from frozen records and separate outcomes.

``evaluate_pairs(pairs, outcomes)`` accepts decrypted ledger slots: keep slot
identity/digest fields and replace encrypted ``snapshot``/``result`` strings
with their decoded dictionaries. Outcomes are independently obtained records
with post_id, question_id, resolution ("yes"/"no"), closed_at and resolved_at.
Both outcome timestamps must be timezone-aware ISO strings. Missing timestamps
never fall back to the captured schedule. Do not manufacture outcomes from a
model response or use this evaluator to reforecast old questions.

CLI: python -B shadow_evaluate.py --pairs pairs.json --outcomes outcomes.json
It reads local JSON arrays and prints aggregate diagnostics only. No network,
credentials, live inference, publication or promotion are implemented here.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from shadow_comparison import (
    ENDS_AT, EXPERIMENT_ID, MODEL, ShadowRejected, eligible_time,
    payload_digest, probabilities, request_for, score_pair,
)


def _identity(item):
    if not isinstance(item, dict):
        raise ShadowRejected("invalid_identity")
    values = (item.get("post_id"), item.get("question_id"))
    if any(type(value) is not int or value <= 0 for value in values):
        raise ShadowRejected("invalid_identity")
    return values


def _date(value):
    try:
        if not isinstance(value, str):
            raise ValueError()
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise ShadowRejected("invalid_timestamps") from None


def _identity_counts(items):
    counts = Counter()
    for item in items:
        try:
            counts[_identity(item)] += 1
        except ShadowRejected:
            pass
    return counts


def _validated_pair(pair):
    snapshot, result = pair["snapshot"], pair["result"]
    if _identity(snapshot) != _identity(pair):
        raise ShadowRejected("snapshot_identity_mismatch")
    if (snapshot.get("experiment") != EXPERIMENT_ID
            or snapshot.get("question_type") != "binary"
            or snapshot.get("options") != ["Yes", "No"]):
        raise ShadowRejected("outside_binary_experiment")
    if (not isinstance(snapshot.get("head_sha"), str)
            or re.fullmatch(r"[0-9a-f]{40}", snapshot["head_sha"]) is None
            or snapshot.get("run_id") != str(pair.get("source_run"))):
        raise ShadowRejected("invalid_source_provenance")
    if pair.get("digest") != payload_digest(request_for(snapshot["prompt"], snapshot["options"])):
        raise ShadowRejected("input_digest_mismatch")
    if not isinstance(result, dict) or result.get("state") != "success":
        raise ShadowRejected("candidate_not_successful")
    model = result.get("model")
    if not isinstance(model, str) or not (model == MODEL or model.startswith(MODEL + "-")):
        raise ShadowRejected("wrong_candidate_model")
    baseline = probabilities(snapshot["baseline"], 2)
    candidate = probabilities(result["probabilities"], 2)
    captured = _date(snapshot["captured_at"])
    opened = _date(snapshot["opens_at"])
    original_close = _date(snapshot["closes_at"])
    verified = _date(snapshot["baseline_verified_at"])
    started = _date(result["started_at"])
    completed = _date(result["completed_at"])
    effective_close = _date(result["effective_closes_at"])
    if not opened <= captured <= verified <= started <= completed:
        raise ShadowRejected("invalid_time_order")
    if not eligible_time(captured, min(original_close, effective_close), started):
        raise ShadowRejected("late_candidate_start")
    if completed >= ENDS_AT:
        raise ShadowRejected("completed_after_experiment_expiry")
    return baseline, candidate, completed, min(original_close, effective_close)


def evaluate_pairs(pairs, outcomes):
    """Report equal-weight binary Brier/log loss on eligible resolved pairs.

    Failed/unfinished calls consume their original budget reservations but
    contribute no accuracy observation. Duplicate pair/outcome identities are
    excluded rather than silently selecting a favorable record. The returned
    delta is candidate minus baseline, so negative means lower error.
    """
    if not isinstance(pairs, list) or not isinstance(outcomes, list):
        raise ShadowRejected("evaluation_requires_json_arrays")
    counts = dict(total=len(pairs), scored=0, failed=0, pending=0,
                  skipped=0, unresolved=0, excluded=0)
    reasons = Counter()
    pair_counts, outcome_counts = _identity_counts(pairs), _identity_counts(outcomes)
    outcome_map = {}
    for outcome in outcomes:
        try:
            identity = _identity(outcome)
        except ShadowRejected:
            continue
        if outcome_counts[identity] == 1:
            outcome_map[identity] = outcome
    sums = {model: {metric: 0.0 for metric in ("brier", "logloss")}
            for model in ("baseline", "candidate")}

    for pair in pairs:
        try:
            identity = _identity(pair)
            if pair_counts[identity] != 1:
                raise ShadowRejected("duplicate_pair_identity")
            state = pair.get("state")
            if state in ("failed", "reserved", "skipped"):
                counts[{"failed": "failed", "reserved": "pending", "skipped": "skipped"}[state]] += 1
                continue
            if state != "success":
                raise ShadowRejected("invalid_pair_state")
            baseline, candidate, completed, captured_cutoff = _validated_pair(pair)
            if outcome_counts[identity] > 1:
                raise ShadowRejected("duplicate_outcome_identity")
            outcome = outcome_map.get(identity)
            if outcome is None or outcome.get("resolution") is None:
                counts["unresolved"] += 1
                continue
            if outcome["resolution"] not in ("yes", "no"):
                raise ShadowRejected("unsupported_resolution")
            if not outcome.get("closed_at") or not outcome.get("resolved_at"):
                raise ShadowRejected("missing_outcome_timing")
            cutoff = min(captured_cutoff, _date(outcome["closed_at"]), _date(outcome["resolved_at"]))
            if completed >= cutoff:
                raise ShadowRejected("candidate_completed_after_cutoff")
            scores = score_pair(baseline, candidate, 0 if outcome["resolution"] == "yes" else 1)
        except ShadowRejected as error:
            counts["excluded"] += 1
            reasons[str(error)] += 1
            continue
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            counts["excluded"] += 1
            reasons["malformed_pair"] += 1
            continue
        counts["scored"] += 1
        for model in sums:
            for metric in sums[model]:
                sums[model][metric] += scores[model][metric]

    n = counts["scored"]
    means = {model: {"mean_" + metric: value / n if n else None
                     for metric, value in metrics.items()}
             for model, metrics in sums.items()}
    means["candidate_minus_baseline"] = {
        metric: means["candidate"][metric] - means["baseline"][metric] if n else None
        for metric in means["baseline"]
    }
    return {
        "experiment": EXPERIMENT_ID,
        "counts": counts,
        "exclusion_reasons": dict(sorted(reasons.items())),
        "scores": means,
        "score_scope": "Equal-weight binary pairs; diagnostic scores, not Metaculus peer score.",
        "delta_direction": "Negative candidate-minus-baseline means lower forecast error.",
        "coverage_scope": "Counts cover supplied records only; uncaptured questions are not inferred.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", required=True, type=Path)
    parser.add_argument("--outcomes", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        pairs = json.loads(args.pairs.read_text(encoding="utf-8"))
        outcomes = json.loads(args.outcomes.read_text(encoding="utf-8"))
        print(json.dumps(evaluate_pairs(pairs, outcomes), sort_keys=True, allow_nan=False))
    except (OSError, ValueError, TypeError):
        print("SHADOW_EVALUATION invalid_local_input; no live request was made")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

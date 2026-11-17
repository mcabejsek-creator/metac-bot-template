import copy
from datetime import datetime, timedelta, timezone
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout

import shadow_comparison as policy
from shadow_evaluate import evaluate_pairs, main


N = datetime(2026, 11, 18, tzinfo=timezone.utc)


def stamp(minutes=0):
    return (N + timedelta(minutes=minutes)).isoformat()


def pair(post_id=1):
    snapshot = {
        "experiment": policy.EXPERIMENT_ID, "post_id": post_id,
        "question_id": post_id + 100, "question_type": "binary",
        "options": ["Yes", "No"], "prompt": "FROZEN PRIVATE EVIDENCE",
        "baseline": [.7, .3], "captured_at": stamp(), "opens_at": stamp(-60),
        "closes_at": stamp(120), "baseline_verified_at": stamp(1),
        "head_sha": "a" * 40, "run_id": "999",
    }
    return {
        "post_id": post_id, "question_id": post_id + 100,
        "state": "success", "source_run": 999,
        "digest": policy.payload_digest(policy.request_for(snapshot["prompt"], snapshot["options"])),
        "snapshot": snapshot,
        "result": {
            "state": "success", "model": policy.MODEL, "probabilities": [.8, .2],
            "started_at": stamp(2), "completed_at": stamp(3),
            "effective_closes_at": stamp(120),
        },
    }


def outcome(post_id=1, resolution="yes"):
    return {
        "post_id": post_id, "question_id": post_id + 100,
        "resolution": resolution, "closed_at": stamp(120), "resolved_at": stamp(180),
    }


class ShadowEvaluationTests(unittest.TestCase):
    def test_known_paired_binary_scores_and_negative_improvement_delta(self):
        report = evaluate_pairs([pair()], [outcome()])
        self.assertEqual(report["counts"]["scored"], 1)
        self.assertAlmostEqual(report["scores"]["baseline"]["mean_brier"], .09)
        self.assertAlmostEqual(report["scores"]["candidate"]["mean_brier"], .04)
        self.assertAlmostEqual(report["scores"]["candidate"]["mean_logloss"], -math.log(.8))
        self.assertAlmostEqual(report["scores"]["candidate_minus_baseline"]["mean_brier"], -.05)
        self.assertNotIn("FROZEN PRIVATE EVIDENCE", json.dumps(report))

    def test_no_resolution_is_not_counted_as_prediction_success(self):
        for outcomes in ([], [outcome(resolution=None)]):
            report = evaluate_pairs([pair()], outcomes)
            self.assertEqual(report["counts"]["scored"], 0)
            self.assertEqual(report["counts"]["unresolved"], 1)
            self.assertIsNone(report["scores"]["candidate"]["mean_brier"])

    def test_failed_pending_and_skipped_records_are_not_accuracy_samples(self):
        pairs = [pair(i) for i in range(1, 5)]
        for row, state in zip(pairs, ("failed", "reserved", "skipped", "success")):
            row["state"] = state
        report = evaluate_pairs(pairs, [outcome(4)])
        self.assertEqual(report["counts"], {
            "total": 4, "scored": 1, "failed": 1, "pending": 1,
            "skipped": 1, "unresolved": 0, "excluded": 0,
        })

    def test_early_independent_close_or_resolution_excludes_late_pair(self):
        for changed in ("closed_at", "resolved_at"):
            with self.subTest(changed=changed):
                resolved = outcome()
                resolved[changed] = stamp(3)
                report = evaluate_pairs([pair()], [resolved])
                self.assertEqual(report["counts"]["scored"], 0)
                self.assertEqual(report["exclusion_reasons"], {"candidate_completed_after_cutoff": 1})

    def test_fresh_prepare_close_is_honored_even_if_later_schedule_moves(self):
        row = pair()
        row["result"]["effective_closes_at"] = stamp(3)
        report = evaluate_pairs([row], [outcome()])
        self.assertEqual(report["exclusion_reasons"], {"candidate_completed_after_cutoff": 1})

    def test_missing_independent_timestamps_never_fall_back_to_frozen_schedule(self):
        for missing in ("closed_at", "resolved_at"):
            resolved = outcome()
            del resolved[missing]
            report = evaluate_pairs([pair()], [resolved])
            self.assertEqual(report["exclusion_reasons"], {"missing_outcome_timing": 1})

    def test_invalid_and_stale_time_order_excluded(self):
        changes = [
            ("started_at", stamp(.5), "invalid_time_order"),
            ("started_at", stamp(16), "invalid_time_order"),
            ("completed_at", "2026-11-18T00:03:00", "invalid_timestamps"),
            ("completed_at", policy.ENDS_AT.isoformat(), "completed_after_experiment_expiry"),
        ]
        for key, value, reason in changes:
            row = pair()
            row["result"][key] = value
            report = evaluate_pairs([row], [outcome()])
            self.assertEqual(report["exclusion_reasons"], {reason: 1})
        row = pair()
        row["result"].update(started_at=stamp(16), completed_at=stamp(17))
        self.assertEqual(evaluate_pairs([row], [outcome()])["exclusion_reasons"], {"late_candidate_start": 1})

    def test_wrong_model_tampered_input_and_multiple_choice_are_excluded(self):
        cases = [
            ("model", "other", "wrong_candidate_model"),
            ("probabilities", [.2, .2], "invalid_distribution"),
        ]
        for key, value, reason in cases:
            row = pair()
            row["result"][key] = value
            self.assertEqual(evaluate_pairs([row], [outcome()])["exclusion_reasons"], {reason: 1})
        row = pair()
        row["snapshot"]["prompt"] += " NEW EVIDENCE"
        self.assertEqual(evaluate_pairs([row], [outcome()])["exclusion_reasons"], {"input_digest_mismatch": 1})
        row = pair()
        row["snapshot"]["question_type"] = "multiple_choice"
        self.assertEqual(evaluate_pairs([row], [outcome()])["exclusion_reasons"], {"outside_binary_experiment": 1})

    def test_duplicates_excluded_instead_of_selecting_better_prediction(self):
        row = pair()
        duplicate = copy.deepcopy(row)
        duplicate["result"]["probabilities"] = [1, 0]
        report = evaluate_pairs([row, duplicate], [outcome()])
        self.assertEqual(report["exclusion_reasons"], {"duplicate_pair_identity": 2})
        report = evaluate_pairs([row], [outcome(), outcome(resolution="no")])
        self.assertEqual(report["exclusion_reasons"], {"duplicate_outcome_identity": 1})

    def test_identity_and_annulled_outcomes_do_not_produce_scores(self):
        row = pair()
        row["snapshot"]["question_id"] = 999
        self.assertEqual(evaluate_pairs([row], [outcome()])["exclusion_reasons"], {"snapshot_identity_mismatch": 1})
        self.assertEqual(evaluate_pairs([pair()], [outcome(resolution="annulled")])["exclusion_reasons"], {"unsupported_resolution": 1})

    def test_equal_weight_mean_uses_same_resolved_pairs_for_both_models(self):
        one, two, unresolved = pair(1), pair(2), pair(3)
        report = evaluate_pairs([one, two, unresolved], [outcome(1), outcome(2, "no")])
        self.assertEqual(report["counts"]["scored"], 2)
        self.assertEqual(report["counts"]["unresolved"], 1)
        self.assertAlmostEqual(report["scores"]["baseline"]["mean_brier"], (.09+.49)/2)
        self.assertAlmostEqual(report["scores"]["candidate"]["mean_brier"], (.04+.64)/2)

    def test_cli_local_files_only_emits_sanitized_aggregate(self):
        with tempfile.TemporaryDirectory() as directory:
            pairs, outcomes = Path(directory)/"pairs.json", Path(directory)/"outcomes.json"
            pairs.write_text(json.dumps([pair()]))
            outcomes.write_text(json.dumps([outcome()]))
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = main(["--pairs", str(pairs), "--outcomes", str(outcomes)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stream.getvalue())["counts"]["scored"], 1)
            self.assertNotIn("FROZEN PRIVATE EVIDENCE", stream.getvalue())


if __name__ == "__main__":
    unittest.main()

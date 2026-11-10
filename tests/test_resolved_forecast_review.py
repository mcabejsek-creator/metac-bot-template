"""Synthetic fixtures only; no real forecast generation, network, or SDK imports."""

import copy
from datetime import datetime, timedelta, timezone
import json
import types
import unittest
from unittest.mock import patch
import urllib.error

import resolved_forecast_review as review


NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)
BASE = datetime(2026, 8, 1, tzinfo=timezone.utc)


def forecast(start, probability, *, end=None, question_id=1, author_id=review.EXPECTED_USER_ID):
    return {"question_id": question_id, "author_id": author_id, "start_time": start.timestamp(),
            "end_time": end.timestamp() if end else None, "forecast_values": [1 - probability, probability]}


def post(identifier=1, *, start=BASE, probability=0.9, outcome="yes"):
    return {"id": identifier + 1000, "projects": {"default_project": {"id": 33121}},
        "question": {"id": identifier, "type": "binary", "status": "resolved", "resolution": outcome,
            "title": "PRIVATE QUESTION TEXT MUST NOT BE EXPORTED",
            "spot_scoring_time": (start + timedelta(hours=1)).isoformat(),
            "actual_close_time": (start + timedelta(hours=2)).isoformat(),
            "actual_resolve_time": (start + timedelta(hours=3)).isoformat(),
            "my_forecasts": {"history": [forecast(start, probability, question_id=identifier)],
                             "score_data": {"spot_peer_score": 3.5, "peer_score": -1.5}}}}


class ReaderTests(unittest.TestCase):
    def build(self, replies):
        calls = []
        queue = iter(replies)

        class Reply:
            status = 200
            def __init__(self, data): self.data = data
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return json.dumps(self.data).encode()

        class Opener:
            def open(self, request, timeout):
                calls.append((request, timeout))
                value = next(queue)
                if isinstance(value, Exception): raise value
                return Reply(value)

        return review.ResolvedReviewReader("fake-test-token", opener=Opener(), clock=lambda: 0,
                                           sleep=lambda _: None), calls

    def test_only_bounded_own_gets_and_two_empty_page_termination(self):
        reader, calls = self.build([
            {"id": review.EXPECTED_USER_ID, "username": "Vrchcab"},
            {"results": [post()], "next": "more"},
            {"results": [], "next": "more"}, {"results": [], "next": "more"}])
        posts, complete = reader.read()
        self.assertTrue(complete)
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(calls), 4)
        for request, timeout in calls:
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["Authorization"], "Token fake-test-token")
            self.assertEqual(timeout, 18)
        self.assertIn("forecaster_id=307558", calls[1][0].full_url)
        self.assertIn("statuses=resolved", calls[1][0].full_url)
        self.assertIn("include_descriptions=false", calls[1][0].full_url)

    def test_wrong_identity_stops_before_inventory(self):
        reader, calls = self.build([{"id": 123, "username": "other-bot"}])
        with self.assertRaises(review.ReviewUnavailable): reader.read()
        self.assertEqual(len(calls), 1)

    def test_no_retry_on_rate_limit_and_redirect_rejected(self):
        error = urllib.error.HTTPError("https://www.metaculus.com/api/users/me/", 429, "sensitive", {}, None)
        reader, calls = self.build([error])
        with self.assertRaises(review.ReviewUnavailable) as caught: reader.read()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("sensitive", str(caught.exception))
        with self.assertRaises(review.ReviewUnavailable):
            review._NoRedirect().redirect_request(None, None, 307, "", {}, "https://other.invalid")

    def test_page_bound_marks_inventory_partial(self):
        reader, calls = self.build([{"id": review.EXPECTED_USER_ID, "username": "Vrchcab"},
                                   {"results": [post()], "next": "more"},
                                   {"results": [post(2)], "next": "more"}])
        with patch.object(review, "MAX_PAGES", 2):
            posts, complete = reader.read()
        self.assertFalse(complete)
        self.assertEqual(len(calls), 3)
        report = review.build_report(posts, inventory_complete=complete, now=NOW)
        self.assertEqual(report["summary"]["cohorts"]["before_2026_11_10"]["calibration_experiment"]["status"],
                         "withheld_incomplete_inventory")


class EvidenceTests(unittest.TestCase):
    def test_official_spot_forecast_is_used_instead_of_latest(self):
        value = post()
        history = value["question"]["my_forecasts"]["history"]
        update = BASE + timedelta(hours=1, minutes=30)
        history[0]["end_time"] = update.timestamp()
        history.append(forecast(update, 0.1))
        value["question"]["my_forecasts"]["latest"] = history[-1]
        rows, skipped = review.extract_records([value], now=NOW)
        self.assertEqual(rows[0]["probability_yes"], 0.9)
        self.assertEqual(rows[0]["forecast_basis"], "official_spot_time")
        self.assertFalse(skipped)

    def test_closed_early_uses_metaculus_effective_spot_time(self):
        value = post()
        value["question"]["spot_scoring_time"] = (BASE + timedelta(days=1)).isoformat()
        rows, _ = review.extract_records([value], now=NOW)
        self.assertEqual(rows[0]["effective_spot_scoring_time"], review._iso(BASE + timedelta(hours=2)))
        self.assertEqual(rows[0]["probability_yes"], 0.9)

    def test_missing_spot_or_withdrawn_forecast_is_not_calibration_evidence(self):
        missing, withdrawn = post(), post(2)
        missing["question"]["spot_scoring_time"] = None
        withdrawn["question"]["my_forecasts"]["history"][0]["end_time"] = (BASE + timedelta(minutes=30)).timestamp()
        rows, _ = review.extract_records([missing, withdrawn], now=NOW)
        self.assertTrue(all(row["probability_yes"] is None for row in rows))
        self.assertEqual(len(rows), 2)  # official score evidence is still retained

    def test_nonresolved_other_user_and_postresolution_forecasts_excluded(self):
        open_post, foreign, late = post(1), post(2), post(3)
        open_post["question"]["status"] = "closed"
        foreign["question"]["my_forecasts"]["history"][0]["author_id"] = 7
        late["question"]["my_forecasts"]["history"][0]["start_time"] = (BASE + timedelta(days=2)).timestamp()
        rows, excluded = review.extract_records([open_post, foreign, late], now=NOW)
        self.assertFalse(rows)
        self.assertEqual(excluded, {"not_resolved": 1, "other_user_history": 1, "no_forecast_before_resolution": 1})

    def test_cohorts_are_separate_and_output_contains_no_private_text(self):
        data = [post(), post(2, start=datetime(2026, 11, 10, 2, tzinfo=timezone.utc))]
        report = review.build_report(data, now=NOW)
        cohorts = report["summary"]["cohorts"]
        self.assertEqual(cohorts["before_2026_11_10"]["resolved_forecasted_questions"], 1)
        self.assertEqual(cohorts["from_2026_11_10"]["resolved_forecasted_questions"], 1)
        self.assertNotIn("PRIVATE QUESTION", json.dumps(report))
        self.assertFalse(report["summary"]["production_calibration_changed"])

    def test_calibration_bins_count_boundary_probabilities_once(self):
        report = review.build_report([post(i + 1, probability=p) for i, p in enumerate([0, .2, .4, .6, .8, 1])], now=NOW)
        bins = report["summary"]["all_periods"]["binary_reliability_bins"]
        self.assertEqual(sum(item["count"] for item in bins), 6)

    def test_group_leaves_and_duplicate_post_pages_are_deduplicated(self):
        a, b = post(1), post(2)
        grouped = {"id": 1001, "group_of_questions": {"questions": [a["question"], b["question"]]}}
        rows, _ = review.extract_records([grouped, grouped], now=NOW)
        self.assertEqual(len(rows), 2)


class CalibrationTests(unittest.TestCase):
    def records(self):
        posts = [post(i + 1, start=BASE + timedelta(days=i), probability=0.9,
                      outcome="yes" if i % 2 else "no") for i in range(60)]
        return review.extract_records(posts, now=NOW)[0]

    def test_alpha_selected_only_on_train_then_evaluated_on_held_out(self):
        rows = self.records()
        result = review.calibration_experiment(rows, inventory_complete=True)
        self.assertEqual(result["selected_alpha_on_training_only"], 0.5)
        self.assertEqual(result["status"], "candidate_improved_on_holdout")
        self.assertEqual(result["train_count"], 42)
        self.assertEqual(result["test_count"], 18)
        changed = copy.deepcopy(rows)
        for row in changed[42:]: row["outcome_yes"] = 1
        alternate = review.calibration_experiment(changed, inventory_complete=True)
        self.assertEqual(alternate["selected_alpha_on_training_only"], 0.5)
        self.assertEqual(alternate["status"], "no_holdout_improvement")
        self.assertFalse(result["production_calibration_changed"])

    def test_outcomes_unavailable_at_test_start_cannot_train_calibration(self):
        rows = self.records()
        for row in rows[:42]: row["resolved_at"] = review._iso(NOW)
        result = review.calibration_experiment(rows, inventory_complete=True)
        self.assertEqual(result["status"], "insufficient_chronologically_available_training_data")
        self.assertEqual(result["train_count"], 0)

    def test_shared_group_post_does_not_cross_training_test_boundary(self):
        rows = self.records()
        rows[0]["post_id"] = rows[-1]["post_id"]
        result = review.calibration_experiment(rows, inventory_complete=True)
        self.assertEqual(result["train_count"], 41)
        self.assertEqual(result["training_excluded_for_outcome_timing_or_shared_post"], 1)

    def test_no_experiment_when_data_small_or_inventory_incomplete(self):
        self.assertEqual(review.calibration_experiment(self.records()[:5], inventory_complete=True)["status"],
                         "insufficient_resolved_binary_forecasts")
        self.assertEqual(review.calibration_experiment(self.records(), inventory_complete=False)["status"],
                         "withheld_incomplete_inventory")


if __name__ == "__main__":
    unittest.main()

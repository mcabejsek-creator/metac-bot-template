"""Offline operator-only public post selection; six synthetic paginated GETs.

The real manual workflow is default-off and requires a distinct explicit
operator choice before a single public post ID may be printed.
"""

from pathlib import Path

from .support import OfflineTestCase
from .test_automatic_one_selection import NOW, atomic
from bounded_open_question_selection import IncompleteQuestionScan
from manual_next_candidate_preview import preview_operator_candidate
from tools.preflight_tournament_inventory_readonly import inspect, FUTUREEVAL, MINIBENCH


ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    def _post_json_to_questions_while_handling_groups(self, post, mode):
        assert mode == "unpack_subquestions"
        return [post["_synthetic_object"]]


def wrap(q):
    return {"question": {"status": "open",
                         "my_forecasts": {"history": ([{"present": True}]
                                                        if q.already_forecasted else [])}},
            "_synthetic_object": q}


class FakePages:
    def __init__(self, first=None, second=None, *, broken_second=False):
        self.first = first or []
        self.second = second or []
        self.calls = []
        self.broken_second = broken_second

    def __call__(self, tournament, offset):
        self.calls.append((tournament, offset))
        if self.broken_second and tournament == MINIBENCH:
            raise IncompleteQuestionScan("Synthetic second tournament outage")
        rows = self.first if tournament == FUTUREEVAL else self.second
        return ([wrap(q) for q in rows] if offset == 0 else [], True)


class OperatorNextCandidateTests(OfflineTestCase):
    def test_new_public_id_requires_complete_two_tournament_inventory(self):
        season = atomic(post_id=999101, qid=900101, hours_left=6)
        mini = atomic(post_id=999102, qid=900102, hours_left=4)
        old = atomic(post_id=999103, qid=900103, hours_left=2, prior=True)
        pages = FakePages(first=[season, old], second=[mini])
        report = inspect(
            FakeClient(), pages, now=NOW, preview_best_candidate=True
        )
        candidate = report["operator_next_public_candidate"]
        self.assertEqual(candidate["public_post_id"], mini.id_of_post)
        self.assertEqual(candidate["public_question_url"],
                         f"https://www.metaculus.com/questions/{mini.id_of_post}/")
        self.assertEqual(candidate["competition"], "MiniBench")
        self.assertEqual(candidate["question_type"], mini.question_type)
        self.assertTrue(candidate["candidate_found"])
        self.assertTrue(candidate["operator_must_reverify_before_any_live_post"])
        self.assertFalse(candidate["publication_authorized"])
        self.assertEqual(candidate["model_calls"], 0)
        self.assertEqual(candidate["post_requests"], 0)
        self.assertEqual(len(pages.calls), 6)
        self.assertEqual(report["authenticated_get_attempts"], 6)
        self.assertTrue(report["completed_both_inventories"])
        import json
        serial = json.dumps(report)
        self.assertNotIn(str(mini.id_of_question), serial)
        self.assertNotIn(str(season.id_of_question), serial)
        self.assertNotIn(str(old.id_of_question), serial)
        self.assertNotIn(str(season.id_of_post), serial)
        self.assertNotIn(str(old.id_of_post), serial)
        self.assertNotIn("present", serial)

    def test_default_counts_only_never_disclose_any_public_post_id(self):
        season = atomic(post_id=999201, qid=900201, hours_left=6)
        pages = FakePages(first=[season])
        report = inspect(FakeClient(), pages, now=NOW)
        self.assertNotIn("operator_next_public_candidate", report)
        self.assertEqual(report["competition_counts"]["Fall FutureEval 2026"]
                         ["no_forecast_in_token_history"], 1)
        import json
        self.assertNotIn(str(season.id_of_post), json.dumps(report))

    def test_no_safe_candidates_explicitly_returns_none_without_public_id(self):
        old = atomic(post_id=999301, qid=900301, hours_left=3, prior=True)
        pages = FakePages(first=[old])
        report = inspect(FakeClient(), pages, now=NOW,
                         preview_best_candidate=True)
        candidate = report["operator_next_public_candidate"]
        self.assertFalse(candidate["candidate_found"])
        self.assertNotIn("public_post_id", candidate)
        self.assertNotIn("public_question_url", candidate)
        self.assertFalse(candidate["publication_authorized"])

    def test_second_tournament_failure_does_not_leak_selected_first_id(self):
        season = atomic(post_id=999401, qid=900401, hours_left=5)
        pages = FakePages(first=[season], broken_second=True)
        with self.assertRaises(IncompleteQuestionScan):
            inspect(FakeClient(), pages, now=NOW, preview_best_candidate=True)
        self.assertEqual(pages.calls, [
            (FUTUREEVAL, 0), (FUTUREEVAL, 100), (FUTUREEVAL, 200),
            (MINIBENCH, 0),
        ])

    def test_public_preview_is_exclusive_to_operator_post_id_inspection(self):
        pages = FakePages()
        with self.assertRaises(IncompleteQuestionScan):
            inspect(FakeClient(), pages, now=NOW, candidate_id=999401,
                    preview_best_candidate=True)
        self.assertEqual(pages.calls, [])

    def test_invalid_preview_mode_denies_before_network(self):
        for bad in (None, 1, "YES", [], object()):
            pages = FakePages()
            with self.subTest(bad=bad), self.assertRaises(IncompleteQuestionScan):
                inspect(FakeClient(), pages, now=NOW, preview_best_candidate=bad)
            self.assertEqual(pages.calls, [])

    def test_under_90_minutes_not_selected(self):
        close = atomic(post_id=999501, qid=900501, hours_left=1)
        after = atomic(post_id=999502, qid=900502, hours_left=5)
        result = preview_operator_candidate([close, after], [], now=NOW)
        self.assertEqual(result["public_post_id"], after.id_of_post)
        self.assertFalse(result["publication_authorized"])

    def test_existing_manual_workflow_requires_separate_opt_in_and_is_not_cron(self):
        wf = (ROOT / ".github/workflows/readonly_inventory_preflight_manual.yaml").read_text()
        self.assertIn("YES_FIND_ONE_PUBLIC_POST_ID", wf)
        self.assertIn("YES_READ_ONLY_MAX_TEN_GETS", wf)
        # Regression: prior workflow always forwarded the counts-only choice,
        # even when the operator explicitly selected the public candidate.
        self.assertIn("VRCHCAB_READONLY_PREFLIGHT_APPROVED: ${{ inputs.confirmation }}", wf)
        self.assertNotIn('VRCHCAB_READONLY_PREFLIGHT_APPROVED: "YES_READ_ONLY_MAX_TEN_GETS"', wf)
        self.assertIn("default: \"DO_NOT_RUN\"", wf)
        self.assertIn("workflow_dispatch:", wf)
        self.assertNotIn("\n  schedule:", wf)
        self.assertNotIn("\n  push:", wf)
        self.assertNotIn("\n  pull_request:", wf)
        self.assertIn("github.run_attempt == 1", wf)
        self.assertNotIn("contents: write", wf)
        self.assertNotIn("secrets.PERPLEXITY_API_KEY", wf)
        self.assertNotIn("secrets.OPENROUTER_API_KEY", wf)
        self.assertNotIn("APPROVE_ONE_LIVE_PUBLICATION_RUN", wf)

    def test_publisher_remains_manual_and_does_not_use_preview(self):
        main = (ROOT / "main.py").read_text()
        publisher = (ROOT / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        self.assertNotIn("manual_next_candidate_preview", main)
        self.assertNotIn("YES_FIND_ONE_PUBLIC_POST_ID", publisher)
        self.assertNotIn("\n  schedule:", publisher)
        self.assertIn("default: DO_NOT_RUN", publisher)
        self.assertNotIn('parser.add_argument("--automatic-one"', main)


if __name__ == "__main__":
    import unittest
    unittest.main()

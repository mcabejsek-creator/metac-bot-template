"""Synthetic GET-only first-live post history validation, no real Metaculus API."""

from datetime import datetime, timezone
import json
from pathlib import Path
import urllib.error

from .support import OfflineTestCase
from metaculus_one_post_history_readonly import (
    HistoryEvidenceUnavailable, POST_ID, URL, MAX_BYTES,
    SinglePostHistoryReader,
)
from tools.check_known_metaculus_history_readonly import (
    APPROVAL, run as run_manual,
)


NOW = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
TOKEN = "SYNTHETIC_V2_BOT_TOKEN_NOT_REAL"


class FakeReply:
    status = 200

    def __init__(self, value):
        self.body = value if isinstance(value, bytes) else json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, cap):
        return self.body[:cap]


class FakeMetaculus:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.fail = False

    def open(self, request, timeout):
        self.calls.append((request.get_method(), request.full_url, timeout,
                           request.get_header("Authorization")))
        if self.fail:
            raise urllib.error.URLError("Synthetic network failure")
        return FakeReply(self.payload)


def post(history=None, post_id=POST_ID, **changes):
    if history is None:
        history = [{"question_id": 908, "start_time": 123.0}]
    q = {"id": 908, "status": "closed",
         "my_forecasts": {"history": history, "latest": None}}
    value = {"id": post_id, "question": q}
    value.update(changes)
    return value


def build(fake, *, clock=lambda: NOW, post_id=POST_ID):
    return SinglePostHistoryReader(token=TOKEN, opener=fake, clock=clock, post_id=post_id)


class SinglePostHistoryReadOnlyTests(OfflineTestCase):
    def test_exact_one_authenticated_get_and_no_post(self):
        fake = FakeMetaculus(post())
        record = build(fake).read()
        self.assertTrue(record.forecast_present_in_token_history)
        self.assertEqual(record.public_post_id, POST_ID)
        self.assertEqual(record.forecast_history_count, 1)
        self.assertEqual(fake.calls, [("GET", URL, 15, "Token " + TOKEN)])
        report = record.public_report()
        self.assertFalse(report["all_historic_forecasts_reconciled"])
        self.assertFalse(report["github_stuck_run_reconciled"])
        self.assertFalse(report["publication_authorized"])
        self.assertEqual(report["metaculus_post_requests"], 0)
        self.assertEqual(report["authenticated_metaculus_get_attempts"], 1)
        self.assertNotIn(TOKEN, str(report))
        self.assertNotIn('"question_id"', str(report))

    def test_exact_one_authenticated_get_for_second_published_post(self):
        fake = FakeMetaculus(post(post_id=46335))
        report = build(fake, post_id=46335).read().public_report()
        self.assertTrue(report["forecast_present_in_token_history"])
        self.assertEqual(report["checked_public_post_id"], 46335)
        self.assertEqual(fake.calls, [
            ("GET", "https://www.metaculus.com/api/posts/46335/", 15, "Token " + TOKEN)
        ])
        self.assertEqual(report["authenticated_metaculus_get_attempts"], 1)
        self.assertEqual(report["metaculus_post_requests"], 0)
        self.assertNotIn(TOKEN, str(report))

    def test_unapproved_id_fails_before_any_network(self):
        for bad in (None, "46335", True, 46336, -1, 0):
            fake = FakeMetaculus(post())
            with self.subTest(bad=bad), self.assertRaises(HistoryEvidenceUnavailable):
                build(fake, post_id=bad)
            self.assertEqual(fake.calls, [])

    def test_selected_id_mismatch_fails_without_accepting_other_post(self):
        fake = FakeMetaculus(post(post_id=POST_ID))
        with self.assertRaises(HistoryEvidenceUnavailable):
            build(fake, post_id=46335).read()
        self.assertEqual(len(fake.calls), 1)

    def test_history_empty_is_only_absence_of_observed_forecast(self):
        fake = FakeMetaculus(post(history=[]))
        report = build(fake).read().public_report()
        self.assertFalse(report["forecast_present_in_token_history"])
        self.assertEqual(report["observed_forecast_history_records"], 0)
        self.assertFalse(report["absence_is_proof_of_no_prior_post"])
        self.assertFalse(report["all_historic_forecasts_reconciled"])

    def test_multiple_historic_forecasts_counted_without_values(self):
        fake = FakeMetaculus(post(history=[
            {"question_id": 908, "start_time": 12},
            {"question_id": 908, "start_time": 13},
        ]))
        evidence = build(fake).read()
        self.assertEqual(evidence.forecast_history_count, 2)

    def test_missing_hidden_or_null_history_never_treated_as_empty(self):
        for q in (
            {"id": 908},
            {"id": 908, "my_forecasts": None},
            {"id": 908, "my_forecasts": {}},
            {"id": 908, "my_forecasts": {"history": None}},
            {"id": 908, "my_forecasts": {"history": "[]"}},
        ):
            with self.subTest(q=q):
                fake = FakeMetaculus({"id": POST_ID, "question": q})
                with self.assertRaises(HistoryEvidenceUnavailable):
                    build(fake).read()
                self.assertEqual(len(fake.calls), 1)

    def test_wrong_post_id_never_accepts_other_account_or_post(self):
        for changed in ({"id": 3333}, {"id": None}, {"id": str(POST_ID)}):
            with self.subTest(changed=changed):
                with self.assertRaises(HistoryEvidenceUnavailable):
                    build(FakeMetaculus(post(**changed))).read()

    def test_group_conditional_notebook_rejected(self):
        for kind in ("group_of_questions", "conditional", "notebook"):
            with self.subTest(kind=kind):
                fake = FakeMetaculus(post(**{kind: {"unexpected": True}}))
                with self.assertRaises(HistoryEvidenceUnavailable):
                    build(fake).read()

    def test_latest_history_disagreement_fails_closed(self):
        value = post(history=[])
        value["question"]["my_forecasts"]["latest"] = {"question_id": 908}
        with self.assertRaises(HistoryEvidenceUnavailable):
            build(FakeMetaculus(value)).read()

    def test_malformed_cross_question_history_fails_closed(self):
        variants = [
            [None],
            ["secret"],
            [{"question_id": 123}],
            [{"question_id": "908"}],
        ]
        for history in variants:
            with self.subTest(history=history):
                with self.assertRaises(HistoryEvidenceUnavailable):
                    build(FakeMetaculus(post(history=history))).read()

    def test_bad_json_or_size_limit_blocks_without_retry(self):
        for payload in (b"not-json", b"z"*(MAX_BYTES+1)):
            fake = FakeMetaculus(payload)
            with self.subTest(size=len(payload)), self.assertRaises(HistoryEvidenceUnavailable):
                build(fake).read()
            self.assertEqual(len(fake.calls), 1)

    def test_network_error_never_retries(self):
        fake = FakeMetaculus(post())
        fake.fail = True
        with self.assertRaises(HistoryEvidenceUnavailable):
            build(fake).read()
        self.assertEqual(len(fake.calls), 1)

    def test_invalid_clock_denied(self):
        with self.assertRaises(HistoryEvidenceUnavailable):
            build(FakeMetaculus(post()), clock=lambda: datetime(2026, 11, 8)).read()

    def test_empty_or_invalid_token_never_contacts_api(self):
        for token in ("", " ", None, " token ", "bad token"):
            with self.subTest(token=token):
                fake = FakeMetaculus(post())
                with self.assertRaises(HistoryEvidenceUnavailable):
                    SinglePostHistoryReader(token=token, opener=fake)
                self.assertEqual(fake.calls, [])

    def test_manual_gate_denies_before_even_one_get(self):
        for permission in (None, "", "DO_NOT_RUN", "YES", True):
            fake = FakeMetaculus(post())
            with self.subTest(permission=permission), self.assertRaises(HistoryEvidenceUnavailable):
                run_manual(env={"VRCHCAB_ONE_HISTORY_GET_APPROVAL": permission,
                                "VRCHCAB_ONE_HISTORY_POST_ID": "46347"},
                           reader=build(fake))
            self.assertEqual(fake.calls, [])

    def test_manual_positive_approval_is_exactly_one_get(self):
        fake = FakeMetaculus(post())
        summary = run_manual(
            env={"VRCHCAB_ONE_HISTORY_GET_APPROVAL": APPROVAL,
                 "VRCHCAB_ONE_HISTORY_POST_ID": "46347"}, reader=build(fake)
        )
        self.assertTrue(summary["forecast_present_in_token_history"])
        self.assertFalse(summary["publication_authorized"])
        self.assertEqual(len(fake.calls), 1)

    def test_manual_post_id_must_be_exact_and_matches_reader(self):
        for bad in (None, "", "DO_NOT_RUN", "46336", "046335", 46335, True):
            fake = FakeMetaculus(post(post_id=46335))
            with self.subTest(bad=bad), self.assertRaises(HistoryEvidenceUnavailable):
                run_manual(
                    env={"VRCHCAB_ONE_HISTORY_GET_APPROVAL": APPROVAL,
                         "VRCHCAB_ONE_HISTORY_POST_ID": bad},
                    reader=build(fake, post_id=46335),
                )
            self.assertEqual(fake.calls, [])
        fake = FakeMetaculus(post(post_id=46335))
        with self.assertRaises(HistoryEvidenceUnavailable):
            run_manual(
                env={"VRCHCAB_ONE_HISTORY_GET_APPROVAL": APPROVAL,
                     "VRCHCAB_ONE_HISTORY_POST_ID": "46335"},
                reader=build(fake, post_id=46347),
            )
        self.assertEqual(fake.calls, [])

    def test_manual_approved_second_post_is_one_get(self):
        fake = FakeMetaculus(post(post_id=46335))
        report = run_manual(
            env={"VRCHCAB_ONE_HISTORY_GET_APPROVAL": APPROVAL,
                 "VRCHCAB_ONE_HISTORY_POST_ID": "46335"},
            reader=build(fake, post_id=46335),
        )
        self.assertEqual(report["checked_public_post_id"], 46335)
        self.assertTrue(report["forecast_present_in_token_history"])
        self.assertEqual(len(fake.calls), 1)

    def test_workflow_is_manual_only_default_off(self):
        wf = (ROOT / ".github/workflows/manual_one_post_history_readonly.yaml").read_text()
        self.assertIn("workflow_dispatch:", wf)
        self.assertIn("default: DO_NOT_RUN", wf)
        self.assertIn("github.run_attempt == 1", wf)
        self.assertIn("contents: read", wf)
        self.assertNotIn("contents: write", wf)
        self.assertNotIn("\n  schedule:", wf)
        self.assertNotIn("\n  push:", wf)
        self.assertNotIn("\n  pull_request:", wf)
        self.assertNotIn("secrets.PERPLEXITY", wf)
        self.assertNotIn("secrets.OPENROUTER", wf)
        self.assertIn("secrets.METACULUS_TOKEN_V2", wf)
        self.assertNotIn("APPROVE_ONE_LIVE_PUBLICATION_RUN", wf)
        self.assertIn('          - "46347"', wf)
        self.assertIn('          - "46335"', wf)
        self.assertIn("(inputs.post_id == '46347' || inputs.post_id == '46335')", wf)
        self.assertIn('VRCHCAB_ONE_HISTORY_POST_ID: ${{ inputs.post_id }}', wf)

    def test_does_not_change_existing_manual_publisher_or_runner(self):
        source = (ROOT / "main.py").read_text()
        publisher = (ROOT / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        self.assertNotIn("metaculus_one_post_history_readonly", source)
        self.assertNotIn("check_known_metaculus_history_readonly", publisher)
        self.assertIn("default: DO_NOT_RUN", publisher)
        self.assertNotIn("\n  schedule:", publisher)
        self.assertNotIn('parser.add_argument("--automatic-one"', source)


if __name__ == "__main__":
    import unittest
    unittest.main()

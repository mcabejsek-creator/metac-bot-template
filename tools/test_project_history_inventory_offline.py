"""Zero-network regression tests for project-scoped forecast inventory."""
import io
import json
import urllib.error
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import project_history_inventory_readonly as audit


def entry(qid=201, author=audit.BOT_ID, start=1794067200):
    return {
        "question_id": qid,
        "forecasts": [
            {"question_id": qid, "author_id": author, "start_time": start}
        ],
    }


class FakeOpener:
    def __init__(self, payload=None, err=None):
        self.calls = 0
        self.payload = payload
        self.err = err

    def open(self, request, timeout=None):
        self.calls += 1
        assert request.get_method() == "GET"
        assert request.has_header("Authorization")
        if self.err:
            raise self.err
        body = json.dumps(self.payload).encode("utf-8")
        response = io.BytesIO(body)
        response.status = 200
        return response


class InventoryTests(unittest.TestCase):
    def test_request_scoped_to_account_project_and_get(self):
        audit.self_check()
        for _, p in audit.PROJECTS:
            req = audit.build_request(p, "x" * 48)
            self.assertEqual(req.get_method(), "GET")
            self.assertNotIn("forecast/", req.full_url)
        self.assertEqual(audit.MAX_REQUESTS, 2)

    def test_counts_only_not_forecasts(self):
        data = {"results": [entry(), {"question_id": 202, "forecasts": []}]}
        report = audit.summarize_response(data)
        self.assertEqual(report["questions_returned"], 2)
        self.assertEqual(report["questions_with_history"], 1)
        self.assertEqual(report["forecast_records"], 1)
        self.assertFalse(report["publication_reconciled"])
        self.assertFalse(report["contains_question_ids_or_forecast_values"])
        serialized = json.dumps(report)
        self.assertNotIn("201", serialized)
        self.assertNotIn("202", serialized)

    def test_empty_response_not_reconciled(self):
        report = audit.summarize_response({"results": []})
        self.assertEqual(report["forecast_records"], 0)
        self.assertFalse(report["publication_reconciled"])
        self.assertIn("not comprehensive", report["coverage"])

    def test_fails_closed_on_malformed_results(self):
        bad = (
            {},
            {"results": None},
            {"results": [entry(), entry()]},
            {"results": [entry(author=1)]},
            {"results": [entry(start=None)]},
            {"results": [{"question_id": 1, "forecasts": None}]},
            {"results": [entry()], "next": "another-page"},
        )
        for case in bad:
            with self.subTest(case=str(case)[:120]):
                with self.assertRaises(RuntimeError):
                    audit.summarize_response(case)

    def test_successful_read_uses_only_one_get(self):
        opener = FakeOpener({"results": [entry()]})
        req = audit.build_request("33121", "x" * 48)
        report = audit.fetch_one(opener, req)
        self.assertEqual(opener.calls, 1)
        self.assertEqual(report["forecast_records"], 1)

    def test_429_aborts_without_retry(self):
        err = urllib.error.HTTPError("https://www.metaculus.com/api/", 429,
                                     "Too Many Requests", {}, None)
        opener = FakeOpener(err=err)
        req = audit.build_request("33121", "x" * 48)
        with self.assertRaisesRegex(RuntimeError, "429"):
            audit.fetch_one(opener, req)
        self.assertEqual(opener.calls, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

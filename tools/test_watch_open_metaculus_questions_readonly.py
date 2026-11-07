"""Offline-only synthetic regressions: two GETs, candidate-only first-page view."""
import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import watch_open_metaculus_questions_readonly as watch


class FakeOpener:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("Unexpected extra request")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        res = io.BytesIO(json.dumps(reply).encode("utf-8"))
        res.status = 200
        return res


class FirstPageWatchTests(unittest.TestCase):
    TOKEN = "x" * 48

    def run_synthetic(self, replies, *, summary_path=""):
        opener = FakeOpener(replies)
        out = io.StringIO()
        with patch.object(watch.urllib.request, "build_opener", return_value=opener), \
             patch.object(watch.time, "sleep"), \
             patch.dict(os.environ, {"METACULUS_TOKEN": self.TOKEN,
                                    "GITHUB_STEP_SUMMARY": summary_path}), \
             contextlib.redirect_stdout(out):
            watch.check()
        report = json.loads(next(line for line in out.getvalue().splitlines() if line.startswith("{")))
        return report, out.getvalue(), opener

    def test_exact_authenticated_get_scope(self):
        watch.self_check()
        for _, project in watch.TARGETS:
            req = watch.make_request(project, self.TOKEN)
            self.assertEqual(req.get_method(), "GET")
            self.assertEqual(req.get_header("Authorization"), "Token " + self.TOKEN)
            self.assertNotIn("acting_user", req.full_url)
            self.assertNotIn("forecast_type", req.full_url)
        self.assertEqual(len(watch.TARGETS), 2)

    def test_aggregate_only_and_no_forecast_status_claim(self):
        report, output, opener = self.run_synthetic([
            {"results": [{"id": 123, "title": "PRIVATE_TITLE"}]},
            {"results": []},
        ])
        self.assertTrue(report["open_candidates_found"])
        self.assertFalse(report["bot_forecast_status_verified"])
        self.assertFalse(report["complete_tournament_inventory"])
        self.assertEqual([x["supported_post_candidates_first_page"] for x in report["projects"]], [1, 0])
        self.assertEqual(report["requests"], 2)
        self.assertNotIn("PRIVATE_TITLE", output)
        self.assertNotIn('"id"', output)
        self.assertTrue(all(req.get_method() == "GET" for req in opener.requests))

    def test_notebooks_are_excluded_from_supported_candidates(self):
        summary = watch.summarize({"results": [{"notebook": {"id": 1}}, {"id": 202}], "next": "https://www.metaculus.com/api/posts/?offset=100"})
        self.assertEqual(summary["supported_post_candidates_first_page"], 1)
        self.assertEqual(summary["notebook_posts_excluded"], 1)
        self.assertTrue(summary["more_pages_indicated"])
        self.assertFalse(summary["global_coverage"])

    def test_zero_first_page_with_next_does_not_imply_global_absence(self):
        report, out, _ = self.run_synthetic([
            {"results": [], "next": "https://www.metaculus.com/api/posts/?offset=100"},
            {"results": [], "next": "https://www.metaculus.com/api/posts/?offset=100"},
        ])
        self.assertFalse(report["open_candidates_found"])
        self.assertFalse(report["complete_tournament_inventory"])
        self.assertTrue(all(x["more_pages_indicated"] for x in report["projects"]))
        self.assertIn("NOT establish absence", out)

    def test_notebook_only_first_page_does_not_alert(self):
        report, out, _ = self.run_synthetic([
            {"results": [{"notebook": {"id": 7}}], "next": "https://www.metaculus.com/api/posts/?offset=100"},
            {"results": [], "next": "https://www.metaculus.com/api/posts/?offset=100"},
        ])
        self.assertFalse(report["open_candidates_found"])
        self.assertNotIn("::warning::", out)
        self.assertEqual(report["projects"][0]["notebook_posts_excluded"], 1)

    def test_step_summary_notebook_counts_and_disclaimer(self):
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".md") as handle:
            _, output, _ = self.run_synthetic([
                {"results": [{"title": "DO_NOT_EXPOSE"}]},
                {"results": []},
            ], summary_path=handle.name)
            with open(handle.name, encoding="utf-8") as doc:
                contents = doc.read()
            self.assertIn("first-page candidate", contents)
            self.assertIn("Incomplete by design", contents)
            self.assertNotIn("DO_NOT_EXPOSE", contents)
            self.assertNotIn("DO_NOT_EXPOSE", output)

    def test_unexpected_project_refused(self):
        with self.assertRaisesRegex(RuntimeError, "Unexpected project"):
            watch.make_request("bot-testing-area", self.TOKEN)

    def test_malformed_data_refused(self):
        for data in ({}, {"results": None}, {"results": [None]}, {"results": [{}] * 101}):
            with self.subTest(payload_len=len(str(data))):
                with self.assertRaises(RuntimeError):
                    watch.summarize(data)

    def test_missing_secret_stops_before_network(self):
        opener = FakeOpener([{"results": []}, {"results": []}])
        with patch.object(watch.urllib.request, "build_opener", return_value=opener), \
             patch.dict(os.environ, {"METACULUS_TOKEN": "", "GITHUB_STEP_SUMMARY": ""}):
            with self.assertRaisesRegex(RuntimeError, "credential"):
                watch.check()
        self.assertEqual(opener.requests, [])

    def test_secret_never_printed_in_successful_run(self):
        token = "SENSITIVE_SYNTHETIC_TOKEN_" + "x" * 40
        opener = FakeOpener([{"results": [{}]}, {"results": []}])
        out = io.StringIO()
        with patch.object(watch.urllib.request, "build_opener", return_value=opener), \
             patch.object(watch.time, "sleep"), \
             patch.dict(os.environ, {"METACULUS_TOKEN": token, "GITHUB_STEP_SUMMARY": ""}), \
             contextlib.redirect_stdout(out):
            watch.check()
        self.assertNotIn(token, out.getvalue())
        self.assertTrue(all(req.get_header("Authorization") == "Token " + token for req in opener.requests))

    def test_http429_stops_without_retry(self):
        exc = urllib.error.HTTPError("https://www.metaculus.com/api/posts/", 429, "Too Many", {}, None)
        opener = FakeOpener([exc, {"results": []}])
        with self.assertRaisesRegex(RuntimeError, "429"):
            watch.fetch_one(opener, "33121", self.TOKEN)
        self.assertEqual(len(opener.requests), 1)

    def test_first_query_error_stops_before_second(self):
        exc = urllib.error.HTTPError("https://www.metaculus.com/api/posts/", 500, "Server", {}, None)
        opener = FakeOpener([exc, {"results": []}])
        with patch.object(watch.urllib.request, "build_opener", return_value=opener), \
             patch.dict(os.environ, {"METACULUS_TOKEN": self.TOKEN,
                                    "GITHUB_STEP_SUMMARY": ""}):
            with self.assertRaisesRegex(RuntimeError, "500"):
                watch.check()
        self.assertEqual(len(opener.requests), 1)

    def test_no_redirect(self):
        with self.assertRaisesRegex(RuntimeError, "Redirect refused"):
            watch.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.example/")

    def test_at_most_two_gets_with_bot_token(self):
        _, _, opener = self.run_synthetic([{"results": [{}]}, {"results": [{}]}])
        self.assertEqual(len(opener.requests), 2)
        self.assertTrue(all(req.get_method() == "GET" for req in opener.requests))
        self.assertTrue(all(req.get_header("Authorization") == "Token " + self.TOKEN for req in opener.requests))


if __name__ == "__main__":
    unittest.main(verbosity=2)

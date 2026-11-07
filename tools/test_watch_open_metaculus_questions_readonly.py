"""Offline-only synthetic tests for public Metaculus watch (never network)."""
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
            raise AssertionError("Unexpected extra HTTP request")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        result = io.BytesIO(json.dumps(reply).encode("utf-8"))
        result.status = 200
        return result


class PublicWatchTests(unittest.TestCase):
    def test_get_contract_no_token_or_user_history(self):
        watch.self_check()
        for _, project in watch.TARGETS:
            req = watch.make_request(project)
            self.assertEqual(req.get_method(), "GET")
            self.assertFalse(req.has_header("Authorization"))
            self.assertNotIn("acting_user", req.full_url)
        self.assertEqual(len(watch.TARGETS), 2)

    def test_aggregates_only_and_avoids_post_details(self):
        opener = FakeOpener([
            {"results": [{"id": 123, "title": "PRIVATE_TITLE"}]},
            {"results": []},
        ])
        output = io.StringIO()
        with patch.object(watch.urllib.request, "build_opener", return_value=opener), \
             patch.object(watch.time, "sleep"), \
             patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}), \
             contextlib.redirect_stdout(output):
            watch.check()
        report = json.loads(next(line for line in output.getvalue().splitlines() if line.startswith("{")))
        self.assertTrue(report["open_candidates_found"])
        self.assertFalse(report["bot_forecast_status_verified"])
        self.assertEqual([a["open_posts"] for a in report["projects"]], [1, 0])
        self.assertEqual(report["requests"], 2)
        self.assertNotIn("PRIVATE_TITLE", output.getvalue())
        self.assertNotIn("123", output.getvalue())
        self.assertTrue(all(req.get_method() == "GET" for req in opener.requests))

    def test_empty_is_not_a_claim_about_forecasts(self):
        opener = FakeOpener([{"results": []}, {"results": []}])
        out = io.StringIO()
        with patch.object(watch.urllib.request, "build_opener", return_value=opener), \
             patch.object(watch.time, "sleep"), \
             patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}), \
             contextlib.redirect_stdout(out):
            watch.check()
        report = json.loads(next(s for s in out.getvalue().splitlines() if s.startswith("{")))
        self.assertFalse(report["open_candidates_found"])
        self.assertFalse(report["bot_forecast_status_verified"])

    def test_summary_never_contains_untrusted_raw_data(self):
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".md") as handle:
            opener = FakeOpener([{"results": [{"title": "DONT_STORE"}]}, {"results": []}])
            with patch.object(watch.urllib.request, "build_opener", return_value=opener), \
                 patch.object(watch.time, "sleep"), \
                 patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": handle.name}), \
                 contextlib.redirect_stdout(io.StringIO()):
                watch.check()
            with open(handle.name, encoding="utf-8") as document:
                contents = document.read()
            self.assertIn("Fall FutureEval", contents)
            self.assertNotIn("DONT_STORE", contents)
            self.assertIn("not", contents.lower())

    def test_refuses_unknown_project(self):
        with self.assertRaisesRegex(RuntimeError, "Unexpected project"):
            watch.make_request("bot-testing-area")

    def test_detects_pagination_instead_of_false_complete_count(self):
        for data in (
            {"results": [{}], "next": "https://www.metaculus.com/api/posts/?offset=100"},
            {"results": [{}] * watch.FIRST_PAGE_LIMIT},
        ):
            with self.subTest(data_type=str(len(data["results"]))):
                with self.assertRaisesRegex(RuntimeError, "pagination"):
                    watch.summarize(data)

    def test_bad_shapes_are_not_silent_zero(self):
        for data in ({}, {"results": None}, {"results": [None]}, {"results": [{}] * 101}):
            with self.subTest(size=len(str(data))):
                with self.assertRaises(RuntimeError):
                    watch.summarize(data)

    def test_http_429_stops_without_retrying(self):
        error = urllib.error.HTTPError("https://www.metaculus.com/api/posts/", 429, "Too Many", {}, None)
        opener = FakeOpener([error, {"results": []}])
        with self.assertRaisesRegex(RuntimeError, "429"):
            watch.fetch_one(opener, "33121")
        self.assertEqual(len(opener.requests), 1)

    def test_first_query_error_stops_before_second(self):
        error = urllib.error.HTTPError("https://www.metaculus.com/api/posts/", 500, "Server Error", {}, None)
        opener = FakeOpener([error, {"results": []}])
        with patch.object(watch.urllib.request, "build_opener", return_value=opener), \
             patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
            with self.assertRaisesRegex(RuntimeError, "500"):
                watch.check()
        self.assertEqual(len(opener.requests), 1)

    def test_redirect_not_followed(self):
        with self.assertRaisesRegex(RuntimeError, "Redirect refused"):
            watch.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.example/")

    def test_all_requests_without_auth_header(self):
        opener = FakeOpener([{"results": [{}]}, {"results": [{}]}])
        with patch.object(watch.urllib.request, "build_opener", return_value=opener), \
             patch.object(watch.time, "sleep"), \
             patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}), \
             contextlib.redirect_stdout(io.StringIO()):
            watch.check()
        self.assertEqual(len(opener.requests), 2)
        self.assertTrue(all(not request.has_header("Authorization") for request in opener.requests))


if __name__ == "__main__":
    unittest.main(verbosity=2)

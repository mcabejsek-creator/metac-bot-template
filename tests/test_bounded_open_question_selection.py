"""Strictly offline, zero-secret tests of the Vrchcab paginated selector."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.parse
from unittest.mock import Mock, patch

from bounded_open_question_selection import (
    API_BASE, BoundedPostReader, IncompleteQuestionScan, PAGE_SIZE,
    MAX_PAGES_PER_TOURNAMENT, deduplicate_tournaments, scan_open_tournament,
)

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)


def question(qid, *, old=False, state="open", opened=None, closes=None):
    return SimpleNamespace(
        id_of_question=qid, already_forecasted=old, state=state,
        open_time=opened or NOW - timedelta(days=1),
        close_time=closes or NOW + timedelta(days=1),
        api_json={"question": {}},
    )


def post(qid, *, old=False, typ="question", q=None):
    leaf = {"id": qid, "status": "open",
            "my_forecasts": {"history": ([{"synthetic": True}] if old else [])}}
    result = {"id": qid * 10, "_synthetic_questions": [q or question(qid, old=old)]}
    if typ == "group":
        result["group_of_questions"] = {"questions": [leaf]}
    else:
        result["question"] = leaf
    return result


class FakeClient:
    def _post_json_to_questions_while_handling_groups(self, data, mode):
        assert mode == "unpack_subquestions"
        return data["_synthetic_questions"]


class ScanTests(unittest.TestCase):
    def test_first_page_empty_does_not_hide_later_open_question(self):
        calls = []
        pages = {
            0: ([], True),
            100: ([post(1)], True),
            200: ([], False),
        }
        def read(tournament, offset):
            self.assertEqual(tournament, 33121)
            calls.append(offset)
            return pages[offset]
        result = scan_open_tournament(FakeClient(), 33121, read_page=read, now=NOW)
        self.assertEqual([q.id_of_question for q in result], [1])
        self.assertEqual(calls, [0, 100, 200])

    def test_notebook_then_group_with_multiple_subquestions(self):
        group = post(31, typ="group")
        group["group_of_questions"]["questions"].append({
            "id": 32, "status": "open", "my_forecasts": {"history": []}})
        group["_synthetic_questions"] = [question(31), question(32)]
        pages = iter([([{"notebook": {"content": "synthetic"}}], True),
                      ([group], False)])
        result = scan_open_tournament(FakeClient(), "minibench",
                                     read_page=lambda *_: next(pages), now=NOW)
        self.assertEqual([q.id_of_question for q in result], [31, 32])

    def test_conditional_validates_nested_history(self):
        leaf = lambda ident: {
            "id": ident, "status": "open", "my_forecasts": {"history": []}
        }
        conditional = {
            "id": 700,
            "conditional": {
                "condition": leaf(701), "condition_child": leaf(702),
                "question_yes": leaf(703), "question_no": leaf(704),
            },
            "_synthetic_questions": [question(703), question(704)],
        }
        result = scan_open_tournament(
            FakeClient(), 33121, read_page=lambda *_: ([conditional], False), now=NOW,
        )
        self.assertEqual([q.id_of_question for q in result], [703, 704])
        conditional["conditional"]["question_yes"]["my_forecasts"] = None
        with self.assertRaisesRegex(IncompleteQuestionScan, "history"):
            scan_open_tournament(
                FakeClient(), 33121, read_page=lambda *_: ([conditional], False), now=NOW,
            )

    def test_conditional_mixed_histories_fail_closed(self):
        # Pinned SDK chooses yes.my_forecasts OR no.my_forecasts, even when
        # the yes history list is empty. A no-only prior forecast must
        # never be mistaken for an entirely unforecasted conditional.
        def leaf(ident, old=False):
            return {"id": ident, "status": "open",
                    "my_forecasts": {"history": ([{"synthetic": True}] if old else [])}}
        p = {
            "id": 700,
            "conditional": {
                "condition": leaf(701), "condition_child": leaf(702),
                "question_yes": leaf(703), "question_no": leaf(704, old=True),
            },
            "_synthetic_questions": [question(700)],
        }
        with self.assertRaisesRegex(IncompleteQuestionScan, "Conflicting conditional branch histories"):
            scan_open_tournament(FakeClient(), 33121,
                                 read_page=lambda *_: ([p], False), now=NOW)

    def test_actual_six_get_metadata_pattern_replayed_offline(self):
        # The approved Nov 7 read-only audit showed exactly these aggregate
        # post counts: FutureEval [2,0,0] (one notebook + one question),
        # MiniBench [0,0,0]. No real question data is retained here.
        # The scanner should stop after 3+2 GETs, ignoring phantom next.
        pages = {
            (33121, 0): ([{"notebook": {"content": "synthetic"}}, post(141)], True),
            (33121, 100): ([], True),
            (33121, 200): ([], True),
            ("minibench", 0): ([], True),
            ("minibench", 100): ([], True),
        }
        calls = []
        def read_page(tournament, offset):
            calls.append((tournament, offset))
            return pages[(tournament, offset)]
        client = FakeClient()
        future = scan_open_tournament(client, 33121, read_page=read_page, now=NOW)
        mini = scan_open_tournament(client, "minibench", read_page=read_page, now=NOW)
        self.assertEqual([q.id_of_question for q in future], [141])
        self.assertFalse(future[0].already_forecasted)
        self.assertEqual(mini, [])
        self.assertEqual(calls, [
            (33121, 0), (33121, 100), (33121, 200),
            ("minibench", 0), ("minibench", 100),
        ])

    def test_deduplicate_question_ids_and_preserve_forecast_status(self):
        p = post(4, old=True)
        pages = iter([([p], True), ([p, post(5)], False)])
        result = scan_open_tournament(FakeClient(), 33121,
                                     read_page=lambda *_: next(pages), now=NOW)
        self.assertEqual([(q.id_of_question, q.already_forecasted) for q in result],
                         [(4, True), (5, False)])

    def test_expired_and_not_yet_open_questions_are_not_returned(self):
        rows = [
            post(1, q=question(1, closes=NOW - timedelta(seconds=1))),
            post(2, q=question(2, opened=NOW + timedelta(hours=1))),
            post(3),
            post(4, q=question(4, state="closed")),
        ]
        got = scan_open_tournament(FakeClient(), 33121,
                                   read_page=lambda *_: (rows, False), now=NOW)
        self.assertEqual([q.id_of_question for q in got], [3])

    def test_contradictory_forecast_history_must_abort_without_partial_result(self):
        first = post(12, old=False)
        conflict = post(12, old=True)
        pages = iter([([first], True), ([conflict], False)])
        with self.assertRaisesRegex(IncompleteQuestionScan, "Conflicting"):
            scan_open_tournament(FakeClient(), 33121,
                                 read_page=lambda *_: next(pages), now=NOW)

    def test_null_forecast_history_is_not_assumed_unforecasted(self):
        p = post(7)
        p["question"]["my_forecasts"] = None
        with self.assertRaisesRegex(IncompleteQuestionScan, "history"):
            scan_open_tournament(FakeClient(), 33121,
                                 read_page=lambda *_: ([p], False), now=NOW)

    def test_phantom_next_terminates_after_two_empty_pages(self):
        # Django CountlessLimitOffsetPagination uses infinite 'count', so
        # 'next' stays populated after the last available post.
        calls = []
        def endless(_, offset):
            calls.append(offset)
            return [], True
        result = scan_open_tournament(FakeClient(), "minibench",
                                      read_page=endless, now=NOW)
        self.assertEqual(result, [])
        self.assertEqual(calls, [0, PAGE_SIZE])

    def test_single_empty_page_is_not_terminal(self):
        pages = iter([([], True), ([post(11)], True),
                      ([], True), ([], True)])
        result = scan_open_tournament(
            FakeClient(), 33121, read_page=lambda *_: next(pages), now=NOW
        )
        self.assertEqual([q.id_of_question for q in result], [11])

    def test_all_pages_nonempty_aborts_at_fixed_cap(self):
        calls = []
        def endless(_, offset):
            calls.append(offset)
            return [post(offset + 1)], True
        with self.assertRaisesRegex(IncompleteQuestionScan, "fixed GET cap"):
            scan_open_tournament(FakeClient(), "minibench",
                                 read_page=endless, now=NOW)
        self.assertEqual(calls, [i * PAGE_SIZE for i in range(MAX_PAGES_PER_TOURNAMENT)])

    def test_fail_closed_on_parse_error_and_invalid_response(self):
        class Broken:
            def _post_json_to_questions_while_handling_groups(self, *_):
                raise ValueError("Synthetic raw question secret")
        with self.assertRaisesRegex(IncompleteQuestionScan, "SDK failed"):
            scan_open_tournament(Broken(), 33121, read_page=lambda *_: ([post(1)], False), now=NOW)
        with self.assertRaises(IncompleteQuestionScan):
            scan_open_tournament(FakeClient(), 33121, read_page=lambda *_: ([], "maybe"), now=NOW)

    def test_cross_tournament_dedup(self):
        a, b = deduplicate_tournaments([question(1)], [question(1), question(2)])
        self.assertEqual([q.id_of_question for q in a], [1])
        self.assertEqual([q.id_of_question for q in b], [2])
        with self.assertRaises(IncompleteQuestionScan):
            deduplicate_tournaments([question(1, old=True)], [question(1, old=False)])

    def test_unapproved_tournament_and_invalid_page_budget(self):
        with self.assertRaises(IncompleteQuestionScan):
            scan_open_tournament(FakeClient(), 33108, read_page=lambda *_: ([], False))
        with self.assertRaises(IncompleteQuestionScan):
            scan_open_tournament(FakeClient(), 33121, read_page=lambda *_: ([], False),
                                 max_pages=100)


class PinnedSDKCompatibilityTests(unittest.TestCase):
    def test_installed_sdk_parses_synthetic_post_without_network(self):
        # Deliberately exercise the REAL locked SDK 0.2.92, not only a fake.
        from forecasting_tools import MetaculusClient
        client = MetaculusClient(token="synthetic-offline-only")
        self.assertTrue(callable(getattr(client, "_post_json_to_questions_while_handling_groups")))
        raw = {
            "id": 9991,
            "question": {
                "id": 9992, "title": "Synthetic offline test question",
                "type": "binary", "status": "open",
                "open_time": "2026-11-01T00:00:00Z",
                "scheduled_close_time": "2026-11-10T00:00:00Z",
                "my_forecasts": {"history": [], "latest": None},
            },
            "projects": {
                "tournament": [{"slug": "fall-futureeval-2026"}],
                "default_project": {"id": 33121, "visibility": "normal"},
                "category": [],
            },
        }
        result = scan_open_tournament(
            client, 33121, read_page=lambda *_: ([raw], False), now=NOW,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id_of_question, 9992)
        self.assertFalse(result[0].already_forecasted)

    def test_pinned_sdk_unpacks_group_post_subquestions(self):
        from forecasting_tools import MetaculusClient
        def child(qid):
            return {
                "id": qid, "title": "Synthetic group subquestion",
                "type": "binary", "status": "open",
                "open_time": "2026-11-01T00:00:00Z",
                "scheduled_close_time": "2026-11-10T00:00:00Z",
                "my_forecasts": {"history": [], "latest": None},
            }
        raw = {
            "id": 9871,
            "group_of_questions": {
                "questions": [child(9872), child(9873)],
                "fine_print": "Synthetic only",
                "description": "Synthetic only",
                "resolution_criteria": "Synthetic only",
            },
            "projects": {
                "tournament": [{"slug": "fall-futureeval-2026"}],
                "default_project": {"id": 33121, "visibility": "normal"},
                "category": [],
            },
        }
        client = MetaculusClient(token="synthetic-offline-only")
        result = scan_open_tournament(
            client, 33121, read_page=lambda *_: ([raw], False), now=NOW,
        )
        self.assertEqual([q.id_of_question for q in result], [9872, 9873])
        self.assertTrue(all(q.already_forecasted is False for q in result))


    def test_pinned_sdk_parses_synthetic_conditional_without_network(self):
        from forecasting_tools import MetaculusClient
        def part(qid):
            return {
                "id": qid, "title": "Synthetic conditional component",
                "type": "binary", "status": "open",
                "open_time": "2026-11-01T00:00:00Z",
                "scheduled_close_time": "2026-11-10T00:00:00Z",
                "my_forecasts": {"history": [], "latest": None},
            }
        raw = {
            "id": 9871,
            "conditional": {
                "id": 9872, "title": "Synthetic conditional",
                "status": "open",
                "open_time": "2026-11-01T00:00:00Z",
                "scheduled_close_time": "2026-11-10T00:00:00Z",
                "condition": part(9873), "condition_child": part(9874),
                "question_yes": part(9875), "question_no": part(9876),
            },
            "projects": {
                "tournament": [{"slug": "fall-futureeval-2026"}],
                "default_project": {"id": 33121, "visibility": "normal"},
                "category": [],
            },
        }
        client = MetaculusClient(token="synthetic-offline-only")
        scanned = scan_open_tournament(
            client, 33121, read_page=lambda *_: ([raw], False), now=NOW,
        )
        self.assertEqual(len(scanned), 1)
        self.assertEqual(scanned[0].id_of_question, 9872)
        self.assertFalse(scanned[0].already_forecasted)
        self.assertEqual(scanned[0].question_type, "conditional")


class FakeResponse:
    status = 200
    def __init__(self, obj):
        self._body = json.dumps(obj).encode()
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def read(self, limit):
        return self._body[:limit]


class ReadTransportTests(unittest.TestCase):
    def setUp(self):
        self.client = SimpleNamespace(base_url=API_BASE, token="fake-offline-token",
                                      acting_user=None)

    def test_exact_GET_query_and_six_seconds_between_projects(self):
        calls = []
        now = [100.]
        def sleeper(seconds):
            now[0] += seconds
        class Opener:
            def open(self, req, timeout):
                calls.append(req)
                return FakeResponse({"results": [], "next": None})
        reader = BoundedPostReader(self.client, clock=lambda: now[0],
                                   sleep=sleeper, opener=Opener())
        reader(33121, 0)
        reader("minibench", 100)
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(now[0], 106.)
        for req, tournament, offset in zip(calls, ["33121", "minibench"], [0, 100]):
            self.assertEqual(req.get_method(), "GET")
            parsed = urllib.parse.urlsplit(req.full_url)
            params = urllib.parse.parse_qs(parsed.query)
            self.assertEqual((parsed.scheme, parsed.netloc, parsed.path),
                             ("https", "www.metaculus.com", "/api/posts/"))
            self.assertEqual(params["tournaments"], [tournament])
            self.assertEqual(params["offset"], [str(offset)])
            self.assertEqual(params["statuses"], ["open"])
            self.assertEqual(params["limit"], ["100"])
            self.assertNotIn("forecast_type", params)

    def test_http_429_never_retried(self):
        calls = []
        class RateLimited:
            def open(self, req, timeout):
                calls.append(req)
                raise urllib.error.HTTPError(req.full_url, 429, "rate", {}, None)
        reader = BoundedPostReader(self.client, opener=RateLimited())
        with self.assertRaisesRegex(IncompleteQuestionScan, "no retry"):
            reader(33121, 0)
        self.assertEqual(len(calls), 1)

    def test_redirect_and_unexpected_host_rejected(self):
        wrong = SimpleNamespace(base_url="https://other.example/api",
                                token="t", acting_user=None)
        with self.assertRaises(IncompleteQuestionScan):
            BoundedPostReader(wrong)
        with self.assertRaises(IncompleteQuestionScan):
            BoundedPostReader(SimpleNamespace(base_url=API_BASE, token="t",
                                               acting_user=307558))

    def test_malformed_envelope_blocks_publishing(self):
        class Opener:
            def open(self, req, timeout):
                return FakeResponse({"results": [], "next": 100})
        with self.assertRaisesRegex(IncompleteQuestionScan, "next marker"):
            BoundedPostReader(self.client, opener=Opener())(33121, 0)


if __name__ == "__main__":
    # Even if someone later edits these tests, attempts to use a real network
    # socket fail immediately. No real credentials, forecasts or LLM calls.
    import os
    import socket
    for name in ("METACULUS_TOKEN", "METACULUS_TOKEN_V2",
                 "PERPLEXITY_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        os.environ.pop(name, None)
    with patch.object(socket.socket, "connect", side_effect=AssertionError("OFFLINE GUARD")), \
         patch.object(socket.socket, "connect_ex", side_effect=AssertionError("OFFLINE GUARD")), \
         patch("socket.create_connection", side_effect=AssertionError("OFFLINE GUARD")):
        unittest.main(verbosity=2)

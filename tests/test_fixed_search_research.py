"""No-network checks for the unattended fixed-price research path."""

import asyncio
import copy
import hashlib
import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from fixed_search_research import (
    FixedSearchClient, GitHubSearchStorage, MARKER, RESOURCE, PHASES,
    ResearchAttemptUncertain, SearchResearchError, WeeklyBudgetExhausted,
    SEARCH_PRICE_USD, _parse_planned_queries, call_search_once, fixed_search_research,
)


NOW = datetime(2026, 11, 10, 1, 0, tzinfo=timezone.utc)
RESPONSE = {"id": "search-id", "results": [{"title": "Official result", "url": "https://example.org/result",
    "snippet": "On 9 November 2026 the official count was 12.", "date": "2026-11-09"}]}


class MemoryStorage:
    def __init__(self):
        self.state = {"version": 1, "requests": {}}
        self.revision, self.evidence = 0, {}
        self.fail_save = False
        self.fail_cache = False

    def load(self):
        return copy.deepcopy(self.state), str(self.revision)

    def save(self, state, sha):
        if self.fail_save or sha != str(self.revision):
            raise RuntimeError("No durable acknowledgement")
        self.state = copy.deepcopy(state)
        self.revision += 1

    def save_evidence(self, key, evidence):
        if self.fail_cache:
            raise RuntimeError("Lost cache acknowledgement")
        if key in self.evidence:
            raise RuntimeError("Create-if-absent conflict")
        self.evidence[key] = copy.deepcopy(evidence)
        return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()

    def load_evidence(self, key, expected_hash):
        evidence = self.evidence[key]
        if hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest() != expected_hash:
            raise RuntimeError("Cache changed")
        return copy.deepcopy(evidence)


class FixedSearchTests(unittest.TestCase):
    def setUp(self):
        self.storage, self.calls = MemoryStorage(), []
        self.now = NOW

        def transport(queries, key):
            self.assertEqual(self.storage.state["requests"]["10:initial"]["state"], "reserved")
            self.calls.append(queries)
            return copy.deepcopy(RESPONSE)

        self.transport = transport
        self.client = FixedSearchClient(self.storage, "fake-key", clock=lambda: self.now, transport=transport)

    def test_success_is_reserved_first_then_cached_across_restart(self):
        result = self.client.search(10, "initial", ["exact metric"])
        restarted = FixedSearchClient(self.storage, "fake-key", clock=lambda: self.now,
            transport=lambda *args: self.fail("Cached Search POST must never repeat"))
        self.assertEqual(restarted.search(10, "initial", ["newly planned query"]), result)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(restarted.remaining_usd(), Decimal("4.995"))
        self.assertEqual(restarted.blocked_post_ids(), set())

    def test_timeout_stays_charged_and_blocks_after_restart_and_week_rollover(self):
        def timeout(*args):
            self.calls.append(args)
            raise TimeoutError("Provider may have charged")
        self.client.transport = timeout
        with self.assertRaises(ResearchAttemptUncertain):
            self.client.search(10, "initial", ["metric"])
        self.assertEqual(self.client.remaining_usd(), Decimal("4.995"))
        self.assertEqual(self.client.blocked_post_ids(), {10})
        self.now += timedelta(days=8)
        with self.assertRaises(ResearchAttemptUncertain):
            self.client.search(10, "initial", ["metric"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.client.remaining_usd(), Decimal("5.00"))

    def test_failed_reservation_never_sends(self):
        self.storage.fail_save = True
        with self.assertRaises(RuntimeError):
            self.client.search(10, "initial", ["metric"])
        self.assertEqual(self.calls, [])

    def test_failed_cache_checkpoint_does_not_authorize_resend(self):
        self.storage.fail_cache = True
        with self.assertRaises(ResearchAttemptUncertain):
            self.client.search(10, "initial", ["metric"])
        with self.assertRaises(ResearchAttemptUncertain):
            self.client.search(10, "initial", ["metric"])
        self.assertEqual(len(self.calls), 1)

    def test_rolling_week_limit_applies_before_send_and_checks_two_phase_budget(self):
        for i in range(999):
            self.storage.state["requests"][f"{i + 100}:initial"] = {
                "post_id": i + 100, "phase": "initial", "state": "reserved",
                "at_utc": NOW.isoformat().replace("+00:00", "Z"), "cost_usd": "0.005"}
        with self.assertRaises(WeeklyBudgetExhausted):
            self.client.preflight(10)
        self.client.search(10, "initial", ["metric"])
        with self.assertRaises(WeeklyBudgetExhausted):
            self.client.search(10, "fact-check", ["check"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.client.remaining_usd(), Decimal("0.000"))
        self.now += timedelta(days=7, seconds=1)
        self.assertEqual(self.client.remaining_usd(), Decimal("5.00"))

    def test_invalid_response_is_not_retried(self):
        self.client.transport = lambda *args: {"results": []}
        with self.assertRaises(ResearchAttemptUncertain):
            self.client.search(10, "initial", ["metric"])
        self.assertEqual(self.client.blocked_post_ids(), {10})

    def test_error_never_exposes_provider_payload_or_key(self):
        def denied(*args):
            raise RuntimeError("Authorization: fake-key sensitive body")
        self.client.transport = denied
        with self.assertRaises(ResearchAttemptUncertain) as caught:
            self.client.search(10, "initial", ["metric"])
        self.assertNotIn("fake-key", str(caught.exception))
        self.assertNotIn("sensitive", str(caught.exception))

    def test_malformed_budget_or_future_clock_never_sends(self):
        self.storage.state["requests"]["10:initial"] = {"post_id": 10, "phase": "initial",
            "at_utc": (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "state": "reserved", "cost_usd": "0.005"}
        with self.assertRaises(SearchResearchError):
            self.client.search(11, "initial", ["metric"])
        self.assertEqual(self.calls, [])

    def test_planner_is_bounded(self):
        self.assertEqual(_parse_planned_queries('```json\n["metric official", "metric history"]\n```'),
                         ["metric official", "metric history"])
        for text in ('not JSON', '[]', '["a", "b", "c", "d", "e", "f"]', '[true]'):
            with self.assertRaises(SearchResearchError):
                _parse_planned_queries(text)

    def test_transport_disables_redirects_and_has_only_one_post(self):
        calls = []

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return json.dumps(RESPONSE).encode()

        class Opener:
            def open(self, request, timeout):
                calls.append(request)
                self_request = json.loads(request.data)
                self_test.assertEqual(self_request["search_type"], "web")
                self_test.assertEqual(self_request["max_results"], 10)
                return Response()

        self_test = self
        with patch("fixed_search_research.urllib.request.build_opener", return_value=Opener()) as build:
            call_search_once(["metric"], "fake-key")
        handler = build.call_args.args[0]
        self.assertIsNone(handler.redirect_request(None, None, 307, "redirect", {}, "https://other.invalid"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].method, "POST")
        self.assertEqual(calls[0].full_url, "https://api.perplexity.ai/search")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.files, self.manifest = {}, {"version": 1, "resources": []}
        outer = self

        class Missing(Exception): pass

        class Contents:
            def __init__(self, path="publication.enc"): self.path = path
            def at_path(self, path): return Contents(path)
            def read(self):
                if self.path not in outer.files: raise Missing()
                raw = outer.files[self.path]
                return raw, hashlib.sha256(raw).hexdigest()
            def create(self, raw):
                if self.path in outer.files: raise RuntimeError("Already exists")
                outer.files[self.path] = raw
            def compare_and_swap(self, sha, raw):
                if self.path == "publication.enc":
                    outer.manifest = json.loads(raw)["data"]
                else:
                    if self.read()[1] != sha: raise RuntimeError("Conflict")
                    outer.files[self.path] = raw

        class Codec:
            def encode(self, raw, resource=None):
                return json.dumps({"data": json.loads(raw), "resource": resource}).encode()
            def decode(self, raw, resource=None):
                data = json.loads(raw)
                if data["resource"] != resource: raise RuntimeError("Wrong encryption context")
                return json.dumps(data["data"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            def resource_name(self, resource): return hashlib.sha256(resource.encode()).hexdigest()

        self.store = types.SimpleNamespace(contents=Contents(), codec=Codec(),
            _manifest=lambda: (copy.deepcopy(self.manifest), "manifest-sha"))
        self.storage = GitHubSearchStorage(self.store)
        module = types.ModuleType("github_publication")
        module.MissingStateFile = Missing
        module.git_blob_sha = lambda raw: hashlib.sha256(raw).hexdigest()
        self.patch = patch.dict(sys.modules, {"github_publication": module})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_initializes_once_and_preserves_other_manifest_fields(self):
        self.manifest["legacy_cutover"] = {"mode": "frozen"}
        state, sha = self.storage.load()
        self.assertEqual(state, {"version": 1, "requests": {}})
        self.assertIn(MARKER, self.manifest)
        self.assertEqual(self.manifest["legacy_cutover"], {"mode": "frozen"})
        state["requests"]["example"] = {"state": "reserved"}
        self.storage.save(state, sha)
        self.assertEqual(self.storage.load()[0], state)

    def test_deleted_registered_ledger_is_not_reset(self):
        self.storage.load()
        del self.files[self.storage.contents.path]
        with self.assertRaises(SearchResearchError):
            self.storage.load()
        self.assertNotIn(self.storage.contents.path, self.files)

    def test_unregistered_existing_ledger_is_preserved(self):
        state = {"version": 1, "requests": {"spent": "kept"}}
        self.files[self.storage.contents.path] = self.store.codec.encode(json.dumps(state).encode(), RESOURCE)
        self.assertEqual(self.storage.load()[0], state)
        self.assertIn(MARKER, self.manifest)

    def test_cache_identity_and_integrity(self):
        evidence = {"queries": ["metric"], "sources": RESPONSE["results"]}
        digest = self.storage.save_evidence("10:initial", evidence)
        self.assertEqual(self.storage.load_evidence("10:initial", digest), evidence)
        with self.assertRaises(SearchResearchError):
            self.storage.load_evidence("10:initial", "0" * 64)


class HelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_searches_and_sponsored_synthesis(self):
        storage, calls, prompts = MemoryStorage(), [], []
        client = FixedSearchClient(storage, "fake-key", clock=lambda: NOW,
            transport=lambda queries, key: (calls.append(queries) or copy.deepcopy(RESPONSE)))
        answers = iter(['["metric current", "metric historical"]', 'Initial sourced report.',
                        '["metric primary verification"]', 'Corrected report with https://example.org/result'])

        class Model:
            async def invoke(self, prompt):
                prompts.append(prompt)
                return next(answers)

        class Bot:
            _fixed_search_client = client
            def get_llm(self, purpose):
                if purpose != "default": raise AssertionError("Only sponsored default may synthesize")
                return Model()
            async def _run_blocking(self, function, *args, _timeout=None):
                return function(*args)

        result = await fixed_search_research(Bot(), types.SimpleNamespace(id_of_post=10), "Question and criteria")
        self.assertEqual(len(calls), 2)
        self.assertEqual(client.remaining_usd(), Decimal("4.990"))
        self.assertIn("Corrected report", result)
        self.assertIn("not instructions", prompts[-1])
        self.assertIn("Initial sourced report", prompts[-1])


if __name__ == "__main__":
    unittest.main()

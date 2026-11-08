"""Synthetic HTTP-only GitHub Actions queue inventory diagnostics; zero network."""

from datetime import datetime, timezone
import json
import urllib.error
from urllib.parse import parse_qs, urlparse

from .support import OfflineTestCase
from github_publisher_queue_readonly import (
    GitHubPublisherQueueReader, GitHubRunEvidenceUnavailable,
    KNOWN_STUCK_RUN_ID, PUBLISHING_WORKFLOW_ID, SCANNED_STATES,
    MAX_RUNS_PER_STATE, _STATUS_URLS,
)


NOW = datetime(2026, 11, 8, 13, 15, tzinfo=timezone.utc)


class Reply:
    status = 200

    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit):
        return self.body[:limit]


class FakeGitHub:
    def __init__(self, batches=None):
        self.batches = batches or {}
        self.calls = []
        self.broken_status = None
        self.altered = None

    def open(self, request, timeout):
        self.calls.append((request.get_method(), request.full_url, timeout))
        url = request.full_url
        if url.endswith(f"/actions/runs/{KNOWN_STUCK_RUN_ID}"):
            return Reply({"id": KNOWN_STUCK_RUN_ID, "status": "queued"})
        query = parse_qs(urlparse(url).query)
        status = query["status"][0]
        if status == self.broken_status:
            raise urllib.error.URLError("simulated no network")
        runs = self.batches.get(status, [])
        data = {"total_count": len(runs), "workflow_runs": runs}
        if status == self.altered:
            data["total_count"] += 1
        return Reply(data)


def run(run_id, state, workflow=PUBLISHING_WORKFLOW_ID):
    return {"id": run_id, "status": state, "workflow_id": workflow}


class PublisherQueueReadOnlyTests(OfflineTestCase):
    def test_empty_verified_inventory_is_clear_without_writing_anything(self):
        fake = FakeGitHub()
        reader = GitHubPublisherQueueReader(token="SYNTHETIC", opener=fake,
                                            clock=lambda: NOW)
        result = reader.read()
        self.assertTrue(result.clear_to_consider_dispatch)
        self.assertEqual(result.queued_publisher_runs, 0)
        self.assertEqual(result.active_publisher_runs, 0)
        self.assertEqual(result.publisher_run_ids, ())
        self.assertEqual(result.checked_at, NOW)
        self.assertEqual(len(fake.calls), len(SCANNED_STATES))
        self.assertEqual([q[1] for q in fake.calls],
                         [_STATUS_URLS[status] for status in SCANNED_STATES])
        self.assertTrue(all(q[0] == "GET" and q[2] == 12 for q in fake.calls))

    def test_2026_stuck_queue_blocks_automated_publish_even_if_no_jobs(self):
        fake = FakeGitHub({"queued": [run(KNOWN_STUCK_RUN_ID, "queued")]})
        result = GitHubPublisherQueueReader(
            token="SYNTHETIC", opener=fake, clock=lambda: NOW
        ).read()
        self.assertFalse(result.clear_to_consider_dispatch)
        self.assertTrue(result.known_stuck_queued)
        self.assertEqual(result.queued_publisher_runs, 1)
        self.assertEqual(result.publisher_run_ids, (KNOWN_STUCK_RUN_ID,))

    def test_other_workflows_do_not_block_publisher(self):
        fake = FakeGitHub({
            "queued": [run(555, "queued", 410403549)],
            "in_progress": [run(556, "in_progress", 410403549)],
        })
        report = GitHubPublisherQueueReader(
            token="SYNTHETIC", opener=fake, clock=lambda: NOW
        ).read()
        self.assertTrue(report.clear_to_consider_dispatch)

    def test_active_and_queued_publisher_counted_independently(self):
        fake = FakeGitHub({
            "queued": [run(2, "queued"), run(7, "queued")],
            "in_progress": [run(5, "in_progress")],
            "waiting": [run(8, "waiting")],
        })
        report = GitHubPublisherQueueReader(
            token="SYNTHETIC", opener=fake, clock=lambda: NOW
        ).read()
        self.assertFalse(report.clear_to_consider_dispatch)
        self.assertEqual(report.active_publisher_runs, 1)
        self.assertEqual(report.queued_publisher_runs, 3)
        self.assertEqual(report.publisher_run_ids, (2, 5, 7, 8))

    def test_missing_authentication_denies_before_any_get(self):
        for token in ("", " ", None, " abc ", 5):
            with self.subTest(token=token), self.assertRaises(GitHubRunEvidenceUnavailable):
                GitHubPublisherQueueReader(token=token, opener=FakeGitHub())

    def test_partial_inventory_never_claims_zero_publisher_runs(self):
        fake = FakeGitHub()
        fake.altered = "queued"
        reader = GitHubPublisherQueueReader(
            token="SYNTHETIC", opener=fake, clock=lambda: NOW
        )
        with self.assertRaises(GitHubRunEvidenceUnavailable):
            reader.read()
        self.assertEqual(len(fake.calls), 2)

    def test_failed_http_get_never_retries(self):
        fake = FakeGitHub()
        fake.broken_status = "queued"
        reader = GitHubPublisherQueueReader(
            token="SYNTHETIC", opener=fake, clock=lambda: NOW
        )
        with self.assertRaises(GitHubRunEvidenceUnavailable):
            reader.read()
        self.assertEqual([s for _, s, _ in fake.calls], [
            _STATUS_URLS["in_progress"], _STATUS_URLS["queued"]
        ])

    def test_mismatched_run_identity_or_duplicate_fails_closed(self):
        for change in (
            {"id": "bad"}, {"id": True}, {"status": "completed"},
            {"workflow_id": None}, {"workflow_id": True},
        ):
            fake = FakeGitHub({"queued": [{**run(10, "queued"), **change}]})
            with self.subTest(change=change), self.assertRaises(GitHubRunEvidenceUnavailable):
                GitHubPublisherQueueReader(
                    token="SYNTHETIC", opener=fake, clock=lambda: NOW
                ).read()
        fake = FakeGitHub({"queued": [run(10, "queued")],
                           "in_progress": [run(10, "in_progress")]})
        with self.assertRaises(GitHubRunEvidenceUnavailable):
            GitHubPublisherQueueReader(
                token="SYNTHETIC", opener=fake, clock=lambda: NOW
            ).read()

    def test_stuck_run_extra_diagnostic_is_one_get_not_cancellation(self):
        fake = FakeGitHub()
        reader = GitHubPublisherQueueReader(
            token="SYNTHETIC", opener=fake, clock=lambda: NOW
        )
        data = reader.get_known_stuck_run()
        self.assertEqual(data, {"run_id": KNOWN_STUCK_RUN_ID, "status": "queued"})
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][0], "GET")

    def test_clock_requires_aware_timestamp(self):
        fake = FakeGitHub()
        with self.assertRaises(GitHubRunEvidenceUnavailable):
            GitHubPublisherQueueReader(
                token="SYNTHETIC", opener=fake,
                clock=lambda: datetime(2026, 11, 8, 13, 15),
            ).read()

    def test_existing_publisher_remains_manual_only(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        existing = (root / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        main = (root / "main.py").read_text()
        self.assertIn("default: DO_NOT_RUN", existing)
        self.assertNotIn("\n  schedule:", existing)
        self.assertNotIn("--automatic-one", existing)
        self.assertNotIn('parser.add_argument("--automatic-one"', main)
        source = (root / "github_publisher_queue_readonly.py").read_text()
        self.assertNotIn('method="POST"', source)
        self.assertNotIn('method="PUT"', source)
        self.assertNotIn("workflow_dispatch(", source)


if __name__ == "__main__":
    import unittest
    unittest.main()

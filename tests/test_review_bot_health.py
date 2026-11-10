"""Synthetic GitHub health responses; no real network, tokens, or model calls."""

from datetime import datetime, timedelta, timezone
import json
import unittest
import urllib.error

from tools.review_bot_health import (
    HealthReader, HealthUnavailable, MAX_BYTES, REPOSITORY, REVIEW_PATH,
    RUNS_URL, WORKFLOW_PATH, WORKFLOW_URL, _NoRedirect, review, summarize,
)


NOW = datetime(2026, 11, 10, 8, tzinfo=timezone.utc)
TOKEN = "SYNTHETIC_NOT_A_CREDENTIAL"


def environment():
    return {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_EVENT_NAME": "schedule", "GITHUB_TOKEN": TOKEN,
            "GITHUB_WORKFLOW_REF": REPOSITORY + "/" + REVIEW_PATH + "@refs/heads/main"}


def workflow(**changes):
    result = {"id": 99, "path": WORKFLOW_PATH, "state": "active"}
    result.update(changes)
    return result


def run(**changes):
    stamp = (NOW - timedelta(minutes=10)).isoformat()
    result = {"id": 123, "workflow_id": 99, "path": WORKFLOW_PATH, "head_branch": "main",
              "repository": {"full_name": REPOSITORY}, "status": "completed", "conclusion": "success",
              "created_at": stamp, "run_started_at": stamp, "updated_at": stamp}
    result.update(changes)
    return result


class Reply:
    status = 200

    def __init__(self, data):
        self.raw = data if isinstance(data, bytes) else json.dumps(data).encode()

    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def read(self, limit): return self.raw[:limit]


class Opener:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return Reply(reply)


class HealthTests(unittest.TestCase):
    def test_two_fixed_gets_produce_safe_healthy_summary(self):
        opener = Opener(workflow(), {"total_count": 2, "workflow_runs": [
            run(), run(id=124, status="queued", conclusion=None, run_started_at=None)]})
        result = review(environment(), opener=opener, now=NOW)
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["github_get_attempts"], 2)
        self.assertEqual(result["sample_counts"]["queued_or_waiting"], 1)
        self.assertEqual([call[0].full_url for call in opener.calls], [WORKFLOW_URL, RUNS_URL])
        self.assertTrue(all(request.get_method() == "GET" and request.data is None and timeout == 20
                            for request, timeout in opener.calls))
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(result["last_successful_run"]["run_id"], 123)

    def test_disabled_stale_and_failed_runs_have_actionable_warnings(self):
        stale = (NOW - timedelta(hours=4)).isoformat()
        for metadata, records, expected in (
            (workflow(state="disabled_manually"), [run()], "automatic_workflow_not_active"),
            (workflow(), [run(created_at=stale, updated_at=stale, run_started_at=stale)], "no_started_run_within_90_minutes"),
            (workflow(), [run(conclusion="failure")], "latest_completed_run_failed"),
            (workflow(), [], "no_successful_run_within_3_hours_in_sample"),
        ):
            with self.subTest(expected=expected):
                result = summarize(metadata, {"total_count": len(records), "workflow_runs": records}, now=NOW)
                self.assertEqual(result["status"], "warning")
                self.assertIn(expected, result["warnings"])

    def test_recent_sample_is_explicitly_partial_and_cancellations_are_not_failures(self):
        result = summarize(workflow(), {"total_count": 500, "workflow_runs": [
            run(), run(id=124, conclusion="cancelled", updated_at=NOW.isoformat())]}, now=NOW)
        self.assertTrue(result["sample"]["truncated"])
        self.assertEqual(result["sample"]["limit"], 100)
        self.assertEqual(result["sample_counts"]["failed"], 0)
        self.assertEqual(result["status"], "healthy")

    def test_transport_boundary_refuses_extra_gets_redirects_and_no_retry_after_failure(self):
        opener = Opener({}, {})
        reader = HealthReader(TOKEN, opener=opener)
        with self.assertRaises(HealthUnavailable):
            reader.get("https://example.invalid/credential-leak")
        self.assertEqual(opener.calls, [])
        reader.get(WORKFLOW_URL)
        reader.get(RUNS_URL)
        with self.assertRaises(HealthUnavailable):
            reader.get(RUNS_URL)
        self.assertEqual(len(opener.calls), 2)
        with self.assertRaises(HealthUnavailable):
            _NoRedirect().redirect_request(None)
        for response in (urllib.error.URLError(TOKEN), b"x" * (MAX_BYTES + 1), b"not JSON"):
            failed = Opener(response)
            with self.subTest(response=type(response).__name__), self.assertRaises(HealthUnavailable) as caught:
                HealthReader(TOKEN, opener=failed).get(WORKFLOW_URL)
            self.assertEqual(len(failed.calls), 1)
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_unapproved_workflow_context_is_rejected_before_any_get(self):
        for key, value in (("GITHUB_REF", "refs/pull/4/merge"), ("GITHUB_EVENT_NAME", "pull_request"),
                           ("GITHUB_REPOSITORY", "other/repo"), ("GITHUB_RUN_ATTEMPT", "2"),
                           ("GITHUB_WORKFLOW_REF", "other.yml@main")):
            env, opener = environment(), Opener()
            env[key] = value
            with self.subTest(key=key), self.assertRaises(HealthUnavailable):
                review(env, opener=opener, now=NOW)
            self.assertEqual(opener.calls, [])

    def test_malformed_or_foreign_run_cannot_report_healthy(self):
        for changes in ({"path": ".github/workflows/other.yaml"}, {"head_branch": "feature"},
                        {"repository": {"full_name": "other/repo"}}, {"run_started_at": None},
                        {"conclusion": None}, {"status": "unknown"},
                        {"updated_at": (NOW + timedelta(hours=1)).isoformat()}):
            with self.subTest(changes=changes), self.assertRaises(HealthUnavailable):
                summarize(workflow(), {"total_count": 1, "workflow_runs": [run(**changes)]}, now=NOW)


if __name__ == "__main__":
    unittest.main()

"""Deterministic successor dispatch; no network, credentials, or real waits."""

from datetime import datetime, timedelta, timezone
import json
import unittest
import urllib.error

from tools.queue_next_automatic import (
    GitHub, INTERVAL_SECONDS, QueueNextError, REPOSITORY, ROOT, WORKFLOW,
    WORKFLOW_PATH, _NoRedirect, queue_next,
)


NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)


def environment(event="workflow_dispatch"):
    return {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_RUN_ID": "200", "GITHUB_SHA": "a" * 40,
            "GITHUB_EVENT_NAME": event,
            "GITHUB_WORKFLOW_REF": REPOSITORY + "/" + WORKFLOW_PATH + "@refs/heads/main"}


def record(event="workflow_dispatch", **changes):
    result = {"id": 200, "workflow_id": 999, "run_attempt": 1,
              "repository": {"full_name": REPOSITORY}, "path": WORKFLOW_PATH,
              "head_branch": "main", "head_sha": "a" * 40, "event": event,
              "status": "in_progress", "run_started_at": NOW.isoformat()}
    result.update(changes)
    return result


class FakeGitHub:
    def __init__(self, *, event="workflow_dispatch", newer=None):
        self.current = record(event)
        self.newer = [] if newer is None else newer
        self.reads = 0
        self.posts = 0
        self.error = None

    def own_run(self, run_id):
        assert run_id == 200
        self.reads += 1
        return self.current

    def workflow_runs(self):
        return {"workflow_runs": self.newer}

    def dispatch(self):
        self.posts += 1
        if self.error:
            raise self.error


class QueueNextTests(unittest.TestCase):
    def test_regular_cadence_wait_is_bounded_and_push_bootstraps_immediately(self):
        for event, elapsed, expected_wait in (("schedule", 120, 780),
                ("workflow_dispatch", 0, 900), ("workflow_dispatch", 1500, 0),
                ("push", 20, 0)):
            with self.subTest(event=event, elapsed=elapsed):
                fake, waits = FakeGitHub(event=event), []
                result = queue_next(environment(event), api=fake,
                    now=lambda: NOW + timedelta(seconds=elapsed), sleep=waits.append)
                self.assertEqual(sum(waits), expected_wait)
                self.assertTrue(all(0 < delay <= 30 for delay in waits))
                self.assertEqual(fake.reads, 2)
                self.assertEqual(fake.posts, 1)
                self.assertEqual(result["status"], "one_successor_requested")
                self.assertEqual(result["cadence_seconds"], INTERVAL_SECONDS)

    def test_only_verified_success_drains_the_backlog_without_idle_wait(self):
        for outcome, count, expected_wait in (("success", "1", 0),
                ("success", "0", 780), ("failure", "1", 780),
                ("cancelled", "1", 780), ("success", "2", 780),
                ("success", "true", 780)):
            with self.subTest(outcome=outcome, count=count):
                env, fake, waits = environment(), FakeGitHub(), []
                env.update(VRCHCAB_BOT_OUTCOME=outcome, VRCHCAB_VERIFIED_FORECASTS=count)
                result = queue_next(env, api=fake,
                    now=lambda: NOW + timedelta(seconds=120), sleep=waits.append)
                self.assertEqual(sum(waits), expected_wait)
                self.assertEqual(fake.posts, 1)
                self.assertEqual(result["immediate_after_verified_forecast"], expected_wait == 0)

    def test_newer_same_workflow_first_main_run_takes_over(self):
        for state in ("queued", "in_progress", "pending"):
            fake = FakeGitHub(newer=[record(id=201, status=state)])
            result = queue_next(environment(), api=fake, now=lambda: NOW, sleep=lambda _: None)
            self.assertEqual(result, {"status": "newer_run_already_waiting", "run_id": 201})
            self.assertEqual(fake.posts, 0)
        for changed in ({"id": 199}, {"id": 201, "run_attempt": 2},
                        {"id": 201, "head_branch": "feature"},
                        {"id": 201, "workflow_id": 888},
                        {"id": 201, "status": "completed"}):
            fake = FakeGitHub(newer=[record(**changed)])
            queue_next(environment(), api=fake, now=lambda: NOW, sleep=lambda _: None)
            self.assertEqual(fake.posts, 1)

    def test_wrong_context_rerun_or_cancelled_current_run_never_dispatch(self):
        for key, value in (("GITHUB_REPOSITORY", "other/repository"),
                ("GITHUB_REF", "refs/heads/feature"), ("GITHUB_RUN_ATTEMPT", "2"),
                ("GITHUB_WORKFLOW_REF", "other.yml@main"),
                ("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_RUN_ID", "not-an-id")):
            env, fake = environment(), FakeGitHub()
            env[key] = value
            with self.subTest(key=key), self.assertRaises(QueueNextError):
                queue_next(env, api=fake, now=lambda: NOW, sleep=lambda _: None)
            self.assertEqual(fake.reads, 0)
            self.assertEqual(fake.posts, 0)
        fake = FakeGitHub()
        def cancel(_seconds):
            fake.current = record(status="completed", conclusion="cancelled")
        with self.assertRaises(QueueNextError):
            queue_next(environment(), api=fake, now=lambda: NOW, sleep=cancel)
        self.assertEqual(fake.posts, 0)

    def test_single_fixed_dispatch_and_no_retry_after_uncertain_http_outcome(self):
        class Reply:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *_): return False
        class Opener:
            def __init__(self): self.calls, self.fail = [], False
            def open(self, request, timeout):
                self.calls.append((request, timeout))
                if self.fail:
                    raise urllib.error.URLError("synthetic transport uncertainty")
                return Reply()
        opener = Opener()
        github = GitHub("SYNTHETIC_TOKEN_NOT_A_CREDENTIAL", opener=opener)
        github.dispatch()
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, ROOT + "/actions/workflows/" + WORKFLOW + "/dispatches")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"ref": "main"})
        self.assertEqual(timeout, 20)
        opener.fail = True
        with self.assertRaises(QueueNextError):
            github.dispatch()
        self.assertEqual(len(opener.calls), 2)  # One per invocation, no internal retry.
        with self.assertRaises(QueueNextError):
            _NoRedirect().redirect_request(None)


if __name__ == "__main__":
    unittest.main()

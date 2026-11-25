"""Offline startup recovery boundaries; no HTTP, credentials, models or sleeps."""

import copy
from datetime import datetime, timedelta, timezone
import json
import unittest
import urllib.error

from tools.recover_automatic_startup import (
    GitHub, RECOVERY_PATH, REPOSITORY, ROOT, RecoveryUnavailable,
    WORKFLOW, WORKFLOW_PATH, _NoRedirect, recover, recover_stale, ACTIVE, WAKE_PATHS, STALE_AFTER,
)


NOW = datetime(2026, 11, 11, 22, tzinfo=timezone.utc)


def environment():
    return {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_EVENT_NAME": "workflow_run", "GITHUB_RUN_ID": "900",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_WORKFLOW_REF": REPOSITORY + "/" + RECOVERY_PATH + "@refs/heads/main"}


def record(**changes):
    stamp = (NOW - timedelta(minutes=2)).isoformat()
    result = {"id": 200, "workflow_id": 123, "path": WORKFLOW_PATH,
        "repository": {"full_name": REPOSITORY}, "head_repository": {"full_name": REPOSITORY},
        "head_branch": "main", "event": "workflow_dispatch", "run_attempt": 1,
        "status": "completed", "conclusion": "startup_failure",
        "created_at": stamp, "updated_at": stamp}
    result.update(changes)
    return result


def event():
    return {"action": "completed", "repository": {"full_name": REPOSITORY}, "workflow_run": record()}


class FakeGitHub:
    def __init__(self):
        self.source = record()
        self.jobs_result = {"total_count": 0, "jobs": []}
        self.runs = [record()]
        self.total = None
        self.gets, self.posts = [], 0
        self.dispatch_error = None

    def run(self, ident):
        self.gets.append(("run", ident))
        return copy.deepcopy(self.source)

    def jobs(self, ident):
        self.gets.append(("jobs", ident))
        return self.jobs_result

    def recent_runs(self):
        self.gets.append(("recent", None))
        return {"total_count": len(self.runs) if self.total is None else self.total,
                "workflow_runs": self.runs}

    def dispatch(self):
        self.posts += 1
        if self.dispatch_error:
            raise self.dispatch_error


class RecoveryTests(unittest.TestCase):
    def attempt(self, api, *, env=None, payload=None):
        return recover(environment() if env is None else env,
                       event=event() if payload is None else payload, api=api, now=NOW)

    def test_only_verified_jobless_startup_failure_requests_one_fresh_run(self):
        api = FakeGitHub()
        result = self.attempt(api)
        self.assertEqual(result["status"], "one_fresh_automatic_run_requested")
        self.assertEqual(result["ref"], "main")
        self.assertEqual(api.gets, [("run", 200), ("jobs", 200), ("recent", None)])
        self.assertEqual(api.posts, 1)

    def test_wrong_context_event_or_source_never_dispatches(self):
        for key, value in (("GITHUB_REF", "refs/heads/feature"), ("GITHUB_RUN_ATTEMPT", "2"),
                ("GITHUB_EVENT_NAME", "push"), ("GITHUB_REPOSITORY", "other/repo"),
                ("GITHUB_WORKFLOW_REF", "other.yml@main")):
            env, api = environment(), FakeGitHub()
            env[key] = value
            with self.subTest(context=key), self.assertRaises(RecoveryUnavailable):
                self.attempt(api, env=env)
            self.assertEqual(api.gets, [])
            self.assertEqual(api.posts, 0)
        for changes in ({"conclusion": "failure"}, {"conclusion": "cancelled"},
                        {"run_attempt": 2}, {"head_branch": "feature"}):
            payload, api = event(), FakeGitHub()
            payload["workflow_run"].update(changes)
            with self.subTest(event=changes), self.assertRaises(RecoveryUnavailable):
                self.attempt(api, payload=payload)
            self.assertEqual(api.gets, [])
        future = (NOW + timedelta(hours=1)).isoformat()
        for changes in ({"id": 201}, {"path": ".github/workflows/test_bot.yaml"},
                {"repository": {"full_name": "other/repo"}}, {"head_repository": {"full_name": "fork/repo"}},
                {"conclusion": "success"}, {"run_attempt": 2},
                {"created_at": future, "updated_at": future}):
            api = FakeGitHub()
            api.source.update(changes)
            with self.subTest(source=changes), self.assertRaises(RecoveryUnavailable):
                self.attempt(api)
            self.assertEqual(api.posts, 0)

    def test_delayed_event_after_two_hour_outage_recovers_only_if_still_latest(self):
        old = (NOW - timedelta(hours=2)).isoformat()
        for newer in (False, True):
            api, payload = FakeGitHub(), event()
            api.source.update(created_at=old, updated_at=old)
            payload["workflow_run"].update(created_at=old, updated_at=old)
            api.runs = [copy.deepcopy(api.source)]
            if newer:
                api.runs.append(record(id=201, conclusion="success"))
            with self.subTest(newer=newer):
                result = self.attempt(api, payload=payload)
                self.assertEqual(result["status"], "newer_automatic_run_exists" if newer
                                 else "one_fresh_automatic_run_requested")
                self.assertEqual(api.posts, 0 if newer else 1)

    def test_even_one_job_or_unknown_job_count_refuses_recovery(self):
        for jobs in ({"total_count": 1, "jobs": [{"id": 12}]}, {"total_count": 1, "jobs": []},
                     {"total_count": False, "jobs": []}, {"jobs": []}):
            api = FakeGitHub()
            api.jobs_result = jobs
            with self.subTest(jobs=jobs), self.assertRaises(RecoveryUnavailable):
                self.attempt(api)
            self.assertEqual(api.posts, 0)

    def test_newer_run_blocks_dispatch_but_older_parent_can_finish_under_shared_lock(self):
        for newer in (record(id=201, status="queued", conclusion=None),
                      record(id=201, conclusion="success"), record(id=201)):
            api = FakeGitHub()
            api.runs.append(newer)
            self.assertEqual(self.attempt(api)["status"], "newer_automatic_run_exists")
            self.assertEqual(api.posts, 0)
        for state in ("in_progress", "queued", "pending", "waiting", "requested"):
            api = FakeGitHub()
            api.runs.append(record(id=199, status=state, conclusion=None))
            with self.subTest(older_parent_state=state):
                self.assertEqual(self.attempt(api)["status"], "one_fresh_automatic_run_requested")
                self.assertEqual(api.posts, 1)

    def test_third_startup_failure_in_hour_stops_storm_and_old_failures_expire(self):
        api = FakeGitHub()
        api.runs.extend([record(id=199), record(id=198)])
        result = self.attempt(api)
        self.assertEqual(result["status"], "startup_failure_hourly_limit")
        self.assertEqual(result["startup_failures_last_hour"], 3)
        self.assertEqual(api.posts, 0)
        old = (NOW - timedelta(hours=2)).isoformat()
        api.runs[-1].update(created_at=old, updated_at=old)
        self.assertEqual(self.attempt(api)["status"], "one_fresh_automatic_run_requested")
        self.assertEqual(api.posts, 1)

    def test_incomplete_recent_history_cannot_bypass_hourly_bound(self):
        for records, total in (([], 0), ([record(), record()], 2),
                               ([record()], 101), ([record(id=199)], 1)):
            api = FakeGitHub()
            api.runs, api.total = records, total
            with self.subTest(total=total, records=len(records)), self.assertRaises(RecoveryUnavailable):
                self.attempt(api)
            self.assertEqual(api.posts, 0)

    def test_dispatch_uncertainty_is_never_retried(self):
        api = FakeGitHub()
        api.dispatch_error = RecoveryUnavailable("Synthetic timeout")
        with self.assertRaises(RecoveryUnavailable):
            self.attempt(api)
        self.assertEqual(api.posts, 1)

    def test_transport_uses_fixed_main_dispatch_without_retry_or_redirect(self):
        class Reply:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *_args): return False
        class Opener:
            def __init__(self): self.calls, self.fail = [], False
            def open(self, request, timeout):
                self.calls.append((request, timeout))
                if self.fail:
                    raise urllib.error.URLError("SYNTHETIC_TOKEN")
                return Reply()
        opener = Opener()
        api = GitHub("SYNTHETIC_TOKEN", opener=opener)
        api.dispatch()
        request, timeout = opener.calls[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, ROOT + "/actions/workflows/" + WORKFLOW + "/dispatches")
        self.assertEqual(json.loads(request.data), {"ref": "main"})
        self.assertEqual(timeout, 20)
        opener.fail = True
        with self.assertRaises(RecoveryUnavailable) as caught:
            api.dispatch()
        self.assertNotIn("SYNTHETIC_TOKEN", str(caught.exception))
        self.assertEqual(len(opener.calls), 2)
        with self.assertRaises(RecoveryUnavailable):
            _NoRedirect().redirect_request(None)



class StaleGitHub(FakeGitHub):
    def __init__(self, conclusion="failure"):
        super().__init__()
        old = (NOW - timedelta(minutes=60)).isoformat()
        self.source = record(conclusion=conclusion, created_at=old, updated_at=old,
                             head_sha="a" * 40)
        self.runs = [copy.deepcopy(self.source)]
        self.workflow_result = {"id": 123, "path": WORKFLOW_PATH, "state": "active"}
        self.active = {}
        self.final_runs = None
        self.final_workflow = None
        self.read_count = 0
        self.workflow_count = 0
        self.jobs_result = {"total_count": 1, "jobs": [{
            "run_id": 200, "name": "forecast_job", "status": "completed",
            "conclusion": conclusion, "steps": [
                {"name": name, "number": number, "status": "completed", "conclusion": outcome}
                for name, number, outcome in (
                    ("Offline publication regressions", 6, "success"),
                    ("Run bot", 7, "success"),
                    ("Queue next automatic pass", 10, conclusion))]}]}

    def workflow(self):
        self.gets.append(("workflow", None))
        self.workflow_count += 1
        return self.final_workflow if self.workflow_count > 1 and self.final_workflow is not None else self.workflow_result

    def recent_runs(self):
        self.read_count += 1
        if self.read_count > 1 and self.final_runs is not None:
            self.gets.append(("recent", None))
            return {"total_count": len(self.final_runs), "workflow_runs": self.final_runs}
        return super().recent_runs()

    def active_runs(self, status):
        self.gets.append(("active", status))
        return self.active.get(status, {"total_count": 0, "workflow_runs": []})


class StaleRecoveryTests(unittest.TestCase):
    def attempt(self, api, *, env=None, payload=None):
        if env is None:
            env = environment()
            env["GITHUB_EVENT_NAME"] = "schedule"
        return recover_stale(env, event=payload, api=api, now=NOW)

    def test_stalled_after_successful_bot_and_failed_successor(self):
        api = StaleGitHub()
        self.assertEqual(self.attempt(api)["status"], "one_fresh_automatic_run_requested")
        self.assertEqual(api.posts, 1)
        self.assertEqual(len(api.gets), 11)
        self.assertEqual(api.gets[-2:], [("recent", None), ("workflow", None)])

    def test_successful_run_with_no_later_run_can_recover(self):
        api = StaleGitHub("success")
        self.assertEqual(self.attempt(api)["status"], "one_fresh_automatic_run_requested")
        self.assertEqual(api.posts, 1)

    def test_recent_or_active_or_disabled_blocks(self):
        for kind in ("recent", "active", "disabled", "empty", "cancelled", "rerun"):
            api = StaleGitHub()
            if kind == "recent":
                api.runs[0]["updated_at"] = (NOW - STALE_AFTER + timedelta(seconds=1)).isoformat()
            elif kind == "active":
                api.runs[0].update(status="in_progress", conclusion=None)
            elif kind == "disabled":
                api.workflow_result["state"] = "disabled_manually"
            elif kind == "empty":
                api.runs = []
            elif kind == "cancelled":
                api.runs[0]["conclusion"] = "cancelled"
            elif kind == "rerun":
                api.runs[0]["run_attempt"] = 2
            with self.subTest(kind=kind):
                self.assertNotEqual(self.attempt(api)["status"], "one_fresh_automatic_run_requested")
                self.assertEqual(api.posts, 0)

    def test_older_active_run_outside_recent_page_blocks(self):
        for state in ACTIVE:
            api = StaleGitHub()
            api.active[state] = {"total_count": 1, "workflow_runs": [{"id": 1}]}
            self.assertEqual(self.attempt(api)["status"], "automatic_run_active")
            self.assertEqual(api.posts, 0)

    def test_failed_bot_or_offline_guard_never_recovers(self):
        for name in ("Run bot", "Offline publication regressions"):
            api = StaleGitHub()
            next(step for step in api.jobs_result["jobs"][0]["steps"] if step["name"] == name)["conclusion"] = "failure"
            self.assertEqual(self.attempt(api)["status"], "latest_job_not_safe_for_recovery")
            self.assertEqual(api.posts, 0)

    def test_jobless_startup_failure_after_stall_recovers(self):
        api = StaleGitHub("startup_failure")
        api.jobs_result = {"total_count": 0, "jobs": []}
        self.assertEqual(self.attempt(api)["status"], "one_fresh_automatic_run_requested")
        self.assertEqual(api.posts, 1)

    def test_latest_changed_during_reads_blocks(self):
        for kind in ("direct", "newer", "active", "disabled"):
            api = StaleGitHub()
            if kind == "direct":
                api.source["updated_at"] = (NOW - timedelta(minutes=50)).isoformat()
            elif kind == "newer":
                api.final_runs = [dict(api.runs[0], id=201), api.runs[0]]
            elif kind == "active":
                api.final_runs = [dict(api.runs[0], id=201, status="queued", conclusion=None), api.runs[0]]
            elif kind == "disabled":
                api.final_workflow = dict(api.workflow_result, state="disabled_manually")
            with self.subTest(kind=kind):
                self.assertNotEqual(self.attempt(api)["status"], "one_fresh_automatic_run_requested")
                self.assertEqual(api.posts, 0)

    def test_malformed_or_partial_inventory_denies(self):
        for kind in ("partial", "duplicate", "future", "active_count"):
            api = StaleGitHub()
            if kind == "partial":
                api.total = 101
            elif kind == "duplicate":
                api.runs.append(copy.deepcopy(api.runs[0]))
            elif kind == "future":
                api.runs[0]["updated_at"] = (NOW + timedelta(hours=1)).isoformat()
            elif kind == "active_count":
                api.active["queued"] = {"total_count": 1, "workflow_runs": []}
            with self.subTest(kind=kind), self.assertRaises(RecoveryUnavailable):
                self.attempt(api)
            self.assertEqual(api.posts, 0)

    def test_no_repeat_after_uncertain_dispatch(self):
        api = StaleGitHub()
        api.dispatch_error = RecoveryUnavailable("synthetic timeout")
        with self.assertRaises(RecoveryUnavailable):
            self.attempt(api)
        self.assertEqual(api.posts, 1)

    def test_get_failure_is_not_retried(self):
        api = StaleGitHub()
        def unavailable():
            api.gets.append(("recent", None))
            raise RecoveryUnavailable("synthetic GET")
        api.recent_runs = unavailable
        with self.assertRaises(RecoveryUnavailable):
            self.attempt(api)
        self.assertEqual(api.gets, [("workflow", None), ("recent", None)])
        self.assertEqual(api.posts, 0)

    def test_untrusted_context_and_credentials_deny_before_reads(self):
        for key, val in (("GITHUB_REF", "refs/heads/feature"), ("GITHUB_RUN_ATTEMPT", "2"),
                         ("GITHUB_EVENT_NAME", "workflow_dispatch"), ("METACULUS_TOKEN", "fake"),
                         ("OPENROUTER_API_KEY", "fake"), ("VRCHCAB_OPENAI_TEST_KEY", "fake")):
            api, env = StaleGitHub(), environment()
            env["GITHUB_EVENT_NAME"] = "schedule"
            env[key] = val
            with self.subTest(key=key), self.assertRaises(RecoveryUnavailable):
                self.attempt(api, env=env)
            self.assertEqual(api.gets, [])
            self.assertEqual(api.posts, 0)

    def test_scheduled_monitor_wakeup_not_manual_or_production(self):
        for path in WAKE_PATHS:
            api, payload = StaleGitHub(), event()
            payload["workflow_run"].update(path=path, event="schedule", conclusion="success")
            self.assertEqual(self.attempt(api, env=environment(), payload=payload)["status"],
                             "one_fresh_automatic_run_requested")
            self.assertEqual(api.posts, 1)
        for changes in ({"path": WORKFLOW_PATH}, {"event": "workflow_dispatch"},
                        {"head_branch": "feature"}, {"run_attempt": 2},
                        {"head_repository": {"full_name": "fork/repo"}}):
            api, payload = StaleGitHub(), event()
            payload["workflow_run"].update(path=sorted(WAKE_PATHS)[0], event="schedule", conclusion="failure")
            payload["workflow_run"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(RecoveryUnavailable):
                self.attempt(api, env=environment(), payload=payload)
            self.assertEqual(api.gets, [])
            self.assertEqual(api.posts, 0)


if __name__ == "__main__":
    unittest.main()

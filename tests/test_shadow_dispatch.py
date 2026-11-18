"""Explicit automatic dispatch and source-completion race, wholly offline."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import shadow_capture as capture
import shadow_comparison as policy
import shadow_runner as worker
from tools import dispatch_shadow as dispatcher

NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)


def producer_env(event="workflow_dispatch"):
    return {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": capture.REPO,
        "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_ID": "123", "GITHUB_SHA": "a" * 40, "GITHUB_EVENT_NAME": event,
        "GITHUB_WORKFLOW_REF": capture.REPO + "/" + worker.SOURCE_WORKFLOW + "@refs/heads/main",
        "VRCHCAB_VERIFIED_FORECASTS": "1", "VRCHCAB_SHADOW_ARTIFACT_ID": "789"}


def source(**change):
    value = {"id": 123, "repository": {"full_name": capture.REPO},
        "head_repository": {"full_name": capture.REPO}, "head_branch": "main",
        "head_sha": "a" * 40, "path": worker.SOURCE_WORKFLOW,
        "event": "workflow_dispatch", "run_attempt": 1, "status": "completed", "conclusion": "success"}
    value.update(change)
    return value


def event(kind="workflow_dispatch"):
    return {"inputs": {"source_run_id": "123"}} if kind == "workflow_dispatch" else {"workflow_run": source()}


def snapshot():
    return {"experiment": policy.EXPERIMENT_ID, "repository": capture.REPO,
        "run_id": "123", "head_sha": "a" * 40, "post_id": 101, "question_id": 202,
        "question_type": "binary", "options": ["Yes", "No"], "prompt": "Frozen original evidence",
        "inconsistent": False, "baseline": [.6, .4],
        "opens_at": (NOW - timedelta(days=1)).isoformat(),
        "captured_at": (NOW - timedelta(minutes=2)).isoformat(),
        "baseline_verified_at": (NOW - timedelta(minutes=1)).isoformat(),
        "closes_at": (NOW + timedelta(hours=1)).isoformat()}


class DispatchTests(unittest.TestCase):
    def test_each_trusted_production_event_dispatches_its_own_source_once(self):
        for kind in ("push", "schedule", "workflow_dispatch"):
            api = Mock()
            api.own_run.return_value = source(event=kind, status="in_progress", conclusion=None)
            result = dispatcher.dispatch_shadow(producer_env(kind), api=api, now=NOW)
            api.own_run.assert_called_once_with(123)
            api.dispatch.assert_called_once_with(123)
            self.assertEqual(result["status"], "one_shadow_dispatch_requested")

    def test_missing_capture_wrong_context_or_leaked_provider_key_never_reads_or_dispatches(self):
        changes = ({"GITHUB_ACTIONS": "false"}, {"GITHUB_REPOSITORY": "other/repo"},
            {"GITHUB_REF": "refs/heads/feature"}, {"GITHUB_RUN_ATTEMPT": "2"},
            {"GITHUB_WORKFLOW_REF": "other.yml"}, {"GITHUB_EVENT_NAME": "pull_request"},
            {"GITHUB_RUN_ID": "123/dispatches"}, {"GITHUB_SHA": "bad"},
            {"VRCHCAB_VERIFIED_FORECASTS": "0"}, {"VRCHCAB_SHADOW_ARTIFACT_ID": ""},
            {"VRCHCAB_SHADOW_ARTIFACT_ID": "0"}, {"METACULUS_TOKEN": "synthetic"},
            {"OPENROUTER_API_KEY": "synthetic"}, {"VRCHCAB_OPENAI_TEST_KEY": "synthetic"})
        for change in changes:
            api = Mock()
            with self.subTest(change=change), self.assertRaises(dispatcher.DispatchShadowError):
                dispatcher.dispatch_shadow(dict(producer_env(), **change), api=api, now=NOW)
            self.assertEqual(api.mock_calls, [])

    def test_foreign_changed_rerun_or_completed_producer_never_dispatches(self):
        changes = ({"id": 124}, {"id": True}, {"repository": {"full_name": "other/repo"}},
            {"head_repository": {"full_name": "other/repo"}}, {"path": worker.WORKFLOW},
            {"head_branch": "feature"}, {"head_sha": "b" * 40}, {"run_attempt": 2},
            {"run_attempt": True}, {"event": "push"}, {"status": "completed"},
            {"conclusion": "cancelled"})
        for change in changes:
            api = Mock()
            api.own_run.return_value = dict(source(status="in_progress", conclusion=None), **change)
            with self.subTest(change=change), self.assertRaises(dispatcher.DispatchShadowError):
                dispatcher.dispatch_shadow(producer_env(), api=api, now=NOW)
            api.dispatch.assert_not_called()

    def test_expiry_stops_before_any_dispatch_or_read(self):
        api = Mock()
        result = dispatcher.dispatch_shadow(producer_env(), api=api, now=dispatcher.ENDS_AT)
        self.assertEqual(result, {"status": "expired_no_dispatch"})
        self.assertEqual(api.mock_calls, [])
        self.assertEqual(dispatcher.ENDS_AT, policy.ENDS_AT)

    def test_fixed_dispatch_body_one_request_and_uncertain_transport_never_retries(self):
        connection = Mock()
        connection.getresponse.return_value.status = 204
        with patch.object(dispatcher.http.client, "HTTPSConnection", return_value=connection) as factory:
            dispatcher.GitHub("synthetic").dispatch(123)
        factory.assert_called_once_with("api.github.com", timeout=20)
        connection.request.assert_called_once()
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("POST", dispatcher.ROOT + "/actions/workflows/openai_shadow.yaml/dispatches"))
        self.assertEqual(json.loads(kwargs["body"]), {"ref": "main", "inputs": {"source_run_id": "123"}})
        connection.close.assert_called_once()
        connection = Mock()
        connection.getresponse.side_effect = TimeoutError("PRIVATE_TOKEN_AND_BODY")
        with patch.object(dispatcher.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(dispatcher.DispatchShadowError, "no_retry") as failure:
                dispatcher.GitHub("synthetic").dispatch(123)
        self.assertNotIn("PRIVATE", str(failure.exception))
        connection.request.assert_called_once()
        connection.close.assert_called_once()

    def test_redirect_or_failed_status_is_not_followed(self):
        for status in (301, 302, 307, 401, 500):
            connection = Mock()
            connection.getresponse.return_value.status = status
            with patch.object(dispatcher.http.client, "HTTPSConnection", return_value=connection) as factory:
                with self.assertRaises(dispatcher.DispatchShadowError):
                    dispatcher.GitHub("synthetic").dispatch(123)
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(connection.request.call_count, 1)

    def test_main_never_prints_exception_content_or_credentials(self):
        output = io.StringIO()
        with patch.object(dispatcher, "dispatch_shadow", side_effect=ValueError("PRIVATE")), redirect_stdout(output):
            self.assertEqual(dispatcher.main(), 1)
        self.assertNotIn("PRIVATE", output.getvalue())


class WorkerSourceTests(unittest.TestCase):
    def test_both_trigger_contexts_supported_but_reruns_remain_denied(self):
        env = producer_env()
        env["GITHUB_WORKFLOW_REF"] = capture.REPO + "/" + worker.WORKFLOW + "@refs/heads/main"
        for kind in ("workflow_run", "workflow_dispatch"):
            with patch.dict(os.environ, dict(env, GITHUB_EVENT_NAME=kind), clear=True):
                worker.check_context()
            with patch.dict(os.environ, dict(env, GITHUB_EVENT_NAME=kind, GITHUB_RUN_ATTEMPT="2"), clear=True):
                with self.assertRaises(policy.ShadowRejected): worker.check_context()

    def test_dispatch_id_is_strict_and_no_untrusted_metadata_is_accepted_from_input(self):
        for value in (None, True, 123, "", "0", "0123", "-1", "123/foo", "123\n", "9" * 21):
            with patch.object(worker, "gh") as call:
                with self.assertRaises(policy.ShadowRejected):
                    worker.resolve_source({"inputs": {"source_run_id": value}}, "workflow_dispatch")
            call.assert_not_called()
        with patch.object(worker, "gh", return_value=source()) as call:
            self.assertEqual(worker.resolve_source(event(), "workflow_dispatch"), source())
        call.assert_called_once_with(worker.ROOT + "/actions/runs/123")

    def test_running_parent_is_rechecked_until_success_before_use(self):
        clock, sleeps = [0.0], []
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        with patch.object(worker, "gh", side_effect=[source(status="in_progress", conclusion=None), source()]) as call:
            result = worker.resolve_source(event(), "workflow_dispatch", sleep=sleep, monotonic=lambda: clock[0])
        self.assertEqual(result["conclusion"], "success")
        self.assertEqual(sleeps, [5])
        self.assertEqual(call.call_count, 2)

    def test_never_completed_parent_wait_is_bounded_and_cannot_claim(self):
        clock, sleeps = [0.0], []
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        with patch.object(worker, "gh", return_value=source(status="in_progress", conclusion=None)) as call, \
                patch.object(worker, "read_state") as read, patch.object(worker, "write_state") as write:
            with self.assertRaisesRegex(policy.ShadowRejected, "source_not_completed_no_claim"):
                worker.resolve_source(event(), "workflow_dispatch", sleep=sleep, monotonic=lambda: clock[0])
        self.assertEqual(sum(sleeps), worker.SOURCE_COMPLETION_WAIT_SECONDS)
        self.assertEqual(call.call_count, 7)
        read.assert_not_called()
        write.assert_not_called()

    def test_untrusted_source_or_failure_never_waits_and_changed_sha_after_wait_is_denied(self):
        changes = ({"id": 124}, {"id": True}, {"run_attempt": 2}, {"run_attempt": True},
            {"repository": {"full_name": "other/repo"}}, {"head_repository": {"full_name": "other/repo"}},
            {"head_branch": "feature"}, {"path": worker.WORKFLOW}, {"head_sha": "bad"},
            {"event": "pull_request"}, {"status": "completed", "conclusion": "failure"})
        for change in changes:
            sleep = Mock()
            with self.subTest(change=change), patch.object(worker, "gh", return_value=source(**change)) as call:
                with self.assertRaises(policy.ShadowRejected):
                    worker.resolve_source(event(), "workflow_dispatch", sleep=sleep)
            self.assertEqual(call.call_count, 1)
            sleep.assert_not_called()
        with patch.object(worker, "gh", side_effect=[source(status="in_progress", conclusion=None), source(head_sha="b" * 40)]):
            with self.assertRaisesRegex(policy.ShadowRejected, "untrusted_source_run"):
                worker.resolve_source(event(), "workflow_dispatch", sleep=lambda _: None)

    def test_workflow_run_still_requires_event_sha_match_and_completed_source(self):
        for record in (source(head_sha="b" * 40), source(status="in_progress", conclusion=None)):
            sleep = Mock()
            with patch.object(worker, "gh", return_value=record):
                with self.assertRaises(policy.ShadowRejected):
                    worker.resolve_source(event("workflow_run"), "workflow_run", sleep=sleep)
            sleep.assert_not_called()

    def test_duplicate_triggers_keep_existing_attempt_and_never_claim_or_read_question_again(self):
        encrypted = capture.cipher("k" * 64).encrypt(worker.canonical(snapshot()))
        for kind in ("workflow_dispatch", "workflow_run"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "event.json"
                path.write_text(json.dumps(event(kind)))
                env = {"GITHUB_EVENT_NAME": kind, "GITHUB_EVENT_PATH": str(path), "METACULUS_TOKEN": "k" * 64}
                with patch.dict(os.environ, env, clear=True), patch.object(worker, "check_context"), \
                        patch.object(worker, "now", return_value=NOW), patch.object(worker, "gh", return_value=source()), \
                        patch.object(worker, "read_state", return_value=({"slots": [{"post_id": 101}]}, "oldsha")), \
                        patch.object(worker, "artifact", return_value=encrypted), patch.object(worker, "check_still_open") as status, \
                        patch.object(worker, "write_state") as write:
                    worker.prepare()
                write.assert_not_called()
                status.assert_not_called()

    def test_old_capture_via_new_dispatch_still_rejected_before_reservation(self):
        item = snapshot()
        item["captured_at"] = (NOW - timedelta(minutes=16)).isoformat()
        encrypted = capture.cipher("k" * 64).encrypt(worker.canonical(item))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps(event()))
            env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_EVENT_PATH": str(path), "METACULUS_TOKEN": "k" * 64}
            with patch.dict(os.environ, env, clear=True), patch.object(worker, "check_context"), \
                    patch.object(worker, "now", return_value=NOW), patch.object(worker, "gh", return_value=source()), \
                    patch.object(worker, "read_state", return_value=({"slots": []}, "oldsha")), \
                    patch.object(worker, "artifact", return_value=encrypted), patch.object(worker, "write_state") as write, \
                    patch.object(worker, "check_still_open") as status:
                with self.assertRaisesRegex(policy.ShadowRejected, "snapshot_not_prospectively_eligible"):
                    worker.prepare()
            write.assert_not_called()
            status.assert_not_called()


if __name__ == "__main__":
    unittest.main()

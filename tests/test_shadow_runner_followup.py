"""Prospective timing and real Metaculus response-shape regressions, offline."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import shadow_capture as capture
import shadow_comparison as policy
import shadow_runner as worker


NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)


def snapshot():
    return {
        "experiment": policy.EXPERIMENT_ID, "repository": capture.REPO,
        "run_id": "123", "head_sha": "a" * 40,
        "post_id": 101, "question_id": 202, "question_type": "binary",
        "options": ["Yes", "No"], "prompt": "Original frozen evidence",
        "inconsistent": False, "baseline": [.6, .4],
        "opens_at": (NOW - timedelta(days=1)).isoformat(),
        "captured_at": (NOW - timedelta(minutes=2)).isoformat(),
        "baseline_verified_at": (NOW - timedelta(minutes=1)).isoformat(),
        "closes_at": (NOW + timedelta(hours=1)).isoformat(),
    }


def source_run():
    return {
        "id": 123, "repository": {"full_name": capture.REPO},
        "head_repository": {"full_name": capture.REPO}, "head_branch": "main",
        "head_sha": "a" * 40, "path": worker.SOURCE_WORKFLOW,
        "event": "workflow_dispatch", "run_attempt": 1, "status": "completed", "conclusion": "success",
    }


def public_post():
    # The repository's real locked-SDK fixtures keep status on the question.
    return {"id": 101, "question": {
        "id": 202, "type": "binary", "status": "open", "resolution": None,
        "open_time": (NOW - timedelta(days=1)).isoformat(),
        "actual_close_time": None,
        "scheduled_close_time": (NOW + timedelta(minutes=2)).isoformat(),
    }}


class ShadowRunnerFollowupTests(unittest.TestCase):
    def test_expected_closed_or_stale_skip_is_healthy_but_configuration_failure_is_not(self):
        from contextlib import redirect_stdout
        import io
        for reason in ('question_closed', 'snapshot_expired_before_reservation', 'inference_permit_stale'):
            out = io.StringIO()
            with patch.object(worker.sys, 'argv', ['shadow_runner.py', 'prepare']), patch.object(worker, 'prepare', side_effect=policy.ShadowRejected(reason)), redirect_stdout(out):
                self.assertEqual(worker.main(), 0)
            self.assertIn('skipped_no_retry', out.getvalue())
        with patch.object(worker.sys, 'argv', ['shadow_runner.py', 'prepare']), patch.object(worker, 'prepare', side_effect=policy.ShadowRejected('ledger_invalid_no_reset')), redirect_stdout(io.StringIO()):
            self.assertEqual(worker.main(), 1)

    def test_snapshot_requires_binary_blind_options_and_ordered_timestamps(self):
        item = snapshot()
        payload = worker.validate_snapshot(item, source_run(), NOW)
        self.assertTrue(payload["input"].startswith(item["prompt"]))
        self.assertNotIn("baseline", payload["input"])
        for change in (
            {"question_type": "multiple_choice"}, {"options": ["No", "Yes"]},
            {"baseline_verified_at": (NOW + timedelta(seconds=1)).isoformat()},
            {"baseline_verified_at": (NOW - timedelta(minutes=3)).isoformat()},
            {"opens_at": (NOW - timedelta(minutes=1)).isoformat()},
        ):
            with self.subTest(change=change), self.assertRaises(policy.ShadowRejected):
                worker.validate_snapshot(dict(item, **change), source_run(), NOW)

    def test_fresh_question_status_and_shortened_close_use_real_nested_shape(self):
        post = public_post()
        with patch.dict(os.environ, {"METACULUS_TOKEN": "synthetic"}, clear=True), \
                patch.object(worker, "now", return_value=NOW), \
                patch.object(worker, "network", return_value=(200, {}, worker.canonical(post))) as call:
            actual = worker.check_still_open(snapshot())
        self.assertEqual(actual, NOW + timedelta(minutes=2))
        call.assert_called_once()
        self.assertEqual(call.call_args.args, ("www.metaculus.com", "/api/posts/101/"))
        self.assertEqual(call.call_args.kwargs["headers"]["Cache-Control"], "no-cache")

    def test_fresh_not_open_resolved_wrong_type_or_identity_is_rejected(self):
        changes = (
            {"status": "closed"}, {"status": None}, {"resolution": "yes"},
            {"type": "multiple_choice"}, {"id": 999}, {"id": True},
            {"open_time": (NOW + timedelta(seconds=1)).isoformat()},
            {"open_time": (NOW - timedelta(minutes=1)).isoformat()},
            {"scheduled_close_time": NOW.isoformat()}, {"scheduled_close_time": None},
        )
        for change in changes:
            post = public_post()
            post["question"].update(change)
            with self.subTest(change=change), \
                    patch.dict(os.environ, {"METACULUS_TOKEN": "synthetic"}, clear=True), \
                    patch.object(worker, "now", return_value=NOW), \
                    patch.object(worker, "network", return_value=(200, {}, worker.canonical(post))) as call:
                with self.assertRaises((policy.ShadowRejected, ValueError)):
                    worker.check_still_open(snapshot())
                call.assert_called_once()

    def test_preparation_clamps_close_and_commits_before_local_permit(self):
        for fresh_close in (NOW + timedelta(minutes=2), NOW + timedelta(hours=2)):
            with self.subTest(fresh_close=fresh_close), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                work = root / "work"
                event = root / "event.json"
                event.write_text(json.dumps({"workflow_run": source_run()}))
                output = root / "output.txt"
                ledger = {"experiment": policy.EXPERIMENT_ID, "schema": 1, "slots": []}
                encrypted = capture.cipher("k" * 64).encrypt(worker.canonical(snapshot()))

                def commit_reserved(data, sha):
                    self.assertFalse((work / "request.json").exists())
                    self.assertFalse(output.exists())
                    self.assertEqual(sha, "oldblob")
                    self.assertEqual(data["slots"][0]["state"], "reserved")
                    self.assertEqual(data["slots"][0]["reserved_usd"], "0.24")
                    return "newblob"

                env = {"GITHUB_EVENT_PATH": str(event), "GITHUB_RUN_ID": "456",
                       "GITHUB_OUTPUT": str(output), "METACULUS_TOKEN": "k" * 64}
                with patch.dict(os.environ, env, clear=True), \
                        patch.object(worker, "WORK", work), \
                        patch.object(worker, "check_context"), \
                        patch.object(worker, "now", return_value=NOW), \
                        patch.object(worker, "gh", return_value=source_run()), \
                        patch.object(worker, "read_state", return_value=(ledger, "oldblob")), \
                        patch.object(worker, "artifact", return_value=encrypted), \
                        patch.object(worker, "check_still_open", return_value=fresh_close), \
                        patch.object(worker, "write_state", side_effect=commit_reserved) as write:
                    worker.prepare()
                write.assert_called_once()
                permit = json.loads((work / "request.json").read_text())
                expected = min(NOW + timedelta(hours=1), fresh_close)
                self.assertEqual(worker.date(permit["closes_at"]), expected)
                self.assertEqual(output.read_text(), "ready=true\n")
                self.assertNotIn("baseline", permit)

    def test_candidate_completed_after_fresh_close_is_failed_and_cost_retained(self):
        request = policy.request_for("Frozen evidence", ["Yes", "No"])
        ctx = {"payload": request, "digest": policy.payload_digest(request), "worker_run": "456",
               "captured_at": NOW.isoformat(), "closes_at": (NOW + timedelta(minutes=2)).isoformat(),
               "prepared_at": NOW.isoformat(), "option_count": 2}
        response = {"model": policy.MODEL, "status": "completed",
                    "usage": {"input_tokens": 100, "output_tokens": 100},
                    "output": [{"type": "message", "status": "completed", "content": [{
                        "type": "output_text", "text": json.dumps({"probabilities": [.7, .3], "rationale": "Frozen evidence"})}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "request.json").write_text(json.dumps(ctx))
            with patch.object(worker, "WORK", work), patch.object(worker, "check_context"), \
                    patch.dict(os.environ, {"GITHUB_RUN_ID": "456", "VRCHCAB_OPENAI_TEST_KEY": "synthetic"}, clear=True), \
                    patch.object(worker, "now", side_effect=[NOW, NOW + timedelta(minutes=3)]), \
                    patch.object(worker, "network", return_value=(200, {}, worker.canonical(response))) as call:
                worker.infer()
            result = json.loads((work / "result.json").read_text())
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["reason"], "candidate_completed_after_close")
            self.assertEqual(result["started_at"], NOW.isoformat())
            self.assertEqual(result["effective_closes_at"], ctx["closes_at"])
            self.assertGreater(float(result["estimated_usd"]), 0)
            self.assertTrue((work / "sent").exists())
            call.assert_called_once()

    def test_future_permit_is_rejected_without_billable_request(self):
        request = policy.request_for("Frozen evidence", ["Yes", "No"])
        ctx = {"payload": request, "digest": policy.payload_digest(request), "worker_run": "456",
               "captured_at": NOW.isoformat(), "closes_at": (NOW + timedelta(hours=1)).isoformat(),
               "prepared_at": (NOW + timedelta(seconds=1)).isoformat(), "option_count": 2}
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "request.json").write_text(json.dumps(ctx))
            with patch.object(worker, "WORK", work), patch.object(worker, "check_context"), \
                    patch.dict(os.environ, {"GITHUB_RUN_ID": "456", "VRCHCAB_OPENAI_TEST_KEY": "synthetic"}, clear=True), \
                    patch.object(worker, "now", return_value=NOW), patch.object(worker, "network") as call:
                with self.assertRaises(policy.ShadowRejected):
                    worker.infer()
            call.assert_not_called()
            self.assertFalse((work / "sent").exists())


if __name__ == "__main__":
    unittest.main()

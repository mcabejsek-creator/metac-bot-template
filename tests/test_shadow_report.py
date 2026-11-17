from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shadow_capture import REPO, cipher
from shadow_comparison import EXPERIMENT_ID, MODEL, ShadowRejected, payload_digest, request_for
import shadow_report as report

N = datetime(2026, 11, 18, tzinfo=timezone.utc)
TOKEN = "k" * 64


def fixture_post(post_id=1, question_id=2):
    return {"id": post_id, "status": "resolved", "question": {
        "id": question_id, "type": "binary", "status": "resolved", "resolution": "yes",
        "actual_close_time": (N + timedelta(hours=1)).isoformat(),
        "actual_resolve_time": (N + timedelta(hours=2)).isoformat()}}


def fixture_ledger(state="success"):
    prompt = "PRIVATE_FROZEN_RESEARCH"
    snapshot = {"experiment": EXPERIMENT_ID, "post_id": 1, "question_id": 2,
        "repository": REPO, "question_type": "binary", "options": ["Yes", "No"],
        "prompt": prompt, "baseline": [.7, .3], "captured_at": N.isoformat(),
        "opens_at": (N - timedelta(hours=1)).isoformat(), "closes_at": (N + timedelta(hours=1)).isoformat(),
        "baseline_verified_at": (N + timedelta(minutes=1)).isoformat(), "head_sha": "a" * 40, "run_id": "123"}
    result = {"state": state, "model": MODEL, "probabilities": [.8, .2],
        "rationale": "PRIVATE_CANDIDATE_RATIONALE", "started_at": (N + timedelta(minutes=2)).isoformat(),
        "completed_at": (N + timedelta(minutes=3)).isoformat(),
        "effective_closes_at": (N + timedelta(hours=1)).isoformat()}
    enc = lambda value: cipher(TOKEN).encrypt(json.dumps(value).encode()).decode()
    return {"experiment": EXPERIMENT_ID, "schema": 1, "slots": [{
        "state": state, "post_id": 1, "question_id": 2, "source_run": 123,
        "digest": payload_digest(request_for(prompt, ["Yes", "No"])), "reserved_usd": "0.24",
        "snapshot": enc(snapshot), "result": enc(result) if state != "reserved" else None}]}


class ShadowReportTests(unittest.TestCase):
    def test_daily_workflow_only_has_read_credentials_and_aggregate_artifact(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.load((root / '.github/workflows/shadow_report.yaml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        self.assertEqual(workflow['on']['schedule'], [{'cron': '11 8 * * *'}])
        steps = workflow['jobs']['report']['steps']
        live = next(step for step in steps if step.get('name') == 'Evaluate recorded pairs using read-only outcomes')
        self.assertEqual(set(live['env']), {'GITHUB_TOKEN', 'METACULUS_TOKEN'})
        self.assertEqual(live['run'], 'poetry run python -B shadow_report.py --output shadow-report.json')
        self.assertNotIn('VRCHCAB_OPENAI_TEST_KEY', str(workflow))
        self.assertNotIn('env', workflow)
        self.assertNotIn('env', workflow['jobs']['report'])
        artifact = steps[-1]['with']
        self.assertEqual(artifact['path'], 'shadow-report.json')

    def reader(self, value=None, *, status=200, transport=None):
        payload = fixture_post() if value is None else value
        call = transport or (lambda *a, **kw: (status, {}, json.dumps(payload).encode()))
        return report.OutcomeReader(TOKEN, transport=call, clock=lambda: 0, sleep=lambda _: None)

    def test_resolved_pair_uses_actual_times_and_prints_only_aggregates(self):
        ledger = fixture_ledger()
        unchanged = deepcopy(ledger)
        result = report.build_report(ledger, TOKEN, self.reader(), N + timedelta(hours=3))
        self.assertEqual(ledger, unchanged)
        self.assertEqual(result["ledger_attempts"], 1)
        self.assertEqual(result["reserved_usd"], "0.24")
        self.assertEqual(result["metaculus_get_attempts"], 1)
        self.assertEqual(result["outcome_read_counts"]["resolved"], 1)
        self.assertEqual(result["evaluation"]["counts"]["scored"], 1)
        text = json.dumps(result)
        for private in (TOKEN, "PRIVATE_FROZEN_RESEARCH", "PRIVATE_CANDIDATE_RATIONALE", "probabilities", "baseline_verified_at"):
            self.assertNotIn(private, text)

    def test_empty_ledger_has_zero_requests_and_does_not_reset_budget(self):
        ledger = {"experiment": EXPERIMENT_ID, "schema": 1, "slots": []}
        reader = self.reader(transport=lambda *a, **kw: self.fail("No GET for empty ledger"))
        result = report.build_report(ledger, TOKEN, reader, N)
        self.assertEqual(result["metaculus_get_attempts"], 0)
        self.assertEqual(result["ledger_attempts"], 0)
        self.assertEqual(result["evaluation"]["counts"]["scored"], 0)

    def test_failed_reserved_and_undecryptable_records_never_trigger_get(self):
        for state in ("failed", "reserved", "success"):
            ledger = fixture_ledger(state)
            if state == "success": ledger["slots"][0]["snapshot"] = "not ciphertext"
            reader = self.reader(transport=lambda *a, **kw: self.fail("No GET for ineligible slot"))
            result = report.build_report(ledger, TOKEN, reader, N)
            self.assertEqual(result["metaculus_get_attempts"], 0)
            self.assertEqual(result["reserved_usd"], "0.24")
            self.assertEqual(result["decrypt_failures"], int(state == "success"))

    def test_early_actual_close_excludes_late_candidate(self):
        post = fixture_post()
        post["question"]["actual_close_time"] = (N + timedelta(minutes=2, seconds=30)).isoformat()
        result = report.build_report(fixture_ledger(), TOKEN, self.reader(post), N + timedelta(hours=3))
        self.assertEqual(result["evaluation"]["counts"]["scored"], 0)
        self.assertEqual(result["evaluation"]["counts"]["excluded"], 1)

    def test_missing_actual_close_has_no_scheduled_fallback(self):
        post = fixture_post()
        del post["question"]["actual_close_time"]
        post["question"]["scheduled_close_time"] = (N + timedelta(hours=1)).isoformat()
        result = report.build_report(fixture_ledger(), TOKEN, self.reader(post), N + timedelta(hours=3))
        self.assertEqual(result["outcome_read_counts"]["missing_timing"], 1)
        self.assertEqual(result["evaluation"]["counts"]["scored"], 0)

    def test_question_status_and_numeric_timestamps_match_existing_review_shapes(self):
        post = fixture_post()
        del post["status"]
        post["question"]["actual_close_time"] = (N + timedelta(hours=1)).timestamp()
        post["question"]["actual_resolve_time"] = (N + timedelta(hours=2)).timestamp()
        reader = self.reader(post)
        outcome = reader.read(1, 2, N + timedelta(hours=3))
        self.assertEqual(outcome["closed_at"], (N + timedelta(hours=1)).isoformat())
        self.assertEqual(outcome["resolved_at"], (N + timedelta(hours=2)).isoformat())
        self.assertEqual(reader.counts["resolved"], 1)

    def test_unresolved_and_unscored_are_not_errors(self):
        for status, resolution, count in (("open", None, "unresolved"), ("resolved", "annulled", "unscored")):
            post = fixture_post()
            post["status"] = post["question"]["status"] = status
            post["question"]["resolution"] = resolution
            result = report.build_report(fixture_ledger(), TOKEN, self.reader(post), N + timedelta(hours=3))
            self.assertEqual(result["outcome_read_counts"][count], 1)
            self.assertEqual(result["evaluation"]["counts"]["scored"], 0)

    def test_wrong_identity_nonbinary_and_group_payloads_are_rejected(self):
        for change in ("id", "question_id", "type", "group"):
            post = fixture_post()
            if change == "id": post["id"] = 99
            elif change == "question_id": post["question"]["id"] = 99
            elif change == "type": post["question"]["type"] = "multiple_choice"
            else: post["group_of_questions"] = {}
            reader = self.reader(post)
            self.assertIsNone(reader.read(1, 2, N + timedelta(hours=3)))
            self.assertEqual(reader.counts["invalid_response"], 1)

    def test_no_retry_on_http_failure_or_timeout_and_only_fixed_get(self):
        for failure in (False, True):
            calls = []
            def transport(host, path, **kwargs):
                calls.append((host, path, kwargs))
                if failure: raise TimeoutError("PRIVATE_EXCEPTION")
                return 429, {}, b"PRIVATE_PROVIDER_RESPONSE"
            reader = self.reader(transport=transport)
            self.assertIsNone(reader.read(1, 2, N))
            self.assertEqual(reader.get_attempts, 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][:2], ("www.metaculus.com", "/api/posts/1/"))
            self.assertEqual(calls[0][2]["method"], "GET")
            self.assertNotIn("body", calls[0][2])
            self.assertEqual(reader.counts["unavailable"], 1)

    def test_twenty_get_cap_and_six_second_spacing(self):
        delays = []
        reader = report.OutcomeReader(TOKEN, transport=lambda *a, **kw: (503, {}, b""),
                                      clock=lambda: 0, sleep=delays.append)
        for index in range(20): reader.read(index + 1, index + 2, N)
        with self.assertRaises(ShadowRejected): reader.read(21, 22, N)
        self.assertEqual(reader.get_attempts, 20)
        self.assertEqual(delays, [6] * 19)

    def test_context_denies_model_keys_wrong_workflow_or_rerun(self):
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": REPO, "GITHUB_REF": "refs/heads/main",
               "GITHUB_RUN_ATTEMPT": "1", "GITHUB_EVENT_NAME": "schedule",
               "GITHUB_WORKFLOW_REF": REPO + "/" + report.WORKFLOW + "@refs/heads/main"}
        report.check_context(env)
        for change in ({"VRCHCAB_OPENAI_TEST_KEY": "PRIVATE"}, {"OPENAI_API_KEY": "PRIVATE"},
                       {"GITHUB_RUN_ATTEMPT": "2"}, {"GITHUB_EVENT_NAME": "pull_request"},
                       {"GITHUB_WORKFLOW_REF": "another workflow"}):
            with self.assertRaises(ShadowRejected): report.check_context(dict(env, **change))

    def test_main_reads_ledger_only_and_writes_sanitized_report(self):
        ledger = {"experiment": EXPERIMENT_ID, "schema": 1, "slots": []}
        with tempfile.TemporaryDirectory() as directory:
            old = os.getcwd()
            try:
                os.chdir(directory)
                output = io.StringIO()
                with patch.object(report, "check_context"), patch.object(report, "read_state", return_value=(ledger, "sha")) as read, patch.dict(os.environ, {"METACULUS_TOKEN": TOKEN}, clear=True), redirect_stdout(output):
                    self.assertEqual(report.main(["--output", "shadow-report.json"]), 0)
                read.assert_called_once_with()
                saved = json.loads(Path("shadow-report.json").read_text())
                self.assertEqual(saved["metaculus_get_attempts"], 0)
                self.assertNotIn(TOKEN, output.getvalue())
            finally:
                os.chdir(old)


if __name__ == "__main__":
    unittest.main()

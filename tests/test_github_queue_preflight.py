"""All synthetic; no GitHub network, no model calls, no publishing."""

from pathlib import Path

from .support import OfflineTestCase
from .test_github_publisher_queue_readonly import FakeGitHub, NOW, run as api_run
from github_publisher_queue_readonly import (
    GitHubPublisherQueueReader, KNOWN_STUCK_RUN_ID,
)
from github_queue_preflight import GitHubQueuePreflightSource
from autonomous_orchestrator import (
    AutonomousPreflightRejected, IndependentReadings,
    build_dispatch_snapshot,
)
from tools.diagnose_github_queue_readonly import (
    APPROVAL, run as run_cli,
)

ROOT = Path(__file__).resolve().parents[1]


def reader(fake):
    return GitHubPublisherQueueReader(
        token="SYNTHETIC_GITHUB_READ_ONLY", opener=fake, clock=lambda: NOW,
    )


class GitHubQueuePreflightTests(OfflineTestCase):
    def test_real_reader_adapter_never_fabricates_provider_or_operator_proof(self):
        fake = FakeGitHub()
        data = GitHubQueuePreflightSource(reader(fake)).read()
        self.assertIsInstance(data, IndependentReadings)
        self.assertEqual(data.github_checked_at, NOW)
        self.assertEqual(data.active_publishers, 0)
        self.assertEqual(data.queued_publishers, 0)
        self.assertIsNone(data.perplexity_checked_at)
        self.assertIsNone(data.funded_openrouter_checked_at)
        self.assertIsNone(data.journal_checked_at)
        self.assertFalse(data.perplexity_spend_ok)
        self.assertFalse(data.funded_openrouter_ok)
        self.assertFalse(data.operator_approval_confirmed)
        self.assertFalse(data.automation_switch_confirmed)
        self.assertFalse(data.prior_publication_certain)
        with self.assertRaises(AutonomousPreflightRejected):
            build_dispatch_snapshot(data, now=NOW, last_dispatch_at=None)
        self.assertEqual(len(fake.calls), 5)

    def test_queued_old_run_passes_through_as_blocking_evidence(self):
        fake = FakeGitHub({
            "queued": [api_run(KNOWN_STUCK_RUN_ID, "queued")],
        })
        data = GitHubQueuePreflightSource(reader(fake)).read()
        self.assertEqual(data.queued_publishers, 1)
        self.assertFalse(data.automation_switch_confirmed)

    def test_wrong_reader_type_denied(self):
        for value in (None, object(), lambda: None):
            with self.subTest(value=value), self.assertRaises(Exception):
                GitHubQueuePreflightSource(value)

    def test_manual_script_requires_explicit_exact_approval(self):
        for authorization in (None, "", "DO_NOT_RUN", "yes", True):
            fake = FakeGitHub()
            env = {"VRCHCAB_GITHUB_QUEUE_READ_APPROVAL": authorization}
            with self.subTest(authorization=authorization), self.assertRaises(Exception):
                run_cli(env=env, reader=reader(fake))
            self.assertEqual(fake.calls, [])

    def test_readonly_diagnostic_reports_real_blockers_no_credentials(self):
        fake = FakeGitHub({"queued": [api_run(KNOWN_STUCK_RUN_ID, "queued")]})
        outcome = run_cli(
            env={"VRCHCAB_GITHUB_QUEUE_READ_APPROVAL": APPROVAL},
            reader=reader(fake),
        )
        self.assertEqual(outcome["status"], "blocked_by_publisher_queue")
        self.assertEqual(outcome["queued_publisher_runs"], 1)
        self.assertEqual(outcome["publishing_run_ids"], [KNOWN_STUCK_RUN_ID])
        self.assertTrue(outcome["known_stuck_run_34463156990_queued"])
        self.assertFalse(outcome["automatic_publication_authorized"])
        self.assertNotIn("SYNTHETIC_GITHUB_READ_ONLY", str(outcome))
        self.assertEqual(len(fake.calls), 5)
        self.assertTrue(all(method == "GET" for method, _, _ in fake.calls))

    def test_clear_queue_is_not_authorized_automatic_publication(self):
        fake = FakeGitHub()
        outcome = run_cli(
            env={"VRCHCAB_GITHUB_QUEUE_READ_APPROVAL": APPROVAL},
            reader=reader(fake),
        )
        self.assertEqual(outcome["status"], "no_current_publishers_observed")
        self.assertFalse(outcome["automatic_publication_authorized"])

    def test_manual_workflow_is_default_denied_no_schedule_or_paid_secrets(self):
        wf = (ROOT / ".github/workflows/manual_github_queue_diagnostic.yaml").read_text()
        self.assertIn("workflow_dispatch:", wf)
        self.assertNotIn("\n  schedule:", wf)
        self.assertIn("default: DO_NOT_RUN", wf)
        self.assertIn("github.run_attempt == 1", wf)
        self.assertIn("actions: read", wf)
        self.assertIn("contents: read", wf)
        self.assertNotIn("contents: write", wf)
        self.assertNotIn("secrets.PERPLEXITY_API_KEY", wf)
        self.assertNotIn("secrets.METACULUS_TOKEN", wf)
        self.assertNotIn("secrets.OPENROUTER_API_KEY", wf)
        self.assertIn("VRCHCAB_GITHUB_QUEUE_READ_APPROVAL", wf)
        self.assertNotIn("--automatic-one", wf)
        self.assertNotIn("workflow_dispatch -", wf)

    def test_production_workflow_never_imports_manual_status_diagnostic(self):
        wf = (ROOT / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        main = (ROOT / "main.py").read_text()
        self.assertNotIn("diagnose_github_queue_readonly", wf)
        self.assertNotIn("github_queue_preflight", main)
        self.assertNotIn("\n  schedule:", wf)
        self.assertIn("default: DO_NOT_RUN", wf)


if __name__ == "__main__":
    import unittest
    unittest.main()

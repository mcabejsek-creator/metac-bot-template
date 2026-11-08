"""Offline contract for a DISABLED cron-job.org to GET-only GitHub watch.

Never contacts cron-job.org, GitHub, Metaculus, or any model. The template must
not include a real token or target a live forecasting workflow.
"""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/external_readonly_metaculus_watch.yaml"
TEMPLATE = ROOT / "config/cronjob_external_readonly_disabled.template.json"
WATCHER = ROOT / "tools/watch_open_metaculus_questions_readonly.py"
LIVE_PUBLISHERS = (
    "run_bot_on_tournament.yaml",
    "run_bot_on_metaculus_cup.yaml",
    "test_bot.yaml",
)
EXPECTED_URL = (
    "https://api.github.com/repos/mcabejsek-creator/metac-bot-template/"
    "actions/workflows/external_readonly_metaculus_watch.yaml/dispatches"
)


class ExternalReadOnlyScheduleContract(unittest.TestCase):
    def test_watcher_has_only_explicit_dispatch_no_implicit_triggers(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("on:\n  workflow_dispatch:", workflow)
        for forbidden in ("\n  schedule:", "\n  push:", "\n  repository_dispatch:",
                          "\n  workflow_run:"):
            self.assertNotIn(forbidden, workflow)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("id-token: write", workflow)
        self.assertIn("default: DO_NOT_RUN", workflow)
        self.assertEqual(workflow.count("inputs.mode == 'CHECK_OPEN_POSTS_READ_ONLY'"), 2)
        self.assertEqual(workflow.count("github.run_attempt == 1"), 2)
        self.assertEqual(workflow.count("github.ref == 'refs/heads/main'"), 2)
        self.assertEqual(workflow.count("persist-credentials: false"), 2)
        self.assertEqual(workflow.count("needs: offline_safety"), 1)
        self.assertIn(
            "run: python -B tools/watch_open_metaculus_questions_readonly.py "
            "--check-public-open-posts", workflow
        )
        self.assertIn(
            "run: python -B tools/watch_open_metaculus_questions_readonly.py "
            "--self-check", workflow
        )
        self.assertEqual(workflow.count("secrets.METACULUS_TOKEN_V2"), 1)
        for dangerous in ("secrets.PERPLEXITY_API_KEY", "secrets.OPENAI_API_KEY",
                          "secrets.ANTHROPIC_API_KEY", "secrets.OPENROUTER_API_KEY",
                          "run: poetry", "run: python -B main.py", "bounded_run"):
            self.assertNotIn(dangerous, workflow)

    def test_cronjob_template_is_off_targets_only_get_watch(self):
        data = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"job"})
        job = data["job"]
        self.assertIs(job["enabled"], False)
        self.assertIs(job["saveResponses"], False)
        self.assertEqual(job["requestMethod"], 1)  # cron-job.org POST
        self.assertEqual(job["url"], EXPECTED_URL)
        for disallowed in ("run_bot_on_tournament", "run_bot_on_metaculus_cup",
                           "test_bot.yaml", "/api/posts/"):
            self.assertNotIn(disallowed, job["url"])
        self.assertIs(job["redirectSuccess"], False)
        self.assertEqual(job["schedule"]["timezone"], "UTC")
        self.assertEqual(job["schedule"]["minutes"], [1, 16, 31, 46])
        self.assertEqual(job["schedule"]["hours"], [-1])
        headers = job["extendedData"]["headers"]
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Accept"], "application/vnd.github+json")
        self.assertEqual(headers["X-GitHub-Api-Version"], "2026-03-10")
        self.assertEqual(
            headers["Authorization"],
            "Bearer REPLACE_IN_CRON_JOB_ORG_CONSOLE_ONLY",
        )
        payload = json.loads(job["extendedData"]["body"])
        self.assertEqual(payload, {
            "ref": "main", "inputs": {"mode": "CHECK_OPEN_POSTS_READ_ONLY"}
        })
        self.assertTrue(job["notification"]["onFailure"])
        self.assertTrue(job["notification"]["onDisable"])

    def test_external_dispatch_must_not_override_live_publisher_default_off(self):
        # Actions:write token permission is repository-wide. The workflow's
        # narrow URL is necessary but not a token-scoped security boundary.
        for name in LIVE_PUBLISHERS:
            with self.subTest(name=name):
                live = (ROOT / ".github/workflows" / name).read_text()
                self.assertIn("default: DO_NOT_RUN", live)
                self.assertIn("inputs.publication_approval == "
                              "'APPROVE_ONE_LIVE_PUBLICATION_RUN'", live)
                self.assertIn("github.run_attempt == 1", live)

    def test_underlying_metaculus_watcher_only_builds_get_requests(self):
        source = WATCHER.read_text(encoding="utf-8")
        self.assertIn('method="GET"', source)
        self.assertIn("NoRedirect()", source)
        self.assertIn("MAX_BODY_BYTES", source)
        self.assertIn("MIN_REQUEST_SPACING", source)
        self.assertIn('for label, project in TARGETS:', source)
        self.assertIn('if total:', source)
        self.assertNotIn("post_binary_question_prediction", source)
        self.assertNotIn("post_numeric_question_prediction", source)
        self.assertNotIn("post_question_comment", source)


if __name__ == "__main__":
    unittest.main()

import subprocess
from pathlib import Path

import yaml

from .support import OfflineTestCase


class ActionsConfigurationTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        self.workflows = {p.name: yaml.load(p.read_text(), Loader=yaml.BaseLoader) for p in root.glob("*.yaml")}
        self.publishers = ("run_bot_on_tournament.yaml", "run_bot_on_metaculus_cup.yaml", "test_bot.yaml")

    def test_all_writers_and_initializer_use_the_same_group_and_state(self):
        names = (*self.publishers, "initialize_publication_state.yaml")
        reference = self.workflows[names[0]]
        for name in names:
            with self.subTest(workflow=name):
                workflow = self.workflows[name]
                self.assertEqual(workflow["concurrency"], reference["concurrency"])
                self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
                self.assertEqual(workflow["env"], reference["env"])
                self.assertEqual(workflow["env"]["METACULUS_PUBLICATION_BACKEND"], "github")
                self.assertEqual(workflow["permissions"], {"contents": "write"})
                self.assertNotIn("github.ref", workflow["concurrency"]["group"])
                self.assertNotIn("github.run", workflow["concurrency"]["group"])

    def test_original_forecast_triggers_and_commands_are_preserved(self):
        expected = {
            "run_bot_on_tournament.yaml": ({"workflow_dispatch"}, "poetry run python main.py"),
            "test_bot.yaml": ({"workflow_dispatch"}, "poetry run python main.py --mode test_questions"),
            "run_bot_on_metaculus_cup.yaml": ({"workflow_dispatch", "schedule"}, "poetry run python main.py --mode metaculus_cup"),
        }
        for name, (triggers, command) in expected.items():
            workflow = self.workflows[name]
            self.assertEqual(set(workflow["on"]), triggers)
            step = next(s for s in workflow["jobs"]["forecast_job"]["steps"] if s.get("name") == "Run bot")
            supervised = step["run"].strip()
            self.assertTrue(supervised.startswith("poetry run python -B -m bounded_run -- "))
            self.assertEqual("poetry run " + supervised.split(" -- ", 1)[1], command)
            self.assertEqual(step["env"]["GITHUB_TOKEN"], "${{ github.token }}")
            self.assertEqual(step["env"]["METACULUS_TOKEN"], "${{ secrets.METACULUS_TOKEN }}")
        self.assertEqual(self.workflows["run_bot_on_metaculus_cup.yaml"]["on"]["schedule"], [{"cron": "0 0 */2 * *"}])

    def test_source_checkout_does_not_persist_state_write_credentials(self):
        for name in (*self.publishers, "initialize_publication_state.yaml"):
            steps = next(iter(self.workflows[name]["jobs"].values()))["steps"]
            checkout = next(step for step in steps if step.get("uses") == "actions/checkout@v4")
            self.assertEqual(checkout["with"]["persist-credentials"], "false")

    def test_publishing_job_budget_includes_supervisor_and_setup_margin(self):
        from runtime_limits import RuntimeLimits
        for name in self.publishers:
            minutes = int(self.workflows[name]["jobs"]["forecast_job"]["timeout-minutes"])
            self.assertEqual(minutes, 210)
            self.assertGreaterEqual(minutes * 60, RuntimeLimits().run_seconds + 60 + 15 * 60)

    def test_offline_regressions_gate_bot_and_initialization_before_secret_step(self):
        for name in (*self.publishers, "initialize_publication_state.yaml"):
            steps = next(iter(self.workflows[name]["jobs"].values()))["steps"]
            checks = next(i for i, step in enumerate(steps) if step.get("name") == "Offline publication regressions")
            secret_step = next(i for i, step in enumerate(steps) if "METACULUS_TOKEN" in step.get("env", {}))
            self.assertLess(checks, secret_step)
            self.assertEqual(steps[checks]["run"], "poetry run python -B -m tests.run")
            self.assertNotIn("env", steps[checks])

    def test_review_is_read_only_and_uses_its_existing_schedule(self):
        workflow = self.workflows["review_bot.yaml"]
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(workflow["on"]["schedule"], [{"cron": "0 6 * * 1"}])
        self.assertNotIn("env", workflow)
        self.assertNotEqual(workflow["concurrency"]["group"], self.workflows[self.publishers[0]]["concurrency"]["group"])

    def test_review_input_cannot_inject_a_publisher_outside_the_shared_lock(self):
        workflow = self.workflows["review_bot.yaml"]
        step = next(step for step in workflow["jobs"]["review_job"]["steps"] if step.get("name") == "Review forecasts")
        self.assertEqual(step["env"]["REVIEW_DAYS"], "${{ inputs.days || 30 }}")
        self.assertNotIn("${{", step["run"])
        self.assertIn('--resolved-since "$REVIEW_DAYS"', step["run"])
        # Execute only the validation prefix, never the review command/API client.
        validation = step["run"].split("poetry run", 1)[0]
        marker = self.publication_store.path.parent / "must-not-exist"
        for value in ("", "-1", "30; touch " + str(marker), "$(touch " + str(marker) + ")"):
            result = subprocess.run(["/bin/bash", "-c", validation],
                env={"REVIEW_DAYS": value}, capture_output=True, text=True, timeout=2)
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        for value in ("0", "30"):
            result = subprocess.run(["/bin/bash", "-c", validation],
                env={"REVIEW_DAYS": value}, capture_output=True, text=True, timeout=2)
            self.assertEqual(result.returncode, 0)

    def test_initialization_is_explicit_manual_and_does_not_run_the_bot(self):
        workflow = self.workflows["initialize_publication_state.yaml"]
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        confirmation = workflow["on"]["workflow_dispatch"]["inputs"]["confirmation"]
        self.assertEqual(confirmation["default"], "not-confirmed")
        step = workflow["jobs"]["initialize"]["steps"][-1]
        self.assertIn('--confirm-prior-runs-reconciled', step["run"])
        self.assertIn('"$PRIOR_RUNS_ACK"', step["run"])
        self.assertNotIn('inputs.confirmation', step["run"])
        self.assertNotIn('main.py', step["run"])
        self.assertEqual(step["env"]["PRIOR_RUNS_ACK"], "${{ inputs.confirmation }}")

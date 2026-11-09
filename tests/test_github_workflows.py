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

    def test_forecast_triggers_and_explicit_live_commands_are_preserved(self):
        expected = {
            "run_bot_on_tournament.yaml": ({"workflow_dispatch"}, '.venv/bin/python -B main.py --mode tournament --first-live-post-id "$FIRST_LIVE_POST_ID"'),
            "test_bot.yaml": ({"workflow_dispatch"}, ".venv/bin/python -B main.py --mode test_questions"),
            "run_bot_on_metaculus_cup.yaml": ({"workflow_dispatch"}, ".venv/bin/python -B main.py --mode metaculus_cup"),
        }
        for name, (triggers, command) in expected.items():
            workflow = self.workflows[name]
            self.assertEqual(set(workflow["on"]), triggers)
            step = next(s for s in workflow["jobs"]["forecast_job"]["steps"] if s.get("name") == "Run bot")
            supervised = step["run"].strip()
            self.assertTrue(supervised.startswith(".venv/bin/python -B -m bounded_run -- "))
            self.assertEqual(supervised.split(" -- ", 1)[1], command)
            self.assertEqual(step["env"]["GITHUB_TOKEN"], "${{ github.token }}")
            self.assertEqual(step["env"]["METACULUS_TOKEN"], "${{ secrets.METACULUS_TOKEN_V2 }}")
            self.assertNotIn("${{ secrets.METACULUS_TOKEN }}", step["env"].values())
            if name == "run_bot_on_tournament.yaml":
                self.assertEqual(step["env"]["FIRST_LIVE_POST_ID"],
                                 "${{ inputs.first_live_post_id }}")
                job_gate = workflow["jobs"]["forecast_job"]["if"]
                self.assertIn("inputs.first_live_post_id != 'DO_NOT_RUN'", job_gate)
                self.assertIn("inputs.publication_approval == 'APPROVE_ONE_LIVE_PUBLICATION_RUN'", job_gate)
                self.assertIn("github.run_attempt == 1", job_gate)
            else:
                self.assertNotIn("FIRST_LIVE_POST_ID", step["env"])
        self.assertNotIn("schedule", self.workflows["run_bot_on_metaculus_cup.yaml"]["on"])

    def test_real_supervisor_cli_accepts_python_flags_without_poetry_parser(self):
        # Uses only the local interpreter, a synthetic child and no API imports.
        # Regression for the 2026-11-07 failed live launch before the bot started.
        import sys
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-B", "-m", "bounded_run", "--",
             sys.executable, "-B", "-c", "print('OFFLINE_SUPERVISOR_EXECUTED')"],
            cwd=root, capture_output=True, text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OFFLINE_SUPERVISOR_EXECUTED", result.stdout)

    def test_source_checkout_does_not_persist_state_write_credentials(self):
        for name in (*self.publishers, "initialize_publication_state.yaml"):
            steps = next(iter(self.workflows[name]["jobs"].values()))["steps"]
            checkout = next(step for step in steps
                            if step.get("uses", "").partition("@")[0] == "actions/checkout")
            self.assertEqual(checkout["with"]["persist-credentials"], "false")

    def test_all_active_actions_use_verified_official_commit_pins(self):
        reviewed = {
            "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",  # v4.4.0
            "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",  # v5.6.0
            "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",  # v4.6.2
            "snok/install-poetry": "a783c322200f0519c7926aa6faa857c4e23e9263",  # v1.4.2
        }

        def references(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "uses":
                        yield item
                    else:
                        yield from references(item)
            elif isinstance(value, list):
                for item in value:
                    yield from references(item)

        seen = set()
        for name, workflow in self.workflows.items():
            for reference in references(workflow):
                with self.subTest(workflow=name, action=reference):
                    action, separator, revision = reference.partition("@")
                    self.assertEqual(separator, "@")
                    self.assertIn(action, reviewed)
                    self.assertRegex(revision, r"^[0-9a-f]{40}$")
                    self.assertEqual(revision, reviewed[action])
                    seen.add(action)
        self.assertEqual(seen, set(reviewed))

    def test_v3_admission_and_effect_only_edits_trigger_offline_ci(self):
        # Future PRs may touch only these V3 modules; no silent CI bypass.
        workflow = self.workflows["offline_bounded_pagination.yaml"]
        monitored = workflow["on"]["pull_request"]["paths"]
        expected = {
            "reserve_bind_offline.py",
            "create_effect_offline.py",
            "tests/test_reserve_bind_offline.py",
            "tests/test_create_effect_offline.py",
            "docs/v3_reserve_bind_offline_20261109.md",
            "docs/create_fixed_effect_offline_20261109.md",
            "docs/v3_integration_after_pr41_20261109.md",
        }
        self.assertEqual(len(monitored), len(set(monitored)), "Duplicate path filters")
        self.assertTrue(expected.issubset(set(monitored)), sorted(expected - set(monitored)))
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertNotIn("workflow_dispatch", workflow["on"])

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
        self.assertIn('--confirm-legacy-frozen-no-retries', step["run"])
        self.assertNotIn("--confirm-prior-runs-reconciled", step["run"])
        self.assertEqual(confirmation["options"], ["not-confirmed", "legacy-frozen-no-retries"])
        self.assertIn('"$PRIOR_RUNS_ACK"', step["run"])
        self.assertNotIn('inputs.confirmation', step["run"])
        self.assertNotIn('main.py', step["run"])
        self.assertEqual(step["env"]["PRIOR_RUNS_ACK"], "${{ inputs.confirmation }}")

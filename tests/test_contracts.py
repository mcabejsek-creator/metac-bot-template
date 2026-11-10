from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from .support import FIXTURES, OfflineTestCase, app, question
from forecasting_tools import BinaryQuestion, ConditionalQuestion, ReasonedPrediction


class PromptContractTests(OfflineTestCase):
    async def capture(self, case_id):
        q = question(case_id)
        bot = self.bot()
        kind = FIXTURES[case_id]["question"]["kind"]
        helper = {"binary": "_binary_prompt_to_forecast", "multiple_choice": "_multiple_choice_prompt_to_forecast",
                  "numeric": "_numeric_prompt_to_forecast", "date": "_date_prompt_to_forecast"}[kind]
        entry = {"binary": "_run_forecast_on_binary", "multiple_choice": "_run_forecast_on_multiple_choice",
                 "numeric": "_run_forecast_on_numeric", "date": "_run_forecast_on_date"}[kind]
        probe = AsyncMock(return_value=None)
        with patch.object(bot, helper, probe):
            await getattr(bot, entry)(q, "Local research; no new facts or criteria.")
        return q, probe.await_args.args[1]

    async def test_criteria_fine_print_and_background_preserved_for_all_types(self):
        for case_id in ("japanese_diet", "fda", "gpr", "date", "cbo", "lake_mead"):
            with self.subTest(case=case_id):
                q, prompt = await self.capture(case_id)
                self.assertIn(q.resolution_criteria, prompt)
                self.assertIn(q.fine_print, prompt)
                if q.background_info:
                    self.assertIn(q.background_info, prompt)

    async def test_close_zero_is_distinct_from_future_resolution_across_types(self):
        for case_id in ("japanese_diet", "gpr", "fda"):
            with self.subTest(case=case_id):
                _, prompt = await self.capture(case_id)
                expected = FIXTURES[case_id]["expected"]
                self.assertIn("Days until close: 0", prompt)
                self.assertIn(f"Days until scheduled resolution: {expected['days_until_resolution']}", prompt)

    async def test_binary_protects_close_vs_real_event_deadline(self):
        _, prompt = await self.capture("japanese_diet")
        self.assertIn("Never use zero or expired days until close as evidence for Yes or No", prompt)
        self.assertIn("A later scheduled resolution time does not extend that event window", prompt)
        self.assertIn("Do not add any motive, purpose, threshold, exclusion", prompt)

    def test_actual_close_time_overrides_scheduled_close_and_falls_back(self):
        bot = self.bot()
        q = question("japanese_diet")
        self.assertEqual(bot._get_effective_close_time(q), q.close_time)
        q.api_json = {"question": {"actual_close_time": "2026-11-05T12:00:00Z"}}
        self.assertEqual(bot._get_effective_close_time(q).isoformat(), "2026-11-05T12:00:00+00:00")
        q.api_json["question"]["actual_close_time"] = None
        self.assertEqual(bot._get_effective_close_time(q), q.close_time)

    async def check_effective_close_context(self, case_id, entry, helper):
        q = question(case_id)
        q.api_json = {"question": {"actual_close_time": "2026-11-05T12:00:00Z"}}
        bot = self.bot()
        probe = AsyncMock(return_value=None)
        with patch.object(bot, helper, probe):
            await getattr(bot, entry)(q, "Local research")
        expected = f"Close time: {bot._get_effective_close_time(q)}"
        self.assertTrue(expected in probe.await_args.args[1],
                           "Forecast uses scheduled close while research uses actual_close_time")

    async def test_numeric_and_research_use_same_effective_close(self):
        await self.check_effective_close_context("gpr", "_run_forecast_on_numeric", "_numeric_prompt_to_forecast")

    async def test_mc_and_research_use_same_effective_close(self):
        await self.check_effective_close_context("fda", "_run_forecast_on_multiple_choice", "_multiple_choice_prompt_to_forecast")

    async def test_forecaster_denominator_and_elapsed_time_are_not_interchanged(self):
        q, prompt = await self.capture("forecasters")
        expected = FIXTURES["forecasters"]["expected"]
        self.assertEqual((q.close_time.date() - q.open_time.date()).days, 730)
        self.assertAlmostEqual(q.num_forecasters / 730, expected["hard_lower_bound"])
        self.assertIn("Days since open: 61", prompt)
        self.assertIn("Total open duration in days: 730", prompt)
        self.assertIn("use days since open", prompt)
        self.assertIn("180/730", prompt)

    async def test_numeric_hard_bound_rules_preserved(self):
        clauses = {
            "forecasters": "never place any forecast percentile below that confirmed minimum",
            "vvix_maximum": "an observed maximum is a hard lower bound",
            "window_minimum": "an observed minimum is a hard upper bound",
            "percentage": "the hard lower bound is 0% and the hard upper bound is 100%",
            "percentage_difference": "-100 to +100 percentage points",
            "unesco": "Historical values, creator-provided likely ranges",
        }
        for case_id, clause in clauses.items():
            with self.subTest(case=case_id):
                _, prompt = await self.capture(case_id)
                self.assertIn(clause, prompt)
        self.assertEqual(FIXTURES["vvix_maximum"]["expected"]["hard_lower_bound"], max([101, 115, 109]))
        self.assertEqual(FIXTURES["window_minimum"]["expected"]["hard_upper_bound"], min([110, 90, 100]))

    async def test_numeric_units_and_strict_percentile_order_preserved(self):
        q, prompt = await self.capture("cbo")
        self.assertIn(q.unit_of_measure, prompt)
        self.assertIn("P10 < P20 < P40 < P60 < P80 < P90", prompt)

    async def test_mc_resolution_options_and_no_status_quo_shortcut(self):
        q, prompt = await self.capture("fda")
        for option in q.options:
            self.assertIn(option, prompt)
        self.assertIn("do not treat the current leader", prompt)
        self.assertIn("sum to exactly 100%", prompt)

    async def test_research_retains_exact_metric_year_and_window_rules(self):
        calls = self.agent()
        bot = self.bot()
        bot.set_llm("perplexity-agent", "researcher")
        await bot.run_research(question("vvix_maximum"))
        prompt = calls[0]["payload"]["input"]
        for clause in (
            "Preserve the resolution formula exactly as written",
            "preserve the exact pairing between each date/year and its value",
            "do not substitute a broader, adjacent, or similarly named metric",
            "maximum or minimum of a numerical series over a fixed future window",
            "comparable windows of the same length",
            "observed\" means an actual realized value",
            "inspect those archives before concluding",
        ):
            self.assertIn(clause, prompt)

    def test_nominal_range_does_not_replace_true_closed_bounds(self):
        q = question("percentage").model_copy(update={"nominal_lower_bound": 20, "nominal_upper_bound": 80})
        upper, lower = self.bot()._create_upper_and_lower_bound_messages(q)
        self.assertTrue("100.0" in upper and "0.0" in lower,
                           f"Prompt presents nominal 20..80 as hard bounds instead of {q.lower_bound}..{q.upper_bound}")

    async def test_parent_reasoning_reaches_child_and_conditional_branches(self):
        parts = {name: BinaryQuestion(question_text=name, conditional_type=name) for name in ("parent", "child", "yes", "no")}
        q = ConditionalQuestion(question_text="Synthetic conditional", parent=parts["parent"], child=parts["child"],
                                question_yes=parts["yes"], question_no=parts["no"])
        bot = self.bot()
        seen = {}

        async def prediction(part, research):
            seen[part.question_text] = research
            return ReasonedPrediction(prediction_value=.5, reasoning=part.question_text.upper() + "_REASON")

        with patch.object(bot, "_make_prediction", prediction):
            await bot._run_forecast_on_conditional(q, "Original facts")
        self.assertTrue(all("PARENT_REASON" in seen[name] for name in ("child", "yes", "no")),
                           "Parent reasoning is lost before subsequent forecasts")


class WorkflowContractTests(OfflineTestCase):
    def workflows(self):
        root = Path(__file__).resolve().parents[1]
        return {p.name: yaml.load(p.read_text(), Loader=yaml.BaseLoader)
                for p in (root / ".github" / "workflows").glob("*.yaml")}

    def test_production_workflow_remains_manual(self):
        # The handover confirms this is intentional; do not restore historical cron.
        self.assertEqual(set(self.workflows()["run_bot_on_tournament.yaml"]["on"]), {"workflow_dispatch"})

    def test_forecasting_jobs_have_explicit_wall_clock_limits(self):
        missing = []
        for name, workflow in self.workflows().items():
            if name == "review_bot.yaml":
                continue
            for job in workflow["jobs"].values():
                value = job.get("timeout-minutes")
                if value is None or not str(value).isdigit() or not 0 < int(value) <= 360:
                    missing.append(name)
        self.assertFalse(missing, f"Explicit timeout-minutes missing/invalid: {missing}")

    def test_publishing_workflows_share_an_account_lock(self):
        workflows = self.workflows()
        groups = [workflows[name].get("concurrency", {}).get("group") for name in
                  ("run_bot_on_tournament.yaml", "run_bot_on_metaculus_cup.yaml", "test_bot.yaml")]
        self.assertTrue(all(groups) and len(set(groups)) == 1 and "github.workflow" not in groups[0],
                        f"Publishing workflows do not share an account-wide lock: {groups}")

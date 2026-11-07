import asyncio
from unittest.mock import AsyncMock, patch

from .support import LocalMetaculus, OfflineTestCase, app, question
from forecasting_tools import BinaryReport, ForecastBot


class PipelineTests(OfflineTestCase):
    async def test_real_binary_pipeline_and_json_round_trip(self):
        calls = self.model()
        bot = self.bot(enable_summarize_research=True)
        report = await bot.forecast_question(question("japanese_diet"))
        self.assertIsInstance(report, BinaryReport)
        self.assertEqual(report.prediction, .65)
        self.assertEqual(report.errors, [])
        self.assertEqual(sum("You are a data analyst" in p for p in calls), 12)
        self.assertEqual(sum("Please summarize" in p for p in calls), 2)
        self.assertEqual(len(calls), 20)  # 6 forecast + 12 parser + 2 summary
        self.assertEqual(BinaryReport.model_validate_json(report.model_dump_json()).prediction, .65)
        self.assertFalse(bot.metaculus_client.forecasts)

    async def test_already_forecasted_question_is_skipped(self):
        calls = self.model()
        bot = self.bot()
        q = question("japanese_diet").model_copy(update={"already_forecasted": True})
        self.assertEqual(await bot.forecast_questions([q]), [])
        self.assertEqual(calls, [])

    async def test_question_limit_also_bounds_forecast_calls(self):
        active = peak = 0
        entered = asyncio.Event()
        release = asyncio.Event()

        async def invoke(model, prompt, *args, **kwargs):
            nonlocal active, peak
            if "You are a data analyst" in str(prompt):
                return '{"prediction_in_decimal": 0.65}'
            active += 1
            peak = max(peak, active)
            entered.set()
            try:
                await release.wait()
                return "Probability of YES: 65%"
            finally:
                active -= 1

        self.start_patch(patch.object(app.GeneralLlm, "invoke", invoke))
        bot = self.bot()
        questions = [question("japanese_diet").model_copy(update={"id_of_question": i}) for i in range(3)]
        task = asyncio.create_task(bot.forecast_questions(questions))
        try:
            await entered.wait()
            # Drain ready coroutines; the barrier, not wall-clock speed, drives overlap.
            for _ in range(50):
                await asyncio.sleep(0)
        finally:
            release.set()
        self.assertEqual(len(await task), 3)
        self.assertTrue(peak <= bot._max_concurrent_questions,
                           f"Question limit is 1 but {peak} forecast calls overlapped")

    async def test_two_workers_do_not_publish_same_snapshot_twice(self):
        self.model()
        sink = LocalMetaculus()
        workers = [self.bot(publish_reports_to_metaculus=True, metaculus_client=sink) for _ in range(2)]
        # Both workers read the same not-yet-forecasted snapshot, as separate runs can.
        reports = await asyncio.gather(*[
            worker.forecast_question(question("japanese_diet")) for worker in workers
        ], return_exceptions=True)
        self.assertEqual(len(reports), 2)
        self.assertTrue(any(isinstance(report, BinaryReport) for report in reports))
        # Blocking publication now yields to the loop. A competing worker may
        # observe sending and must fail closed, then restore the completed plan.
        from publication_journal import PublicationUncertain
        for worker, report in zip(workers, reports):
            if isinstance(report, Exception):
                self.assertIsInstance(report.__cause__, PublicationUncertain)
                self.assertIsInstance(await worker.forecast_question(question("japanese_diet")), BinaryReport)
        self.assertEqual(len(sink.forecasts), 1)
        self.assertEqual(len(sink.comments), 1)

    async def test_partial_publication_failure_is_visible(self):
        self.model()
        sink = LocalMetaculus(fail_comment=True)
        bot = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        reports = await bot.forecast_questions([question("japanese_diet")], return_exceptions=True)
        self.assertIsInstance(reports[0], Exception)
        self.assertEqual(len(sink.forecasts), 1)
        self.assertEqual(sink.comments, [])

    def test_summary_raises_for_failed_questions(self):
        # Corrects an earlier suspicion: locked SDK raises by default, even though
        # the outer forecasting call used return_exceptions=True.
        with self.assertRaises(RuntimeError):
            ForecastBot.log_report_summary([RuntimeError("Synthetic research failure")])

    async def test_binary_extremes_keep_current_production_clamp(self):
        for probability, expected in ((.001, .01), (.999, .99)):
            with self.subTest(probability=probability):
                self.model(f'{{"prediction_in_decimal": {probability}}}')
                result = await self.bot()._binary_prompt_to_forecast(question("japanese_diet"), "Local fixture")
                self.assertEqual(result.prediction_value, expected)

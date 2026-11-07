"""Lossless stage boundaries, with synthetic data and the normal offline guards."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from forecasting_tools import BinaryReport, GeneralLlm, MetaculusClient, PredictionAffirmed, ReasonedPrediction
from forecasting_tools.data_models.forecast_report import ResearchWithPredictions
from forecasting_tools.data_models.timestamped_predictions import BinaryTimestampedPrediction

from .support import NOW, OfflineTestCase, agent_response, question
from .test_forecast_validation import api_post, capture, conditional, identified
from .test_github_publication import GitHubTestCase
from forecast_validation import question_contract
from question_context import ContextIntegrityError, QuestionContext, TRUNCATION_SUFFIX


# Indentation, an interior blank line, code fences and a late rule are intentional.
CRITERIA = "Exact source, fiscal year 2028 only.\n    FY2027: 7.772\n    FY2028: 8.151\n\n        Exclude adjacent metrics.\nCRITERIA_END"
FINE = "Binding conditions:\n    Window: 2028-01-01 through 2028-12-31 UTC.\n        Count only eligible final records.\nFINE_END"
EVIDENCE = "Verified table:\n    year | value (trillion USD)\n    2027 | 7.772\n    2028 | 8.151\n\n```\n# This is source content, not a report section\n    2029 | unavailable\n```\nEVIDENCE_END"


def rich_question(case="cbo"):
    return question(case).model_copy(update={
        "question_text": "Question title\n    Indented title continuation",
        "resolution_criteria": CRITERIA, "fine_print": FINE,
        "background_info": "Official background\n    Preserve this qualification.\nBACKGROUND_END",
        "unit_of_measure": "trillion USD",
        "scheduled_resolution_time": datetime(2029, 2, 1, tzinfo=timezone.utc),
    })


class TimingIntegrityTests(OfflineTestCase):
    def test_effective_close_accepts_epoch_zero_and_normalizes_utc(self):
        bot = self.bot()
        for raw, expected in ((0, "1970-01-01T00:00:00+00:00"),
                              ("2026-11-07T00:30:00+02:00", "2026-11-06T22:30:00+00:00"),
                              ("2026-11-05T12:00:00", "2026-11-05T12:00:00+00:00")):
            with self.subTest(raw=raw):
                q = question("gpr").model_copy(update={"api_json": {"question": {"actual_close_time": raw}}})
                self.assertEqual(bot._get_effective_close_time(q).isoformat(), expected)

    async def test_invalid_authoritative_close_fails_without_research_or_publication(self):
        calls = self.model()
        bot = self.bot(publish_reports_to_metaculus=True)
        for raw in (True, "not-a-date", float("nan"), {}, ""):
            with self.subTest(raw=raw):
                q = identified("japanese_diet").model_copy(update={"api_json": {"question": {"actual_close_time": raw}}})
                result = await bot.forecast_questions([q], return_exceptions=True)
                self.assertIsInstance(result[0], Exception)
        self.assertEqual(calls, [])
        self.assertFalse(bot.metaculus_client.forecasts)

    def test_naive_model_dates_are_rejected_instead_of_using_host_timezone(self):
        q = question("gpr").model_copy(update={"open_time": datetime(2026, 1, 1)})
        with self.assertRaises(ContextIntegrityError):
            QuestionContext.capture(q)

    async def test_all_forecast_types_use_utc_and_distinguish_three_time_roles(self):
        bot = self.bot()
        for case, kind in (("japanese_diet", "binary"), ("fda", "multiple_choice"), ("gpr", "numeric"), ("date", "date")):
            with self.subTest(kind=kind):
                q = rich_question(case).model_copy(update={
                    "open_time": datetime(2026, 11, 6, 0, 30, tzinfo=timezone(timedelta(hours=2))),
                    "close_time": datetime(2027, 1, 1, tzinfo=timezone.utc),
                    "api_json": {"question": {"actual_close_time": "2026-11-06T00:30:00+02:00"}},
                })
                probe = AsyncMock(return_value=None)
                with patch.object(bot, "_" + kind + "_prompt_to_forecast", probe):
                    await getattr(bot, "_run_forecast_on_" + kind)(q, EVIDENCE)
                prompt = probe.await_args.args[1]
                self.assertIn("Today is 2026-11-06", prompt)
                self.assertIn("Close time: 2026-11-05 22:30:00+00:00", prompt)
                self.assertIn("Days until close: -1", prompt)
                self.assertIn("Scheduled resolution time: 2029-02-01", prompt)
                self.assertIn('"actual_resolution_time": null', prompt)
                self.assertIn('"scheduled_close_time": "2027-01-01T00:00:00+00:00"', prompt)
                self.assertIn(CRITERIA, prompt)  # FY2028, not the 2029 resolution year
                self.assertIn(FINE, prompt)
                self.assertIn(EVIDENCE, prompt)
                if kind == "multiple_choice":
                    self.assertNotIn("Do not calculate or infer a different remaining time horizon.", prompt)
                    self.assertIn("Do not infer the target period from the close time or the scheduled resolution time", prompt)
                if kind == "numeric":
                    self.assertIn("Days since open: 1", prompt)
                    self.assertIn("Total open duration in days: 0", prompt)

    def test_contract_keeps_scheduled_and_actual_times_separate(self):
        q = rich_question().model_copy(update={"close_time": NOW, "api_json": {"question": {"actual_close_time": 0}}})
        packet = QuestionContext.capture(q).render()
        self.assertIn("Close time (effective submission deadline): 1970-01-01", packet)
        self.assertIn("Scheduled close time: " + q.close_time.isoformat(), packet)
        self.assertIn("Scheduled resolution time: 2029-02-01", packet)
        self.assertIn("Target period: specified by the question text and resolution criteria/fine print", packet)
        self.assertIn(FINE, packet)


class BoundContextTests(OfflineTestCase):
    def test_each_closed_side_uses_true_bound_and_each_open_side_keeps_nominal_hint(self):
        bot = self.bot()
        for lower_open, upper_open in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(lower_open=lower_open, upper_open=upper_open):
                q = question("percentage").model_copy(update={
                    "nominal_lower_bound": 20.0, "nominal_upper_bound": 80.0,
                    "open_lower_bound": lower_open, "open_upper_bound": upper_open})
                upper, lower = bot._create_upper_and_lower_bound_messages(q)
                self.assertIn("20.0" if lower_open else "0.0", lower)
                self.assertIn("80.0" if upper_open else "100.0", upper)
                self.assertIn("likely" if lower_open else "can not", lower)
                self.assertIn("likely" if upper_open else "can not", upper)

    def test_zero_nominal_hint_remains_valid_for_open_bound(self):
        q = question("percentage_difference").model_copy(update={"open_lower_bound": True, "nominal_lower_bound": 0.0})
        self.assertIn("likely not lower than 0.0", self.bot()._create_upper_and_lower_bound_messages(q)[1])

    def test_date_bound_messages_preserve_intraday_utc_instead_of_rounding(self):
        lower = datetime(2026, 12, 1, 15, 20, 30, tzinfo=timezone(timedelta(hours=2)))
        upper = datetime(2027, 12, 1, 11, 40, 50, tzinfo=timezone.utc)
        q = question("date").model_copy(update={"lower_bound": lower, "upper_bound": upper})
        high, low = self.bot()._create_upper_and_lower_bound_messages(q)
        self.assertIn("2026-12-01T13:20:30+00:00", low)
        self.assertIn("2027-12-01T11:40:50+00:00", high)

    async def test_both_research_phases_receive_scale_units_hard_bounds_and_raw_rules(self):
        calls = self.agent([agent_response(EVIDENCE), agent_response(EVIDENCE)])
        bot = self.bot()
        bot.set_llm("perplexity-agent", "researcher")
        q = rich_question().model_copy(update={"lower_bound": 0.0, "upper_bound": 100.0,
            "nominal_lower_bound": 20.0, "nominal_upper_bound": 80.0,
            "open_lower_bound": False, "open_upper_bound": False})
        self.assertEqual(await bot.run_research(q), EVIDENCE)
        for call in calls:
            prompt = call["payload"]["input"]
            for text in (CRITERIA, FINE, q.background_info, '"units": "trillion USD"',
                         '"lower_bound": 0.0', '"upper_bound": 100.0', '"open_lower_bound": false',
                         '"nominal_upper_bound": 80.0', '"zero_point": null'):
                self.assertIn(text, prompt)
        self.assertIn(EVIDENCE, calls[1]["payload"]["input"])


class StageContextTests(OfflineTestCase):
    async def test_expanded_group_preserves_original_member_and_shared_rules_in_all_stages(self):
        post = api_post("binary")
        member = {**post["question"], "label": "Fiscal year 2028", "fine_print": "MEMBER_CUTOFF_2028",
                  "resolution_criteria": "MEMBER_METRIC", "description": "MEMBER_QUALIFICATION"}
        group = {"questions": [member], "fine_print": "SHARED_FINE_PRINT", "description": "SHARED_BACKGROUND",
                 "resolution_criteria": "SHARED_CRITERIA"}
        q = MetaculusClient._unpack_group_question({"id": 201, "group_of_questions": group})[0]
        # Reproduce actual locked SDK behavior: the parsed member has group prose.
        self.assertEqual(q.fine_print, "SHARED_FINE_PRINT")
        calls = self.agent()
        bot = self.bot()
        bot.set_llm("perplexity-agent", "researcher")
        await bot.run_research(q)
        forecast = AsyncMock(return_value=None)
        with patch.object(bot, "_binary_prompt_to_forecast", forecast):
            await bot._run_forecast_on_binary(q, EVIDENCE)
        for prompt in [call["payload"]["input"] for call in calls] + [forecast.await_args.args[1]]:
            for text in ("Fiscal year 2028", "MEMBER_CUTOFF_2028", "MEMBER_METRIC", "MEMBER_QUALIFICATION",
                         "SHARED_FINE_PRINT", "SHARED_CRITERIA", "SHARED_BACKGROUND"):
                self.assertIn(text, prompt)
        self.assertIn("MEMBER_CUTOFF_2028", QuestionContext.capture(q).render())

    async def test_late_fine_print_and_multiline_options_reach_fact_check(self):
        calls = self.agent()
        q = rich_question("fda").model_copy(update={"fine_print": FINE + "\n" + "x" * 12000 + "\nLATE_BINDING_RULE",
                                                   "options": ["First\n    qualification", "Second", "Third"]})
        bot = self.bot()
        bot.set_llm("perplexity-agent", "researcher")
        await bot.run_research(q)
        prompt = calls[1]["payload"]["input"]
        self.assertIn(q.fine_print, prompt)
        self.assertIn('"First\\n    qualification"', prompt)
        self.assertIn("BACKGROUND_END", prompt)

    async def test_short_units_do_not_corrupt_protected_context(self):
        q = rich_question().model_copy(update={"unit_of_measure": "a", "resolution_criteria": "a\n    a detail"})
        bot = self.bot()
        probe = AsyncMock(return_value=None)
        with patch.object(bot, "_numeric_prompt_to_forecast", probe):
            await bot._run_forecast_on_numeric(q, EVIDENCE)
        prompt = probe.await_args.args[1]
        self.assertIn(q.resolution_criteria, prompt)
        self.assertIn(EVIDENCE, prompt)
        self.assertNotIn("CONTEXT_SLOT_", prompt)

    async def test_summary_sees_full_contract_and_keeps_evidence_for_summary_only_mode(self):
        calls = self.model()
        bot = self.bot(enable_summarize_research=True, use_research_summary_to_forecast=True)
        q = rich_question()
        summary = await bot.summarize_research(q, EVIDENCE)
        self.assertEqual(len(calls), 1)
        for text in (CRITERIA, FINE, EVIDENCE, q.background_info, '"units": "trillion USD"'):
            self.assertIn(text, calls[0])
            self.assertIn(text, summary)
        self.assertIn("Synthetic summary.", summary)

    async def test_default_full_research_mode_and_ensemble_remain_unchanged(self):
        self.model()
        bot = self.bot(enable_summarize_research=True, use_research_summary_to_forecast=False)
        seen = []

        async def prediction(q, research):
            seen.append(research)
            return ReasonedPrediction(prediction_value=.65, reasoning="Local forecast")

        with patch.object(bot, "run_research", AsyncMock(return_value=EVIDENCE)), patch.object(bot, "_make_prediction", prediction):
            await bot.forecast_question(question("japanese_diet"))
        self.assertEqual(seen, [EVIDENCE] * 6)

    async def test_summary_only_pipeline_cannot_lose_rows_omitted_by_summary_model(self):
        calls = self.model()
        bot = self.bot(enable_summarize_research=True, use_research_summary_to_forecast=True)
        with patch.object(bot, "run_research", AsyncMock(return_value=EVIDENCE)):
            await bot.forecast_question(rich_question("japanese_diet"))
        forecasts = [prompt for prompt in calls if "You are a professional forecaster" in prompt]
        self.assertEqual(len(forecasts), 6)
        for prompt in forecasts:
            self.assertIn(EVIDENCE, prompt)
            self.assertIn("Synthetic summary.", prompt)

    async def test_empty_summary_cannot_be_the_forecast_input(self):
        bot = self.bot(enable_summarize_research=True, use_research_summary_to_forecast=True)
        with patch.object(GeneralLlm, "invoke", AsyncMock(return_value="  ")):
            with self.assertRaises(ContextIntegrityError):
                await bot.summarize_research(rich_question(), EVIDENCE)

    async def test_concurrent_summaries_do_not_share_question_context(self):
        prompts = []

        async def invoke(model, prompt, *args, **kwargs):
            prompts.append(prompt)
            await asyncio.sleep(0)
            return "Local summary"

        self.start_patch(patch.object(GeneralLlm, "invoke", invoke))
        bot = self.bot(enable_summarize_research=True, use_research_summary_to_forecast=True)
        a = question("japanese_diet").model_copy(update={"fine_print": "ONLY_QUESTION_A"})
        b = question("japanese_diet").model_copy(update={"fine_print": "ONLY_QUESTION_B"})
        results = await asyncio.gather(bot.summarize_research(a, "Evidence A"), bot.summarize_research(b, "Evidence B"))
        for text in (*prompts, *results):
            self.assertNotEqual("ONLY_QUESTION_A" in text, "ONLY_QUESTION_B" in text)

    async def test_parser_receives_unabridged_reasoning_and_contract_for_every_atomic_type(self):
        reason = EVIDENCE + "\n" + "long source row\n" * 900 + "LATE_PERCENTILE_RULE"
        bot = self.bot()
        for case, kind, reply in (
            ("japanese_diet", "binary", '{"prediction_in_decimal": 0.65}'),
            ("fda", "multiple_choice", None),
            ("percentage", "numeric", '[{"percentile":0.1,"value":20},{"percentile":0.9,"value":80}]'),
            ("date", "date", '[{"percentile":0.1,"value":"2027-01-01"},{"percentile":0.9,"value":"2027-10-01"}]'),
        ):
            with self.subTest(kind=kind):
                q = rich_question(case)
                if reply is None:
                    import json
                    reply = json.dumps({"predicted_options": [{"option_name": name, "probability": p}
                                                               for name, p in zip(q.options, (.3, .5, .2))]})
                seen = []

                async def invoke(model, prompt, *args, **kwargs):
                    seen.append(prompt)
                    return reply if "You are a data analyst" in prompt else reason

                with patch.object(GeneralLlm, "invoke", invoke):
                    result = await getattr(bot, "_" + kind + "_prompt_to_forecast")(q, "Forecast fixture")
                self.assertEqual(result.reasoning, reason)
                self.assertEqual(len(seen), 3)  # One forecast, same two parser validation samples.
                for prompt in seen[1:]:
                    for text in (reason, CRITERIA, FINE, q.background_info, "trillion USD", "2029-02-01"):
                        self.assertIn(text, prompt)
                    self.assertNotIn("CONTEXT_SLOT_", prompt)

    async def test_conditional_chain_keeps_all_prior_reasoning_and_indentation(self):
        bot, q, seen = self.bot(), conditional(), {}
        labels = {part.id_of_question: label for label, part in q.get_all_subquestions().items()}

        async def predict(part, research):
            label = labels[part.id_of_question]
            seen[label] = research
            return ReasonedPrediction(prediction_value=.5, reasoning=label + "\n    Nested rule\nEND_" + label)

        with patch.object(bot, "_make_prediction", predict):
            result = await bot._run_forecast_on_conditional(q, EVIDENCE)
        ordered = ("parent", "child", "question_yes", "question_no")
        for index, label in enumerate(ordered):
            self.assertIn(EVIDENCE, seen[label])
            for previous in ordered[:index]:
                self.assertIn(previous + "\n    Nested rule\nEND_" + previous, seen[label])
            self.assertIn(label + "\n    Nested rule\nEND_" + label, result.reasoning)

    async def test_affirmed_parent_value_reaches_child_without_new_forecast(self):
        bot, q = self.bot(), conditional()
        prior = BinaryTimestampedPrediction(prediction_in_decimal=.37, timestamp=NOW - timedelta(days=1), timestamp_end=None)
        q.parent.previous_forecasts = [prior]
        seen = {}

        async def predict(part, research):
            seen[part.id_of_question] = research
            return ReasonedPrediction(prediction_value=.5, reasoning="Local branch")

        with patch.object(bot, "_make_prediction", predict):
            result = await bot._run_forecast_on_conditional(q, EVIDENCE)
        self.assertIsInstance(result.prediction_value.parent, PredictionAffirmed)
        self.assertNotIn(q.parent.id_of_question, seen)
        self.assertIn("37", seen[q.child.id_of_question])
        self.assertIn("Already existing forecast reaffirmed", seen[q.question_no.id_of_question])

    async def test_expired_or_explicitly_forced_parent_still_uses_original_forecast_path(self):
        for forced in (False, True):
            with self.subTest(forced=forced):
                bot, q = self.bot(), conditional()
                q.parent.previous_forecasts = [BinaryTimestampedPrediction(
                    prediction_in_decimal=.37, timestamp=NOW - timedelta(days=1),
                    timestamp_end=None if forced else NOW - timedelta(seconds=1))]
                if forced:
                    bot.force_reforecast_in_conditional = ["parent"]
                predicted = AsyncMock(return_value=ReasonedPrediction(prediction_value=.6, reasoning=EVIDENCE))
                with patch.object(bot, "_make_prediction", predicted):
                    result, research = await bot._get_question_prediction_info(q.parent, "Facts", "parent")
                self.assertEqual(predicted.await_count, 1)
                self.assertEqual(result.prediction_value, .6)
                self.assertIn(EVIDENCE, research)

    async def test_conditional_original_rules_reach_all_branches_after_sdk_reformatting(self):
        bot, q = self.bot(), conditional()
        q.parent.fine_print, q.child.resolution_criteria = FINE, CRITERIA
        # Branch SDK prose may have lost source indentation before reaching us.
        q.question_yes.fine_print = "Reformatted binding conditions"
        q.question_no.resolution_criteria = "Reformatted source metric"
        seen = []

        async def predict(part, research):
            seen.append(research)
            return ReasonedPrediction(prediction_value=.5, reasoning="Local branch reasoning")

        with patch.object(bot, "_make_prediction", predict):
            await bot._run_forecast_on_conditional(q, EVIDENCE)
        self.assertEqual(len(seen), 4)
        for research in seen:
            for source in (FINE, CRITERIA, EVIDENCE):
                self.assertIn(source, research)


class ExplanationContextTests(OfflineTestCase):
    async def test_report_keeps_full_sources_late_rules_and_three_sdk_sections(self):
        self.model()
        bot = self.bot()
        research = EVIDENCE + "\n" + "research row\n" * 1000 + "LATE_SOURCE_RULE"
        reasoning = EVIDENCE + "\n" + "forecast row\n" * 1000 + "LATE_REASON_RULE"

        async def predict(q, evidence):
            self.assertEqual(evidence, research)
            return ReasonedPrediction(prediction_value=.65, reasoning=reasoning)

        with patch.object(bot, "run_research", AsyncMock(return_value=research)), patch.object(bot, "_make_prediction", predict):
            report = await bot.forecast_question(rich_question("japanese_diet"))
        self.assertEqual(len(report.report_sections), 3)
        self.assertIn("Research", report.research)
        self.assertIn("LATE_REASON_RULE", report.first_rationale)
        decoded = "\n".join(line[4:] if line.startswith("    ") else line for line in report.explanation.split("\n"))
        for source in (research, reasoning, CRITERIA, FINE):
            self.assertIn(source, decoded)
        self.assertNotIn(TRUNCATION_SUFFIX, report.explanation)
        self.assertNotIn("CONTEXT_SLOT_", report.explanation)

    async def test_oversized_explanation_is_rejected_before_journal_or_forecast_write(self):
        bot = self.bot(publish_reports_to_metaculus=True)
        q = identified("japanese_diet")
        with patch.object(bot, "run_research", AsyncMock(return_value="Verified facts")), patch.object(
            bot, "_make_prediction", AsyncMock(return_value=ReasonedPrediction(prediction_value=.65, reasoning="x" * 30000))):
            result = await bot.forecast_questions([q], return_exceptions=True)
        self.assertIsInstance(result[0], Exception)
        self.assertIn("without losing context", str(result[0]))
        self.assertFalse(bot.metaculus_client.forecasts)
        self.assertFalse(bot.metaculus_client.comments)
        self.assertIsNone(self.publication_store.report(bot._publication_key(q)))

    def test_explanation_builder_keeps_inputs_unchanged(self):
        bot, q = self.bot(), question("japanese_diet")
        collection = ResearchWithPredictions(research_report=EVIDENCE, summary_report="Summary",
            predictions=[ReasonedPrediction(prediction_value=.65, reasoning=EVIDENCE)])
        before = collection.model_dump_json()
        bot._create_unified_explanation(q, [collection], .65, 0, 1)
        self.assertEqual(collection.model_dump_json(), before)


class ActionsContextRecoveryTests(GitHubTestCase):
    async def test_fresh_runner_blocks_changed_timing_before_resuming_rejected_comment(self):
        self.model()
        bot = self.bot(publish_reports_to_metaculus=True)
        bot.publication_store = self.runner()
        q = identified("japanese_diet")
        bot._publication_runtime()
        key = bot._publication_key(q)
        report = BinaryReport(question=q, prediction=.65, explanation="# Local complete report")
        operations = await capture(report)
        bot._publication_store.prepare(key, report.model_dump_json(), operations)
        bot._publication_store.claim(key, 0)
        bot._publication_store.finish(key, 0, "confirmed")
        bot._publication_store.claim(key, 1)
        bot._publication_store.finish(key, 1, "rejected")
        before = bot._publication_store.steps(key)
        newer = q.model_copy(update={"scheduled_resolution_time": q.scheduled_resolution_time + timedelta(days=1)})
        second = self.bot(publish_reports_to_metaculus=True)
        second.publication_store = self.runner()  # Independent store/client; fake remote state only.
        with self.assertRaises(Exception) as caught:
            await second.forecast_question(newer)
        self.assertIn("constraints changed", str(caught.exception))
        self.assertEqual(second._publication_store.steps(key), before)
        self.assertFalse(second.metaculus_client.forecasts)
        self.assertFalse(second.metaculus_client.comments)

    async def test_old_truncated_comment_is_blocked_with_confirmed_forecast_untouched(self):
        bot = self.bot(publish_reports_to_metaculus=True)
        bot.publication_store = self.runner()
        bot._publication_runtime()
        q = identified("japanese_diet")
        key = bot._publication_key(q)
        report = BinaryReport(question=q, prediction=.65, explanation="# Legacy partial report\n" + TRUNCATION_SUFFIX)
        bot._publication_store.prepare(key, report.model_dump_json(), await capture(report))
        bot._publication_store.claim(key, 0)
        bot._publication_store.finish(key, 0, "confirmed")
        before = bot._publication_store.steps(key)
        second = self.bot(publish_reports_to_metaculus=True)
        second.publication_store = self.runner()
        with self.assertRaises(Exception) as caught:
            await second.forecast_question(q)
        self.assertIn("without losing context", str(caught.exception))
        self.assertEqual(second._publication_store.steps(key), before)
        self.assertFalse(second.metaculus_client.forecasts)
        self.assertFalse(second.metaculus_client.comments)

    def test_timing_binding_does_not_bind_transient_participation_or_query_time(self):
        q = identified("japanese_diet")
        changed_counts = q.model_copy(update={"num_forecasters": 99999, "num_predictions": 999999})
        self.assertEqual(question_contract(q), question_contract(changed_counts))
        for field in ("open_time", "close_time", "scheduled_resolution_time", "actual_resolution_time"):
            with self.subTest(field=field):
                changed = q.model_copy(update={field: NOW + timedelta(days=999)})
                self.assertNotEqual(question_contract(q), question_contract(changed))

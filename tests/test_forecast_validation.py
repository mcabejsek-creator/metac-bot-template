import copy
import json
import os
import time
from unittest.mock import patch

from forecasting_tools import (
    BinaryQuestion, BinaryReport, ConditionalPrediction, ConditionalQuestion,
    NumericDistribution, Percentile, PredictedOptionList,
)
from forecasting_tools.data_models.conditional_report import ConditionalReport
from forecasting_tools.data_models.data_organizer import DataOrganizer
from forecasting_tools.data_models.questions import MetaculusQuestion

from .support import LocalMetaculus, OfflineTestCase, app, question
from forecast_validation import (
    ForecastValidationError, numeric_payload, raw_parser_prediction, validate_cdf,
    validate_plan, validate_prediction, validate_question,
)
from publication import CapturingClient, SingleAttemptWriter, restore_report
from publication_journal import PublicationUncertain


def identified(case):
    return question(case).model_copy(update={"id_of_question": 101, "id_of_post": 201})


def distribution(q):
    lower = q.lower_bound.timestamp() if q.question_type == "date" else q.lower_bound
    upper = q.upper_bound.timestamp() if q.question_type == "date" else q.upper_bound
    return NumericDistribution.from_question([
        Percentile(percentile=p, value=lower + (upper - lower) * p) for p in (.1, .2, .4, .6, .8, .9)], q)


def mc(q):
    return PredictedOptionList.model_validate({"predicted_options": [
        {"option_name": name, "probability": p} for name, p in zip(q.options, (.3, .5, .2))]})


def conditional(child_kind="binary"):
    parent = identified("japanese_diet").model_copy(update={"id_of_question": 10})
    child = identified("fda" if child_kind == "multiple_choice" else "japanese_diet").model_copy(update={"id_of_question": 11})
    yes = child.model_copy(update={"id_of_question": 12})
    no = child.model_copy(update={"id_of_question": 13})
    return ConditionalQuestion(question_text="Local conditional", id_of_question=14, id_of_post=201,
                               parent=parent, child=child, question_yes=yes, question_no=no)


async def capture(report):
    client = CapturingClient(LocalMetaculus())
    with client.capture() as operations:
        await report.publish_report_to_metaculus(client)
    return operations


def api_post(kind, *, qid=101, post_id=201):
    data = {"id": qid, "type": kind, "title": "Synthetic API question", "status": "open",
            "my_forecasts": {"latest": None, "history": []}}
    if kind == "multiple_choice":
        data.update(options=["A", "B", "C"], group_variable=None)
    if kind in ("numeric", "date", "discrete"):
        lower, upper = ((1796083200, 1827619200) if kind == "date" else (0, 100))
        count = 4 if kind == "discrete" else 200
        data.update(open_lower_bound=False, open_upper_bound=False,
                    scaling={"range_min": lower, "range_max": upper, "zero_point": None,
                             "inbound_outcome_count": count,
                             "continuous_range": [lower + (upper - lower) * i / count for i in range(count + 1)]})
    return {"id": post_id, "question": data}


class ValidationCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.accelerate_parser_retries()


class QuestionFormatTests(ValidationCase):
    def test_locked_sdk_parses_all_existing_atomic_api_types(self):
        for kind in ("binary", "multiple_choice", "numeric", "date", "discrete"):
            with self.subTest(kind=kind):
                q = DataOrganizer.get_question_from_post_json(api_post(kind))
                self.assertEqual(q.question_type, kind)
                validate_question(q, publishing=True)
                if kind in ("numeric", "date", "discrete"):
                    cdf = numeric_payload(q, distribution(q))
                    self.assertEqual(len(cdf), 5 if kind == "discrete" else 201)

    def test_locked_sdk_parses_existing_conditional_api_format(self):
        post = api_post("conditional", qid=14)
        post["question"].update({key: api_post("binary", qid=index)["question"] for key, index in
                                (("condition", 10), ("condition_child", 11), ("question_yes", 12), ("question_no", 13))})
        q = DataOrganizer.get_question_from_post_json(post)
        validate_question(q, publishing=True)
        self.assertEqual([q.parent.id_of_question, q.child.id_of_question, q.question_yes.id_of_question, q.question_no.id_of_question], [10, 11, 12, 13])

    def test_invalid_or_unsupported_question_profiles_are_rejected(self):
        q = identified("percentage")
        bad = [MetaculusQuestion(question_text="Unsupported"),
               q.model_copy(update={"lower_bound": 101}), q.model_copy(update={"upper_bound": float("inf")}),
               q.model_copy(update={"zero_point": q.lower_bound}), q.model_copy(update={"cdf_size": 1}),
               q.model_copy(update={"cdf_size": True}), q.model_copy(update={"open_lower_bound": "false"}),
               identified("fda").model_copy(update={"options": ["A", "A"]}),
               identified("fda").model_copy(update={"options": []})]
        for value in bad:
            with self.subTest(value=type(value)), self.assertRaises(ForecastValidationError):
                validate_question(value)

    def test_object_cannot_override_source_options_scaling_or_ids(self):
        for kind, update in (("multiple_choice", {"options": ["A", "B", "Other"]}),
                             ("numeric", {"upper_bound": 101}), ("numeric", {"cdf_size": 202}),
                             ("binary", {"id_of_question": 999})):
            q = DataOrganizer.get_question_from_post_json(api_post(kind)).model_copy(update=update)
            with self.subTest(kind=kind), self.assertRaises(ForecastValidationError):
                validate_question(q, publishing=True)

    def test_known_api_numeric_string_scaling_is_preserved(self):
        post = api_post("numeric")
        post["question"]["scaling"].update(range_min="0", range_max="100")
        q = DataOrganizer.get_question_from_post_json(post)
        validate_question(q, publishing=True)
        numeric_payload(q, distribution(q))

    def test_missing_api_bound_flags_cannot_silently_default_to_open(self):
        post = api_post("numeric")
        del post["question"]["open_lower_bound"]
        q = DataOrganizer.get_question_from_post_json(post)
        with self.assertRaises(ForecastValidationError):
            validate_question(q)

    async def test_invalid_question_is_rejected_before_research_or_models(self):
        calls = self.model()
        q = identified("fda").model_copy(update={"options": ["A", "A"]})
        result = await self.bot().forecast_questions([q], return_exceptions=True)
        self.assertIsInstance(result[0], Exception)
        self.assertEqual(calls, [])


class RawForecastTests(ValidationCase):
    async def parse(self, case, data, q=None):
        self.model(data if isinstance(data, str) else json.dumps(data))
        method = {"japanese_diet": "_binary_prompt_to_forecast", "fda": "_multiple_choice_prompt_to_forecast",
                  "percentage": "_numeric_prompt_to_forecast", "date": "_date_prompt_to_forecast"}[case]
        return await getattr(self.bot(), method)(q or identified(case), "Local reasoning")

    async def test_binary_edges_preserve_existing_clamp(self):
        for value, expected in ((0, .01), (.001, .01), (.65, .65), (.999, .99), (1, .99)):
            with self.subTest(value=value):
                result = await self.parse("japanese_diet", {"prediction_in_decimal": value})
                self.assertEqual(result.prediction_value, expected)

    async def test_binary_invalid_or_ambiguous_values_are_not_clamped_into_validity(self):
        for value in (-.1, 1.1, True, "0.65", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await self.parse("japanese_diet", {"prediction_in_decimal": value})

    async def test_duplicate_json_fields_and_extra_fields_are_rejected(self):
        for data in ('{"prediction_in_decimal":0.2,"prediction_in_decimal":0.8}',
                     '{"prediction_in_decimal":0.5,"alternative":0.7}'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                await self.parse("japanese_diet", data)

    async def test_multiple_choice_labels_can_be_permuted_but_never_renamed(self):
        q = identified("fda")
        options = [{"option_name": name, "probability": p} for name, p in zip(q.options, (.3, .5, .2))]
        result = await self.parse("fda", {"predicted_options": list(reversed(options))})
        self.assertEqual(result.prediction_value.to_dict(), {item["option_name"]: item["probability"] for item in options})
        options[0]["option_name"] += " "
        with self.assertRaises(ValueError):
            await self.parse("fda", {"predicted_options": options})

    async def test_multiple_choice_probability_types_ranges_and_sum(self):
        for probabilities in ((True, 0, 0), ("0.3", .5, .2), (-.1, .7, .4), (1.1, 0, 0),
                              (.2, .2, .2), (.5, .5, .5), (float("nan"), .5, .5)):
            data = {"predicted_options": [{"option_name": name, "probability": p}
                                          for name, p in zip(identified("fda").options, probabilities)]}
            with self.subTest(probabilities=probabilities), self.assertRaises(ValueError):
                await self.parse("fda", data)

    async def test_existing_mc_rounding_normalization_and_explicit_zero_are_preserved(self):
        q = identified("fda")
        for probabilities in ((.3333, .3333, .3333), (.99, .01, 0)):
            result = await self.parse("fda", {"predicted_options": [
                {"option_name": name, "probability": p} for name, p in zip(q.options, probabilities)]})
            values = result.prediction_value.to_dict()
            self.assertEqual(set(values), set(q.options))
            self.assertAlmostEqual(sum(values.values()), 1)
            self.assertTrue(all(value > 0 for value in values.values()))  # Existing SDK clamp.

    async def test_numeric_percentiles_reject_nonfinite_unsorted_and_closed_bound_violations(self):
        valid = [{"percentile": p, "value": v} for p, v in zip((.1, .2, .4, .6, .8, .9), (10, 20, 40, 60, 80, 90))]
        for update in ({"value": -1}, {"value": float("inf")}, {"value": True}, {"value": "10"},
                       {"percentile": -.1}, {"percentile": .2}, {"percentile": True}):
            data = copy.deepcopy(valid)
            data[0].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                await self.parse("percentage", data)
        data = copy.deepcopy(valid)
        data[-1]["value"] = 101
        with self.assertRaises(ValueError):
            await self.parse("percentage", data)

    async def test_numeric_invalid_input_is_refused_before_sdk_clipping(self):
        with patch.object(NumericDistribution, "_standardize_cdf", side_effect=AssertionError("Do not repair invalid input")):
            with self.assertRaises(ValueError):
                await self.parse("percentage", [{"percentile": .1, "value": -5}, {"percentile": .9, "value": 90}])

    async def test_open_tails_and_repeated_values_retain_sdk_interpolation(self):
        for q, values in ((identified("percentage").model_copy(update={"open_lower_bound": True, "open_upper_bound": True}), (-10, 10, 40, 80, 110, 120)),
                          (identified("percentage"), (0, 0, 40, 60, 80, 90)),
                          (identified("percentage"), (1e-7, 1e-7, 40, 60, 80, 90))):
            data = [{"percentile": p, "value": v} for p, v in zip((.1, .2, .4, .6, .8, .9), values)]
            result = await self.parse("percentage", data, q)
            validate_prediction(q, result.prediction_value)

    async def test_nominal_bounds_are_not_enforced_as_hard_bounds(self):
        q = identified("percentage").model_copy(update={"nominal_lower_bound": 20, "nominal_upper_bound": 80})
        result = await self.parse("percentage", [{"percentile": p, "value": v} for p, v in zip((.1, .9), (10, 90))], q)
        validate_prediction(q, result.prediction_value)

    async def test_log_scale_domain_and_payload(self):
        q = identified("percentage").model_copy(update={"lower_bound": 1, "upper_bound": 1000, "zero_point": 0,
                                                       "open_lower_bound": True, "open_upper_bound": True})
        data = [{"percentile": p, "value": 1000 ** p} for p in (.1, .2, .4, .6, .8, .9)]
        result = await self.parse("percentage", data, q)
        validate_prediction(q, result.prediction_value)
        data[0]["value"] = 0
        with self.assertRaises(ValueError):
            await self.parse("percentage", data, q)

    async def test_date_only_means_utc_even_on_a_non_utc_host(self):
        dates = ("2026-12-01", "2027-01-01", "2027-03-01", "2027-05-01", "2027-07-01", "2027-09-01")
        data = [{"percentile": p, "value": date} for p, date in zip((.1, .2, .4, .6, .8, .9), dates)]
        try:
            with patch.dict(os.environ, {"TZ": "Pacific/Honolulu"}):
                time.tzset()
                result = await self.parse("date", data)
                self.assertEqual(result.prediction_value.declared_percentiles[0].value, 1796083200)
        finally:
            time.tzset()

    async def test_date_times_need_timezone_and_closed_bounds(self):
        q = identified("date")
        for value in ("2026-12-01T12:00:00", "not-a-date", 1796083200):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await self.parse("date", [{"percentile": .1, "value": value}, {"percentile": .9, "value": "2027-09-01"}], q)


class PayloadValidationTests(ValidationCase):
    def test_extreme_numeric_values_do_not_reach_cdf_generation(self):
        q = identified("percentage")
        with self.assertRaises(ForecastValidationError):
            raw_parser_prediction(q, list[Percentile], [{"percentile": .1, "value": 10 ** 1000}, {"percentile": .9, "value": 90}])
        with self.assertRaises(ForecastValidationError):
            validate_question(q.model_copy(update={"lower_bound": -1e308, "upper_bound": 1e308}))

    def test_cdf_sum_is_not_a_probability_vector_sum(self):
        cdf = [i / 200 for i in range(201)]
        self.assertGreater(sum(cdf), 1)
        validate_cdf(cdf, identified("percentage"))

    def test_cdf_rejects_invalid_length_range_monotonicity_density_and_tails(self):
        valid = [i / 200 for i in range(201)]
        for index, value in ((1, -.1), (50, .1), (50, float("nan")), (50, True), (0, .001), (200, .999), (50, .245)):
            values = list(valid)
            values[index] = value
            with self.subTest(index=index, value=value), self.assertRaises(ForecastValidationError):
                validate_cdf(values, identified("percentage"))
        with self.assertRaises(ForecastValidationError):
            validate_cdf(valid[:-1], identified("percentage"))
        with self.assertRaises(ForecastValidationError):
            validate_cdf([0] + [1] * 200, identified("percentage"))

    def test_distribution_metadata_must_match_its_question(self):
        q = identified("percentage")
        original = distribution(q)
        for update in ({"lower_bound": -1}, {"upper_bound": 101}, {"open_upper_bound": True},
                       {"cdf_size": 200}, {"is_date": True}, {"zero_point": -1}):
            with self.subTest(update=update), self.assertRaises(ForecastValidationError):
                validate_prediction(q, original.model_copy(update=update))

    def test_distribution_locations_must_match_the_api_grid(self):
        for lower, upper in ((0, 100), (1e-12, 2e-12)):
            post = api_post("numeric")
            post["question"]["scaling"].update(range_min=lower, range_max=upper,
                continuous_range=[lower + (upper - lower) * i / 200 for i in range(201)])
            q = DataOrganizer.get_question_from_post_json(post)
            q.api_json["question"]["scaling"]["continuous_range"][50] += (upper - lower) * .001
            with self.subTest(lower=lower), self.assertRaises(ForecastValidationError):
                validate_prediction(q, distribution(q))

    async def test_invalid_values_cannot_be_hidden_by_aggregation(self):
        with self.assertRaises(ForecastValidationError):
            await self.bot()._aggregate_predictions([.2, float("nan"), .8], identified("japanese_diet"))
        q = identified("fda")
        invalid = mc(q).model_copy(update={"predicted_options": [mc(q).predicted_options[0]] * 3})
        with self.assertRaises(ForecastValidationError):
            await self.bot()._aggregate_predictions([mc(q), invalid], q)

    def test_writer_defends_its_single_attempt_boundary_before_http(self):
        client = app.MetaculusClient()
        writer = SingleAttemptWriter(client)
        with patch("publication.requests.post") as post:
            for payload in ({"probability_yes": float("nan")}, {"probability_yes": 0},
                            {"probability_yes_per_category": {"A": .4, "B": .4}},
                            {"probability_yes": .5, "continuous_cdf": [0, 1]}):
                with self.subTest(payload=payload), self.assertRaises(ForecastValidationError):
                    writer({"kind": "forecast", "question_id": 101, "payload": payload})
            post.assert_not_called()


class PlanValidationTests(ValidationCase):
    async def binary_plan(self):
        q = identified("japanese_diet")
        report = BinaryReport(question=q, prediction=.65, explanation="# Frozen explanation")
        return q, report, await capture(report)

    async def test_whole_plan_rejects_wrong_ids_extra_steps_and_report_mismatch(self):
        q, report, operations = await self.binary_plan()
        for change in ("question", "post", "payload", "extra", "comment"):
            bad = copy.deepcopy(operations)
            if change == "question": bad[0]["question_id"] += 1
            elif change == "post": bad[1]["post_id"] += 1
            elif change == "payload": bad[0]["payload"]["probability_yes"] = .7
            elif change == "comment": bad[1]["text"] = "Changed"
            else: bad.append(copy.deepcopy(bad[0]))
            with self.subTest(change=change), self.assertRaises(ForecastValidationError):
                validate_plan(q, report, bad)

    async def test_pending_plan_is_blocked_without_claim_if_question_constraints_changed(self):
        q, report, operations = await self.binary_plan()
        key = "resource/initial"
        self.publication_store.prepare(key, report.model_dump_json(exclude_computed_fields=True), operations)
        bot = self.bot(publish_reports_to_metaculus=True)
        publisher = bot._publication_runtime()
        changed = q.model_copy(update={"resolution_criteria": "Changed resolution criteria"})
        with self.assertRaises(ForecastValidationError):
            await bot._drain_publication(publisher, key, changed)
        self.assertEqual([s["state"] for s in self.publication_store.steps(key)], ["ready", "ready"])
        self.assertFalse(bot.metaculus_client.forecasts)

    async def test_saved_numeric_bounds_and_mc_options_cannot_be_reinterpreted(self):
        for kind in ("numeric", "multiple_choice"):
            q = DataOrganizer.get_question_from_post_json(api_post(kind))
            prediction = distribution(q) if kind == "numeric" else mc(q)
            report = DataOrganizer.get_report_type_for_question_type(type(q))(
                question=q, prediction=prediction, explanation="# Frozen")
            operations = await capture(report)
            changed = q.model_copy(update={"api_json": {}, **({"upper_bound": 101} if kind == "numeric" else {"options": ["A", "B", "Other"]})})
            with self.subTest(kind=kind), self.assertRaises(ForecastValidationError):
                validate_plan(changed, report, operations)

    async def test_unknown_operation_stays_quarantined_before_validation(self):
        q, report, operations = await self.binary_plan()
        key = "resource/initial"
        self.publication_store.prepare(key, report.model_dump_json(exclude_computed_fields=True), operations)
        self.publication_store.claim(key, 0)
        bot = self.bot(publish_reports_to_metaculus=True)
        with self.assertRaises(PublicationUncertain):
            await bot._drain_publication(bot._publication_runtime(), key, q.model_copy(update={"question_text": "Changed"}))
        self.assertEqual(self.publication_store.steps(key)[0]["state"], "sending")

    async def test_invalid_mc_never_stages_or_sends(self):
        self.model(json.dumps({"predicted_options": [{"option_name": "A", "probability": .5}] * 2}))
        bot = self.bot(publish_reports_to_metaculus=True)
        q = identified("fda")
        result = await bot.forecast_questions([q], return_exceptions=True)
        self.assertIsInstance(result[0], Exception)
        self.assertFalse(bot.metaculus_client.forecasts)
        self.assertFalse(bot.metaculus_client.comments)
        self.assertIsNone(self.publication_store.pending(bot._publication_resource(q)))

    async def test_valid_mc_freezes_payload_and_restores_without_model_calls(self):
        q = identified("fda")
        calls = self.model(json.dumps({"predicted_options": [
            {"option_name": name, "probability": p} for name, p in zip(q.options, (.3, .5, .2))]}))
        sink = LocalMetaculus()
        bot = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        report = await bot.forecast_question(q)
        count = len(calls)
        self.assertEqual(count, 18)
        restored = await self.bot(publish_reports_to_metaculus=True, metaculus_client=sink).forecast_question(q)
        self.assertEqual(restored.model_dump(), report.model_dump())
        self.assertEqual(len(calls), count)
        self.assertEqual(sink.forecasts, [(q.id_of_question, report.prediction.to_dict())])
        self.assertEqual(len(sink.comments), 1)

    async def test_conditional_types_and_wire_plan_survive_frozen_round_trip(self):
        q = conditional("multiple_choice")
        report = ConditionalReport(question=q, prediction=ConditionalPrediction(
            parent=.5, child=mc(q.child), prediction_yes=mc(q.question_yes), prediction_no=mc(q.question_no)), explanation="# Conditional")
        operations = await capture(report)
        restored = restore_report(ConditionalReport, report.model_dump_json(exclude_computed_fields=True, serialize_as_any=True))
        self.assertEqual(type(restored.question.question_yes), type(q.question_yes))
        validate_plan(q, restored, operations)
        self.assertEqual([op["question_id"] for op in operations if op["kind"] == "forecast"], [12, 13])

    async def test_full_conditional_pipeline_and_restore_do_not_republish(self):
        q = conditional()
        calls = self.model()
        sink = LocalMetaculus()
        first = await self.bot(publish_reports_to_metaculus=True, metaculus_client=sink).forecast_question(q)
        count = len(calls)
        self.assertEqual(count, 72)  # Six conditional predictions, four binary parts each.
        restored = await self.bot(publish_reports_to_metaculus=True, metaculus_client=sink).forecast_question(q)
        self.assertEqual(restored.model_dump(), first.model_dump())
        self.assertEqual(len(calls), count)
        self.assertEqual([qid for qid, _ in sink.forecasts], [12, 13])
        self.assertEqual(len(sink.comments), 2)

    async def test_invalid_conditional_no_branch_prevents_yes_publication_too(self):
        q = conditional()
        report = ConditionalReport(question=q, prediction=ConditionalPrediction(
            parent=.5, child=.5, prediction_yes=.7, prediction_no=1.2), explanation="# Conditional")
        operations = []
        for qid, value in ((12, .7), (13, 1.2)):
            operations.extend([{"kind": "forecast", "question_id": qid, "payload": {"probability_yes": value}},
                               {"kind": "comment", "post_id": 201, "text": report.explanation,
                                "is_private": True, "included_forecast": True}])
        with self.assertRaises(ForecastValidationError):
            validate_plan(q, report, operations)
        key = "conditional/initial"
        self.publication_store.prepare(key, report.model_dump_json(exclude_computed_fields=True, serialize_as_any=True), operations)
        bot = self.bot(publish_reports_to_metaculus=True)
        with self.assertRaises(ForecastValidationError):
            await bot._drain_publication(bot._publication_runtime(), key, q)
        self.assertEqual([step["state"] for step in self.publication_store.steps(key)], ["ready"] * 4)
        self.assertFalse(bot.metaculus_client.forecasts)
        self.assertFalse(bot.metaculus_client.comments)

    def test_incomplete_legacy_conditional_metadata_is_not_guessed(self):
        q = conditional()
        report = ConditionalReport(question=q, prediction=ConditionalPrediction(
            parent=.5, child=.5, prediction_yes=.7, prediction_no=.3), explanation="# Conditional")
        legacy = report.model_dump_json(exclude_computed_fields=True)
        with self.assertRaises(ForecastValidationError):
            restore_report(ConditionalReport, legacy)

    async def test_legacy_conditional_can_recover_types_from_its_complete_api_snapshot(self):
        post = api_post("conditional", qid=14)
        post["question"].update({key: api_post("multiple_choice" if key != "condition" else "binary", qid=index)["question"]
                                for key, index in (("condition", 10), ("condition_child", 11), ("question_yes", 12), ("question_no", 13))})
        q = DataOrganizer.get_question_from_post_json(post)
        report = ConditionalReport(question=q, prediction=ConditionalPrediction(
            parent=.5, child=mc(q.child), prediction_yes=mc(q.question_yes), prediction_no=mc(q.question_no)), explanation="# Frozen")
        operations = await capture(report)
        legacy = report.model_dump_json(exclude_computed_fields=True)
        restored = restore_report(ConditionalReport, legacy)
        validate_plan(q, restored, operations)


class FullNumericPipelineTests(ValidationCase):
    async def pipeline(self, kind):
        # The initial shape-only SDK fixtures intentionally omit scheduling.
        # A real publishing integration needs explicit open/close metadata;
        # supply it here rather than disabling the pre-POST deadline guard.
        post = api_post(kind)
        post["question"]["open_time"] = "2026-11-01T00:00:00Z"
        post["question"]["scheduled_close_time"] = "2026-11-10T00:00:00Z"
        q = DataOrganizer.get_question_from_post_json(post)
        values = [{"percentile": p, "value": q.lower_bound + (q.upper_bound - q.lower_bound) * p}
                  for p in (.1, .2, .4, .6, .8, .9)] if kind != "date" else [
                      {"percentile": p, "value": date} for p, date in zip((.1, .2, .4, .6, .8, .9),
                        ("2026-12-10", "2027-01-10", "2027-03-10", "2027-05-10", "2027-07-10", "2027-09-10"))]
        calls = self.model(json.dumps(values))
        sink = LocalMetaculus()
        bot = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        report = await bot.forecast_question(q)
        before = len(calls)
        self.assertEqual(before, 18)
        restored = await self.bot(publish_reports_to_metaculus=True, metaculus_client=sink).forecast_question(q)
        self.assertEqual(restored.model_dump(), report.model_dump())
        self.assertEqual(len(calls), before)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))
        validate_cdf(sink.forecasts[0][1], q)
        self.assertEqual(len(sink.forecasts[0][1]), q.cdf_size)

    async def test_numeric_pipeline_and_idempotent_restore(self):
        await self.pipeline("numeric")

    async def test_discrete_pipeline_keeps_api_outcome_count(self):
        await self.pipeline("discrete")

    async def test_date_pipeline_and_idempotent_restore(self):
        await self.pipeline("date")

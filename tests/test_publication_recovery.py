import asyncio
import json
import sqlite3
from unittest.mock import patch

import requests

from .support import LocalMetaculus, OfflineTestCase, app, question
from publication import (
    CapturingClient, PublicationRejected, PublicationStore, PublicationUncertain,
    Publisher, SingleAttemptWriter, restore_report,
)


class RecoveryTests(OfflineTestCase):
    async def test_rejected_comment_resumes_without_reforecasting_even_if_api_says_forecasted(self):
        class RejectOnce(LocalMetaculus):
            attempts = 0

            def post_question_comment(self, post_id, text, **kwargs):
                self.attempts += 1
                if self.attempts == 1:
                    raise PublicationRejected(403)
                return super().post_question_comment(post_id, text, **kwargs)

        calls = self.model()
        sink = RejectOnce()
        q = question("japanese_diet")
        first = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        failed = await first.forecast_questions([q], return_exceptions=True)
        self.assertIsInstance(failed[0], Exception)
        key = first._publication_key(q)
        self.assertEqual([s["state"] for s in self.publication_store.steps(key)], ["confirmed", "rejected"])
        frozen_comment = json.loads(self.publication_store.steps(key)[1]["operation"])["text"]
        count = len(calls)
        second = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        second.publication_store = PublicationStore(self.publication_store.path)  # New process/store lifetime.
        reports = await second.forecast_questions([q.model_copy(update={"already_forecasted": True})])
        self.assertEqual(len(reports), 1)
        self.assertEqual(len(calls), count)
        self.assertEqual(sink.forecasts, [(q.id_of_question, .65)])
        self.assertEqual(sink.comments, [(q.id_of_post, frozen_comment)])
        self.assertEqual([s["state"] for s in self.publication_store.steps(key)], ["confirmed", "confirmed"])
        self.assertEqual(await second.forecast_questions([q.model_copy(update={"already_forecasted": True})]), [])

    async def test_accepted_forecast_with_lost_ack_is_never_resent_after_restart(self):
        class LostAck(LocalMetaculus):
            def post_binary_question_prediction(self, question_id, prediction):
                super().post_binary_question_prediction(question_id, prediction)
                raise requests.Timeout("Synthetic lost acknowledgement")

        calls = self.model()
        sink = LostAck()
        q = question("japanese_diet")
        first = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        self.assertIsInstance((await first.forecast_questions([q], return_exceptions=True))[0], Exception)
        count = len(calls)
        second = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        second.publication_store = PublicationStore(self.publication_store.path)
        self.assertIsInstance((await second.forecast_questions([q], return_exceptions=True))[0], Exception)
        self.assertEqual(len(sink.forecasts), 1)
        self.assertEqual(sink.comments, [])
        self.assertEqual(len(calls), count)

    async def test_accepted_comment_with_lost_ack_is_quarantined_not_duplicated(self):
        class LostAck(LocalMetaculus):
            def post_question_comment(self, post_id, text, **kwargs):
                super().post_question_comment(post_id, text, **kwargs)
                raise requests.ConnectionError("Synthetic acknowledgement lost")

        self.model()
        sink = LostAck()
        q = question("japanese_diet")
        first = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        self.assertIsInstance((await first.forecast_questions([q], return_exceptions=True))[0], Exception)
        second = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        self.assertIsInstance((await second.forecast_questions([q.model_copy(update={"already_forecasted": True})], return_exceptions=True))[0], Exception)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))

    async def test_completed_stale_snapshot_returns_original_report_without_model_calls(self):
        calls = self.model()
        sink = LocalMetaculus()
        q = question("japanese_diet")
        first = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        report = await first.forecast_question(q)
        count = len(calls)
        second = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        restored = await second.forecast_question(q)
        self.assertEqual(restored.model_dump(), report.model_dump())
        self.assertEqual(len(calls), count)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))

    async def test_explicit_reforecast_is_idempotent_per_intent_and_allows_new_intent(self):
        self.model()
        sink = LocalMetaculus()
        for intent in ("run-A", "run-A", "run-B"):
            bot = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink,
                           skip_previously_forecasted_questions=False)
            bot.publication_intent = intent
            await bot.forecast_question(question("japanese_diet"))
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (2, 2))

    async def test_unknown_old_intent_blocks_new_intent(self):
        self.model()
        sink = LocalMetaculus(fail_comment=True)
        for intent in ("run-A", "run-B"):
            bot = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink,
                           skip_previously_forecasted_questions=False)
            bot.publication_intent = intent
            self.assertIsInstance((await bot.forecast_questions([question("japanese_diet")], return_exceptions=True))[0], Exception)
        self.assertEqual(len(sink.forecasts), 1)

    async def test_separate_account_scopes_do_not_collide(self):
        self.model()
        sink = LocalMetaculus()
        for scope in ("account-A", "account-B"):
            bot = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
            bot.publication_scope = scope
            await bot.forecast_question(question("japanese_diet"))
        self.assertEqual(len(sink.forecasts), 2)

    async def test_dry_run_never_initializes_publication_or_writes_journal(self):
        self.model()
        bot = self.bot()
        with patch.object(self.publication_store, "prepare", side_effect=AssertionError("Dry run must not stage a publication")):
            await bot.forecast_question(question("japanese_diet"))
        self.assertFalse(hasattr(bot, "_publication_publisher"))


class JournalTests(OfflineTestCase):
    def plan(self):
        capture = CapturingClient(LocalMetaculus())
        with capture.capture() as operations:
            capture.post_binary_question_prediction(1, .65)
            capture.post_question_comment(2, "Original frozen explanation")
        self.publication_store.prepare("resource/initial", '{"original":true}', operations)
        return operations

    def test_first_report_and_payload_win_competing_prepare(self):
        original = self.plan()
        report, created = self.publication_store.prepare("resource/initial", '{"different":true}', [{"kind": "comment", "text": "Wrong"}])
        self.assertFalse(created)
        self.assertEqual(report, '{"original":true}')
        self.assertEqual([json.loads(s["operation"]) for s in self.publication_store.steps("resource/initial")], original)

    def test_claim_survives_restart_and_never_expires_into_a_retry(self):
        self.plan()
        self.assertTrue(self.publication_store.claim("resource/initial", 0))
        reopened = PublicationStore(self.publication_store.path)
        writes = []
        with self.assertRaises(PublicationUncertain):
            Publisher(reopened, writes.append).drain("resource/initial")
        self.assertEqual(writes, [])

    def test_acknowledged_write_with_failed_checkpoint_is_not_resent(self):
        self.plan()
        writes = []
        publisher = Publisher(self.publication_store, writes.append)
        with patch.object(self.publication_store, "finish", side_effect=sqlite3.OperationalError("Synthetic full disk")):
            with self.assertRaises(sqlite3.OperationalError):
                publisher.drain("resource/initial")
        with self.assertRaises(PublicationUncertain):
            Publisher(PublicationStore(self.publication_store.path), writes.append).drain("resource/initial")
        self.assertEqual(len(writes), 1)

    def test_reconciliation_requires_exact_step_and_does_not_replay_accepted_write(self):
        self.plan()
        self.publication_store.claim("resource/initial", 0)
        with self.assertRaises(ValueError):
            self.publication_store.reconcile("resource/initial", 0, "wrong-hash", accepted=True)
        step = self.publication_store.steps("resource/initial")[0]
        self.publication_store.reconcile("resource/initial", 0, step["operation_hash"], accepted=True)
        writes = []
        Publisher(self.publication_store, writes.append).drain("resource/initial")
        self.assertEqual([write["kind"] for write in writes], ["comment"])

    def test_verified_not_applied_can_be_explicitly_released(self):
        self.plan()
        self.publication_store.claim("resource/initial", 0)
        step = self.publication_store.steps("resource/initial")[0]
        self.publication_store.reconcile("resource/initial", 0, step["operation_hash"], accepted=False)
        writes = []
        Publisher(self.publication_store, writes.append).drain("resource/initial")
        self.assertEqual([write["kind"] for write in writes], ["forecast", "comment"])

    def test_corrupt_journal_fails_before_any_write(self):
        self.publication_store.path.write_bytes(b"not a sqlite database")
        with self.assertRaises(sqlite3.DatabaseError):
            PublicationStore(self.publication_store.path)

    def test_capture_without_coordinator_cannot_send(self):
        with self.assertRaises(RuntimeError):
            CapturingClient(LocalMetaculus()).post_question_comment(1, "Unsafe direct call")

    def test_untrusted_custom_transport_is_rejected_before_any_send(self):
        with self.assertRaises(TypeError):
            SingleAttemptWriter(object())

    async def test_concurrent_capture_keeps_each_forecast_and_comment_together(self):
        capture = CapturingClient(LocalMetaculus())

        async def collect(identifier):
            with capture.capture() as operations:
                capture.post_binary_question_prediction(identifier, .65)
                await asyncio.sleep(0)
                capture.post_question_comment(identifier, str(identifier))
                return operations

        plans = await asyncio.gather(collect(1), collect(2))
        for identifier, plan in enumerate(plans, 1):
            self.assertEqual(plan[0]["question_id"], identifier)
            self.assertEqual(plan[1]["post_id"], identifier)
            self.assertEqual(plan[1]["text"], str(identifier))


class PayloadCompatibilityTests(OfflineTestCase):
    def test_reloaded_mc_report_does_not_renormalize_the_frozen_prediction(self):
        from forecasting_tools import MultipleChoiceReport, PredictedOptionList
        q = question("fda").model_copy(update={"id_of_question": 10, "id_of_post": 20})
        prediction = PredictedOptionList.model_validate({"predicted_options": [
            {"option_name": name, "probability": probability} for name, probability in zip(q.options, (.99, .01, 0))]})
        report = MultipleChoiceReport(question=q, prediction=prediction, explanation="# Frozen report")
        restored = restore_report(type(report), report.model_dump_json(exclude_computed_fields=True))
        self.assertEqual(restored.model_dump(), report.model_dump())

    async def test_all_report_shapes_keep_sdk_payload_and_step_order(self):
        from forecasting_tools import (
            BinaryQuestion, ConditionalPrediction, ConditionalQuestion,
            MultipleChoiceReport, NumericDistribution, NumericReport, Percentile,
            PredictedOptionList,
        )
        from forecasting_tools.data_models.conditional_report import ConditionalReport
        from forecasting_tools.data_models.numeric_report import DateReport

        sink = LocalMetaculus()
        capture = CapturingClient(sink)
        mc = question("fda").model_copy(update={"id_of_question": 10, "id_of_post": 20})
        mc_prediction = PredictedOptionList.model_validate({"predicted_options": [
            {"option_name": name, "probability": probability} for name, probability in zip(mc.options, (.3, .5, .2))]})
        numeric = question("percentage").model_copy(update={"id_of_question": 11, "id_of_post": 21})
        distribution = NumericDistribution.from_question([
            Percentile(percentile=p, value=v) for p, v in zip((.1, .2, .4, .6, .8, .9), (10, 20, 40, 60, 80, 90))], numeric)
        date = question("date").model_copy(update={"id_of_question": 12, "id_of_post": 22})
        date_distribution = NumericDistribution.from_question([
            Percentile(percentile=p, value=date.lower_bound.timestamp() + (date.upper_bound.timestamp() - date.lower_bound.timestamp()) * p)
            for p in (.1, .2, .4, .6, .8, .9)], date)
        parts = [BinaryQuestion(question_text=f"Part {i}", id_of_question=30+i, id_of_post=40) for i in range(4)]
        conditional = ConditionalQuestion(question_text="Conditional", id_of_question=30, id_of_post=40,
                                          parent=parts[0], child=parts[1], question_yes=parts[2], question_no=parts[3])
        reports = [
            MultipleChoiceReport(question=mc, prediction=mc_prediction, explanation="# Exact MC explanation"),
            NumericReport(question=numeric, prediction=distribution, explanation="# Exact numeric explanation"),
            DateReport(question=date, prediction=date_distribution, explanation="# Exact date explanation"),
            ConditionalReport(question=conditional, prediction=ConditionalPrediction(parent=.5, child=.5, prediction_yes=.7, prediction_no=.3), explanation="# Exact conditional explanation"),
        ]
        for report in reports:
            with self.subTest(kind=report.question.question_type):
                with capture.capture() as plan:
                    await report.publish_report_to_metaculus(capture)
                restored = type(report).model_validate_json(report.model_dump_json(exclude_computed_fields=True))
                self.assertEqual(restored.question.id_of_post, report.question.id_of_post)
                if report is reports[0]:
                    self.assertEqual(plan[0]["payload"], {"probability_yes_per_category": mc_prediction.to_dict()})
                elif report in reports[1:3]:
                    self.assertEqual(plan[0]["payload"], {"continuous_cdf": [p.percentile for p in report.prediction.get_cdf()]})
                else:
                    self.assertEqual([op["kind"] for op in plan], ["forecast", "comment", "forecast", "comment"])
                    self.assertEqual([op["question_id"] for op in plan if op["kind"] == "forecast"], [32, 33])
                    self.assertEqual([op["payload"]["probability_yes"] for op in plan if op["kind"] == "forecast"], [.7, .3])
                for operation in plan:
                    if operation["kind"] == "comment":
                        self.assertEqual(operation["text"], report.explanation)
                        self.assertTrue(operation["is_private"])
                        self.assertTrue(operation["included_forecast"])


class HTTPWriteTests(OfflineTestCase):
    def send(self, status):
        response = requests.Response()
        response.status_code = status
        response._content = b"{}"
        response._content_consumed = True
        client = app.MetaculusClient()
        self.start_patch(patch.object(client, "_get_auth_headers", return_value={"headers": {}}))
        self.start_patch(patch.object(client, "_sleep_between_requests"))
        post = self.start_patch(patch("publication.requests.post", return_value=response))
        writer = SingleAttemptWriter(client)
        operation = {"kind": "forecast", "question_id": 1, "payload": {"probability_yes": .65}}
        return writer, operation, post

    def test_forecast_payload_timeout_and_no_redirect_match_single_attempt_contract(self):
        writer, operation, post = self.send(201)
        writer(operation)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"], [{"question": 1, "source": "api", "probability_yes": .65}])
        self.assertEqual(post.call_args.kwargs["timeout"], 30)
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertNotIn("Idempotency-Key", post.call_args.kwargs["headers"])

    def test_explicit_403_and_429_are_rejected_without_retry(self):
        for status in (403, 429):
            with self.subTest(status=status):
                writer, operation, post = self.send(status)
                with self.assertRaises(PublicationRejected):
                    writer(operation)
                self.assertEqual(post.call_count, 1)

    def test_5xx_redirect_and_202_are_not_treated_as_confirmed_commits(self):
        for status in (500, 503, 307, 202):
            with self.subTest(status=status):
                writer, operation, post = self.send(status)
                with self.assertRaises(PublicationUncertain):
                    writer(operation)
                self.assertEqual(post.call_count, 1)

    def test_comment_privacy_and_forecast_association_are_preserved(self):
        writer, _, post = self.send(201)
        writer({"kind": "comment", "post_id": 2, "text": "Exact report", "is_private": True, "included_forecast": True})
        self.assertEqual(post.call_args.kwargs["json"], {
            "on_post": 2, "text": "Exact report", "is_private": True, "included_forecast": True})

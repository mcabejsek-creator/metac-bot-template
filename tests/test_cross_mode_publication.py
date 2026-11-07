"""Durable recovery across modes, intents and independent simulated runners."""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from .support import LocalMetaculus, OfflineTestCase, question
from .test_github_publication import GitHubTestCase, PATH
from publication_journal import PublicationRejected, PublicationStore, PublicationUncertain, Publisher
from github_publication import GitHubStateError


class RejectOnce(LocalMetaculus):
    attempts = 0

    def post_question_comment(self, post_id, text, **kwargs):
        self.attempts += 1
        if self.attempts == 1:
            raise PublicationRejected(403)
        return super().post_question_comment(post_id, text, **kwargs)


class CrossModePipelineTests(GitHubTestCase):
    def worker(self, mode, intent, sink):
        bot = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink,
                       skip_previously_forecasted_questions=(mode == "tournament"))
        bot.publication_store = self.runner()
        bot.publication_intent = intent
        return bot

    async def rejected_plan(self, mode="cup", *, q=None):
        q = question("japanese_diet") if q is None else q
        sink = RejectOnce()
        first = self.worker(mode, "old-run", sink)
        result = await first.forecast_questions([q], return_exceptions=True)
        self.assertIsInstance(result[0], Exception)
        key = first._publication_key(q)
        self.assertEqual([step["state"] for step in first._publication_store.steps(key)], ["confirmed", "rejected"])
        return q, sink, first, key

    async def test_all_six_mode_transitions_and_same_workflow_reruns_only_recover_comment(self):
        calls = self.model()
        transitions = [(a, b) for a in ("cup", "test_questions", "tournament")
                       for b in ("cup", "test_questions", "tournament") if a != b]
        for index, (old_mode, new_mode) in enumerate(transitions):
            with self.subTest(old=old_mode, new=new_mode):
                q = question("japanese_diet").model_copy(update={"id_of_question": 1000 + index, "id_of_post": 2000 + index})
                q, sink, old, key = await self.rejected_plan(old_mode, q=q)
                before = len(calls)
                frozen = old._publication_store.report(key)
                current = self.worker(new_mode, "recovery-run", sink)
                await current.forecast_questions([q.model_copy(update={"already_forecasted": True})])
                self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))
                self.assertEqual(current._publication_store.report(current._publication_key(q)), frozen)
                for history in (False, True):
                    rerun = self.worker(new_mode, "recovery-run", sink)
                    await rerun.forecast_questions([q.model_copy(update={"already_forecasted": history})])
                self.assertEqual(len(calls), before)
                self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))
                self.assertEqual([s["state"] for s in self.runner().steps(key)], ["confirmed", "confirmed"])

    async def test_stale_history_does_not_reforecast_already_completed_cup_in_tournament(self):
        calls = self.model()
        q, sink = question("japanese_diet"), LocalMetaculus()
        first = self.worker("cup", "cup-run", sink)
        original = await first.forecast_question(q)
        before = len(calls)
        for _ in range(2):
            current = self.worker("tournament", "new-tournament-run", sink)
            restored = await current.forecast_question(q.model_copy(update={"already_forecasted": False}))
            self.assertEqual(restored.model_dump(), original.model_dump())
        self.assertEqual(len(calls), before)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))

    async def test_existing_completed_initial_intent_still_repairs_later_cup_comment(self):
        calls = self.model()
        q, sink = question("japanese_diet"), LocalMetaculus()
        initial = self.worker("tournament", "first-initial", sink)
        initial_report = await initial.forecast_question(q)
        initial_key = initial._publication_key(q)
        cup = self.worker("cup", "later-cup", sink)
        with patch.object(sink, "post_question_comment", side_effect=PublicationRejected(429)):
            self.assertIsInstance((await cup.forecast_questions([q], return_exceptions=True))[0], Exception)
        before = len(calls)
        latest = self.worker("tournament", "restore-cup", sink)
        await latest.forecast_questions([q.model_copy(update={"already_forecasted": True})])
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (2, 2))
        self.assertEqual(len(calls), before)
        self.assertEqual(json.loads(self.runner().report(initial_key))["prediction"], initial_report.prediction)

    async def test_new_explicit_cup_run_after_recovery_can_reforecast_but_old_run_cannot(self):
        calls = self.model()
        q, sink, old, key = await self.rejected_plan("tournament")
        recovered = self.worker("cup", "recover-only", sink)
        await recovered.forecast_question(q)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))
        await self.worker("cup", "intentional-new-run", sink).forecast_question(q)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (2, 2))
        before = len(calls)
        await self.worker("cup", "recover-only", sink).forecast_question(q)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (2, 2))
        self.assertEqual(len(calls), before)
        self.assertEqual(self.runner().report(recovered._publication_key(q)), self.runner().report(key))

    async def test_lost_binding_ack_before_or_after_commit_never_starts_model_or_post(self):
        calls = self.model()
        for index, lost in enumerate(("before", "after")):
            with self.subTest(lost=lost):
                q = question("japanese_diet").model_copy(update={"id_of_question": 3100 + index, "id_of_post": 4100 + index})
                q, sink, old, key = await self.rejected_plan("tournament", q=q)
                before = len(calls)
                self.server.lose_ack = lost
                recovery = self.worker("cup", "binding-run", sink)
                self.assertIsInstance((await recovery.forecast_questions([q], return_exceptions=True))[0], Exception)
                self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 0))
                await self.worker("cup", "binding-run", sink).forecast_question(q)
                self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))
                self.assertEqual(len(calls), before)

    async def test_actions_rerun_without_saved_binding_cannot_reforecast_after_other_runner_recovers(self):
        calls = self.model()
        q, sink, old, key = await self.rejected_plan("tournament")
        before = len(calls)
        self.server.lose_ack = "before"
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ATTEMPT": "1"}):
            failed = await self.worker("cup", "unrecorded-recovery", sink).forecast_questions([q], return_exceptions=True)
        self.assertIsInstance(failed[0], Exception)
        await self.worker("tournament", "other-recovery", sink).forecast_question(q)
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ATTEMPT": "2"}):
            failed = await self.worker("cup", "unrecorded-recovery", sink).forecast_questions([q], return_exceptions=True)
        self.assertIsInstance(failed[0], Exception)
        self.assertIn("no durable intent binding", str(failed[0]))
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))
        self.assertEqual(len(calls), before)

    async def test_actions_rerun_with_durable_recovery_binding_returns_saved_report(self):
        calls = self.model()
        q, sink, old, key = await self.rejected_plan("tournament")
        before = len(calls)
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ATTEMPT": "1"}):
            await self.worker("cup", "recorded-recovery", sink).forecast_question(q)
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ATTEMPT": "2"}):
            await self.worker("cup", "recorded-recovery", sink).forecast_question(q)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))
        self.assertEqual(len(calls), before)

    async def test_actions_force_rerun_without_any_record_blocks_before_models(self):
        calls = self.model()
        q, sink = question("japanese_diet"), LocalMetaculus()
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ATTEMPT": "2"}):
            failed = await self.worker("test_questions", "failed-before-save", sink).forecast_questions([q], return_exceptions=True)
        self.assertIsInstance(failed[0], Exception)
        self.assertEqual(calls, [])
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (0, 0))

    async def test_actions_force_run_rejects_invalid_attempt_metadata(self):
        calls = self.model()
        for attempt in ("", "zero", "0"):
            with self.subTest(attempt=attempt), patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ATTEMPT": attempt}):
                result = await self.worker("cup", "invalid-attempt", LocalMetaculus()).forecast_questions(
                    [question("japanese_diet")], return_exceptions=True)
                self.assertIsInstance(result[0], Exception)
        self.assertEqual(calls, [])

    async def test_cancel_after_durable_binding_before_drain_resumes_only_old_comment(self):
        calls = self.model()
        q, sink, old, key = await self.rejected_plan("tournament")
        before = len(calls)
        recovery = self.worker("cup", "cancelled-run", sink)
        select = recovery.publication_store.select_for_run

        def cancel_after_binding(*args):
            select(*args)
            raise asyncio.CancelledError()

        with patch.object(recovery.publication_store, "select_for_run", cancel_after_binding):
            with self.assertRaises(asyncio.CancelledError):
                await recovery.forecast_question(q)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 0))
        await self.worker("cup", "cancelled-run", sink).forecast_question(q)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))
        self.assertEqual(len(calls), before)

    async def test_cancel_after_last_comment_confirmation_still_remembers_recovery_intent(self):
        calls = self.model()
        q, sink, old, key = await self.rejected_plan("tournament")
        before = len(calls)
        recovery = self.worker("cup", "finished-but-cancelled", sink)
        finish = recovery.publication_store.finish

        def cancel_after_finish(key, position, state):
            finish(key, position, state)
            if position == 1 and state == "confirmed":
                raise asyncio.CancelledError()

        with patch.object(recovery.publication_store, "finish", cancel_after_finish):
            with self.assertRaises(asyncio.CancelledError):
                await recovery.forecast_question(q)
        self.assertEqual([s["state"] for s in self.runner().steps(key)], ["confirmed", "confirmed"])
        await self.worker("cup", "finished-but-cancelled", sink).forecast_question(q)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))
        self.assertEqual(len(calls), before)

    async def test_ambiguous_comment_stays_blocked_across_every_mode_and_rerun(self):
        calls = self.model()
        q, sink, old, key = await self.rejected_plan("cup")
        store = self.runner()
        store.claim(key, 1)
        store.finish(key, 1, "uncertain")
        before_steps, before_calls = store.steps(key), len(calls)
        for mode in ("tournament", "cup", "test_questions"):
            for attempt in range(2):
                worker = self.worker(mode, "unknown-" + mode, sink)
                result = await worker.forecast_questions([q.model_copy(update={"already_forecasted": bool(attempt)})], return_exceptions=True)
                self.assertIsInstance(result[0], Exception)
        self.assertEqual(self.runner().steps(key), before_steps)
        self.assertEqual(len(calls), before_calls)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 0))

    async def test_interrupted_sending_comment_is_not_released_by_mode_switch(self):
        calls = self.model()
        q, sink, old, key = await self.rejected_plan("tournament")
        self.runner().claim(key, 1)  # Abrupt runner exit: no finish/receipt.
        before = len(calls)
        for mode in ("cup", "test_questions", "tournament"):
            result = await self.worker(mode, "interrupted-" + mode, sink).forecast_questions([q], return_exceptions=True)
            self.assertIsInstance(result[0], Exception)
        self.assertEqual([s["state"] for s in self.runner().steps(key)], ["confirmed", "sending"])
        self.assertEqual(len(calls), before)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 0))

    async def test_recovery_of_ready_forecast_uses_only_original_frozen_payload(self):
        from forecasting_tools import BinaryReport
        from .test_forecast_validation import capture

        calls = self.model()
        q, sink = question("japanese_diet"), LocalMetaculus()
        old = self.worker("tournament", "old-ready", sink)
        old._publication_runtime()
        report = BinaryReport(question=q, prediction=.41, explanation="# Original frozen plan")
        old.publication_store.prepare(old._publication_key(q), report.model_dump_json(), await capture(report))
        await self.worker("cup", "resume-ready", sink).forecast_question(q)
        self.assertEqual(sink.forecasts, [(q.id_of_question, .41)])
        self.assertEqual(sink.comments, [(q.id_of_post, report.explanation)])
        self.assertEqual(calls, [])

    async def test_nonbinary_and_conditional_recovery_preserve_typed_reports_and_wire_plan(self):
        from forecasting_tools import ConditionalPrediction
        from forecasting_tools.data_models.data_organizer import DataOrganizer
        from .test_forecast_validation import api_post, capture, conditional, distribution, identified, mc

        calls = self.model()
        questions = [identified("fda"), identified("percentage"), identified("date"),
                     DataOrganizer.get_question_from_post_json(api_post("discrete")), conditional()]
        for q in questions:
            with self.subTest(kind=q.question_type):
                prediction = (mc(q) if q.question_type == "multiple_choice" else
                              ConditionalPrediction(parent=.5, child=.5, prediction_yes=.7, prediction_no=.3)
                              if q.question_type == "conditional" else distribution(q))
                report_type = DataOrganizer.get_report_type_for_question_type(type(q))
                report = report_type(question=q, prediction=prediction, explanation="# Original typed report")
                operations = await capture(report)
                saved = report.model_dump_json(exclude_computed_fields=True, serialize_as_any=True)
                sink = LocalMetaculus()
                old = self.worker("tournament", "old-typed", sink)
                old._publication_runtime()
                key = old._publication_key(q)
                old.publication_store.prepare(key, saved, operations)
                old.publication_store.claim(key, 0)
                old.publication_store.finish(key, 0, "confirmed")
                old.publication_store.claim(key, 1)
                old.publication_store.finish(key, 1, "rejected")
                recovered = self.worker("cup", "recover-typed", sink)
                restored = await recovered.forecast_question(q)
                self.assertIsInstance(restored, report_type)
                self.assertEqual(self.runner().report(recovered._publication_key(q)), saved)
                steps = self.runner().steps(key)
                self.assertEqual([json.loads(step["operation"]) for step in steps], operations)
                self.assertTrue(all(step["state"] == "confirmed" for step in steps))
                self.assertEqual(len(sink.forecasts), sum(op["kind"] == "forecast" for op in operations) - 1)
                self.assertEqual(len(sink.comments), sum(op["kind"] == "comment" for op in operations))
        self.assertEqual(calls, [])

    async def test_lost_final_checkpoint_keeps_binding_and_never_reposts_accepted_comment(self):
        calls = self.model()
        for index, lost in enumerate(("before", "after")):
            with self.subTest(lost=lost):
                q = question("japanese_diet").model_copy(update={"id_of_question": 5100 + index, "id_of_post": 6100 + index})
                q, sink, old, key = await self.rejected_plan("tournament", q=q)
                before = len(calls)
                comment = sink.post_question_comment

                def accept_and_lose_checkpoint(*args, **kwargs):
                    comment(*args, **kwargs)
                    self.server.lose_ack = lost

                with patch.object(sink, "post_question_comment", accept_and_lose_checkpoint):
                    result = await self.worker("cup", "final-ack-run", sink).forecast_questions([q], return_exceptions=True)
                self.assertIsInstance(result[0], Exception)
                rerun = self.worker("cup", "final-ack-run", sink)
                result = await rerun.forecast_questions([q], return_exceptions=True)
                if lost == "before":
                    self.assertIsInstance(result[0], Exception)
                    step = self.runner().steps(key)[1]
                    self.assertEqual(step["state"], "sending")
                    # Explicit simulated operator evidence, no history guessing.
                    self.runner().reconcile(key, 1, step["operation_hash"], accepted=True)
                    await self.worker("cup", "final-ack-run", sink).forecast_question(q)
                else:
                    self.assertNotIsInstance(result[0], Exception)
                self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))
                self.assertEqual(len(calls), before)


class CrossModeStoreTests(GitHubTestCase):
    def staged(self):
        store = self.runner()
        store.prepare("question/old", '{"original":true}', [{"kind": "forecast"}, {"kind": "comment"}])
        store.claim("question/old", 0)
        store.finish("question/old", 0, "confirmed")
        store.claim("question/old", 1)
        store.finish("question/old", 1, "rejected")
        return store

    def test_empty_selection_does_not_create_remote_state_or_register_resource(self):
        before = dict(self.server.files)
        self.assertEqual(self.runner().select_for_run("new/run", False), ("new/run", None, False))
        self.assertEqual(self.server.files, before)
        self.assertTrue(all(method == "GET" for method, _ in self.server.calls))

    def test_prepare_rechecks_competitor_that_arrived_during_model_work(self):
        store = self.runner()
        self.assertEqual(store.select_for_run("question/new", False), ("question/new", None, False))
        self.staged()
        key, saved, created = store.prepare_for_run("question/new", '{"discarded":true}', [{"kind": "forecast"}], False)
        self.assertEqual((key, saved, created), ("question/old", '{"original":true}', False))
        writes = []
        Publisher(store, lambda operation: writes.append(operation["kind"])).drain("question/new")
        self.assertEqual(writes, ["comment"])
        self.assertEqual(store.report("question/new"), '{"original":true}')

    def test_two_independent_recovery_runners_share_one_canonical_claim(self):
        self.staged()
        for name in ("runner-a", "runner-b"):
            self.runner().select_for_run("question/" + name, False)
        writes = []
        for name in ("runner-a", "runner-b"):
            Publisher(self.runner(), lambda operation: writes.append(operation["kind"])).drain("question/" + name)
        self.assertEqual(writes, ["comment"])

    def test_binding_cas_race_preserves_winner_and_original_operations(self):
        store = self.staged()
        original = store.steps("question/old")
        self.server.on_put = lambda: self.runner().select_for_run("question/new", False)
        result = store.select_for_run("question/new", False)
        self.assertEqual(result, ("question/old", '{"original":true}', False))
        self.assertEqual(store.steps("question/new"), original)

    def test_binding_cas_retry_keeps_recovery_if_competing_runner_finishes_old_plan(self):
        store = self.staged()
        writes = []

        def finish_competitor():
            other = self.runner()
            other.select_for_run("question/competitor", False)
            Publisher(other, lambda operation: writes.append(operation["kind"])).drain("question/competitor")

        self.server.on_put = finish_competitor
        result = store.select_for_run("question/new", False)
        self.assertEqual(result, ("question/old", '{"original":true}', False))
        self.assertEqual(store.report("question/new"), '{"original":true}')
        Publisher(store, writes.append).drain("question/new")
        self.assertEqual(writes, ["comment"])

    def test_prepare_cas_retry_cannot_turn_observed_recovery_into_a_new_plan(self):
        store = self.staged()

        def finish_competitor():
            other = self.runner()
            other.select_for_run("question/competitor", False)
            Publisher(other, lambda operation: None).drain("question/competitor")

        self.server.on_put = finish_competitor
        result = store.prepare_for_run("question/new", '{"discarded":true}', [{"kind": "forecast"}], False)
        self.assertEqual(result, ("question/old", '{"original":true}', False))
        self.assertEqual([s["state"] for s in store.steps("question/new")], ["confirmed", "confirmed"])

    def test_completed_competitor_during_model_work_prevents_duplicate_initial_forecast(self):
        store = self.runner()
        self.assertEqual(store.select_for_run("question/initial", True), ("question/initial", None, False))
        self.staged()
        Publisher(self.runner(), lambda operation: None).drain("question/old")
        result = store.prepare_for_run("question/initial", "discarded", [{"kind": "forecast"}], True)
        self.assertEqual(result, ("question/old", '{"original":true}', False))

    def test_remote_v1_snapshot_migrates_with_binding_without_resetting_index_or_steps(self):
        store = self.staged()
        original = store.steps("question/old")
        shard = self.server.focus
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            path.write_bytes(self.codec.decode(self.server.files[shard], "question"))
            local = PublicationStore(path)
            with local.connect() as db:
                db.execute("DROP TABLE publication_aliases")
                db.execute("PRAGMA user_version = 1")
            self.server.files[shard] = self.codec.encode(path.read_bytes(), "question")
            index = self.server.files[PATH]
            self.runner().select_for_run("question/new", False)
            path.write_bytes(self.codec.decode(self.server.files[shard], "question"))
            with PublicationStore(path).connect() as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT publication_key FROM publication_aliases WHERE key='question/new'").fetchone()[0], "question/old")
        self.assertEqual(self.server.files[PATH], index)
        self.assertEqual(self.runner().steps("question/new"), original)

    def test_alias_cannot_create_new_plan_through_legacy_prepare(self):
        store = self.staged()
        store.select_for_run("question/new", False)
        self.assertEqual(store.prepare("question/new", "wrong", [{"kind": "forecast"}]), ('{"original":true}', False))
        self.assertEqual(store.report("question/new"), store.report("question/old"))

    def test_reconciliation_through_binding_still_requires_exact_hash_and_explicit_evidence(self):
        store = self.staged()
        store.claim("question/old", 1)
        store.finish("question/old", 1, "uncertain")
        store.select_for_run("question/new", False)
        step = store.steps("question/new")[1]
        with self.assertRaises(ValueError):
            store.reconcile("question/new", 1, "wrong", accepted=True)
        store.reconcile("question/new", 1, step["operation_hash"], accepted=True)
        writes = []
        Publisher(store, writes.append).drain("question/new")
        self.assertEqual(writes, [])

    def test_registered_missing_shard_still_blocks_binding(self):
        self.staged()
        del self.server.files[self.server.focus]
        with self.assertRaises(GitHubStateError):
            self.runner().select_for_run("question/new", False)
        self.assertEqual(set(self.server.files), {PATH})


class LocalBindingTests(OfflineTestCase):
    def test_v1_migration_preserves_all_frozen_data_and_persists_v2_binding(self):
        store = self.publication_store
        store.prepare("question/old", '{"report":true}', [{"kind": "forecast"}, {"kind": "comment"}])
        store.claim("question/old", 0)
        store.finish("question/old", 0, "confirmed")
        original = store.steps("question/old")
        with store.connect() as db:
            db.execute("DROP TABLE publication_aliases")
            db.execute("PRAGMA user_version = 1")
        reopened = PublicationStore(store.path)
        self.assertEqual(reopened.steps("question/old"), original)
        self.assertEqual(reopened.select_for_run("question/new", False), ("question/old", '{"report":true}', False))
        fresh = PublicationStore(store.path)
        self.assertEqual(fresh.steps("question/new"), original)
        with fresh.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_cross_resource_binding_fails_closed(self):
        store = self.publication_store
        store.prepare("other/old", "{}", [{"kind": "forecast"}])
        with store.connect() as db:
            db.execute("INSERT INTO publication_aliases VALUES ('question/new', 'other/old')")
        with self.assertRaises(PublicationUncertain):
            store.select_for_run("question/new", False)

    def test_multiple_incomplete_legacy_plans_fail_closed(self):
        store = self.publication_store
        for index in (1, 2):
            with store.connect() as db:
                db.execute("INSERT INTO publications VALUES (?, 'question', '{}')", (f"question/{index}",))
                db.execute("INSERT INTO steps VALUES (?, 0, '{}', 'fake', 'uncertain')", (f"question/{index}",))
        with self.assertRaises(PublicationUncertain):
            store.select_for_run("question/new", False)

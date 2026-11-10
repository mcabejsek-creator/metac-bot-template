"""Idle recovery is comment-only, bounded, and independent of paid forecasting."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from forecasting_tools import BinaryReport, MultipleChoiceReport, NumericReport
from forecasting_tools.data_models.numeric_report import DateReport
from forecasting_tools.data_models.data_organizer import DataOrganizer

from .support import LocalMetaculus, NOW, OfflineTestCase, app
from .test_forecast_validation import api_post, capture, distribution, mc
from automatic_comment_recovery import (
    MAX_RESOURCES_PER_PASS, recover_one_comment, recovery_window,
)
from publication_journal import PublicationRejected, PublicationUncertain


class CommentRecoveryTests(OfflineTestCase):
    async def plan(self, bot, *, index=0, kind="binary", forecast="confirmed", comment="ready"):
        question = DataOrganizer.get_question_from_post_json(
            api_post(kind, qid=71001 + index, post_id=91001 + index),
        ).model_copy(update={
            "open_time": NOW - timedelta(days=2),
            "close_time": NOW - timedelta(days=1),
            "state": "open",  # Frozen at the original forecast, now closed.
        })
        report_type, prediction = {
            "binary": (BinaryReport, lambda: .65),
            "multiple_choice": (MultipleChoiceReport, lambda: mc(question)),
            "numeric": (NumericReport, lambda: distribution(question)),
            "discrete": (NumericReport, lambda: distribution(question)),
            "date": (DateReport, lambda: distribution(question)),
        }[kind]
        report = report_type(question=question, prediction=prediction(),
                             explanation="# Frozen rationale\nPůvodní text, never regenerate.")
        bot._publication_runtime()
        resource, key = bot._publication_resource(question), bot._publication_key(question)
        store = bot._publication_store
        store.prepare(key, report.model_dump_json(exclude_computed_fields=True, serialize_as_any=True),
                      await capture(report))
        for position, state in enumerate((forecast, comment)):
            if state != "ready":
                store.claim(key, position)
                if state != "sending":
                    store.finish(key, position, state)
        return resource, key, report

    async def test_ready_and_rejected_comments_recover_after_close_for_all_atomic_types(self):
        bot = self.bot(publish_reports_to_metaculus=True)
        sink = bot.metaculus_client
        for index, (kind, state) in enumerate((kind, state)
                for kind in ("binary", "multiple_choice", "numeric", "discrete", "date")
                for state in ("ready", "rejected")):
            resource, key, report = await self.plan(bot, index=index, kind=kind, comment=state)
            with self.subTest(kind=kind, state=state), patch.object(
                self.publication_store, "registered_resources", return_value=frozenset({resource}),
            ):
                result = await recover_one_comment(bot, now=NOW)
                self.assertEqual(result["status"], "comment_confirmed")
                self.assertEqual(sink.comments[-1], (report.question.id_of_post, report.explanation))
                self.assertEqual([s["state"] for s in self.publication_store.steps(key)],
                                 ["confirmed", "confirmed"])
                again = await recover_one_comment(bot, now=NOW)
                self.assertEqual(again["status"], "no_recoverable_comment")
                self.assertEqual(len(sink.comments), index + 1)
        self.assertEqual(sink.forecasts, [])

    async def test_idle_mode_reaches_closed_recovery_without_models_credit_or_research(self):
        bot = self.bot(publish_reports_to_metaculus=True)
        sink = bot.metaculus_client
        await self.plan(bot)
        scanned = []

        def scan(client, tournament, **kwargs):
            scanned.append(tournament)
            return []  # The old question is no longer in either open inventory.

        def forbidden(*args, **kwargs):
            raise AssertionError("Comment recovery must not call forecasting or providers")

        self.start_patch(patch.object(app, "BoundedPostReader", lambda *_: object()))
        self.start_patch(patch.object(app, "scan_open_tournament", scan))
        self.start_patch(patch.object(app, "verify_sponsored_openrouter_credit", forbidden))
        self.start_patch(patch.object(bot, "forecast_questions", forbidden))
        self.start_patch(patch("fixed_search_research.FixedSearchClient.from_environment", forbidden))
        result = await app.forecast_for_mode(bot, "tournament", automatic_one=True)
        self.assertEqual(result, [])
        self.assertEqual(scanned, [33121, app.MetaculusClient.CURRENT_MINIBENCH_ID])
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (0, 1))

    async def test_prior_forecast_in_open_inventory_does_not_hide_its_comment(self):
        bot = self.bot(publish_reports_to_metaculus=True)
        sink = bot.metaculus_client
        _, _, report = await self.plan(bot)
        prior = report.question.model_copy(update={
            "already_forecasted": True, "close_time": NOW + timedelta(hours=2),
        })
        self.start_patch(patch.object(app, "BoundedPostReader", lambda *_: object()))
        self.start_patch(patch.object(app, "scan_open_tournament",
            lambda client, tournament, **kwargs: [prior] if tournament == 33121 else []))
        self.assertEqual(await app.forecast_for_mode(bot, "tournament", automatic_one=True), [])
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (0, 1))

    async def test_unconfirmed_forecasts_and_ambiguous_comments_stay_untouched(self):
        bot = self.bot(publish_reports_to_metaculus=True)
        sink = bot.metaculus_client
        pairs = [(state, "ready") for state in ("ready", "rejected", "sending", "uncertain")]
        pairs += [("confirmed", state) for state in ("sending", "uncertain", "confirmed")]
        for index, (forecast, comment) in enumerate(pairs):
            resource, key, _ = await self.plan(bot, index=index, forecast=forecast, comment=comment)
            before = self.publication_store.steps(key)
            with self.subTest(forecast=forecast, comment=comment), patch.object(
                self.publication_store, "registered_resources", return_value=frozenset({resource}),
            ):
                self.assertEqual((await recover_one_comment(bot, now=NOW))["status"],
                                 "no_recoverable_comment")
                self.assertEqual(self.publication_store.steps(key), before)
        self.assertEqual((sink.forecasts, sink.comments), ([], []))

    async def test_comment_only_drain_rechecks_forecast_state_at_its_own_boundary(self):
        bot = self.bot(publish_reports_to_metaculus=True)
        sink = bot.metaculus_client
        _, key, report = await self.plan(bot, forecast="ready")
        with self.assertRaises(PublicationUncertain):
            await bot._drain_publication(bot._publication_publisher, key, report.question,
                                         comments_only=True)
        self.assertEqual([s["state"] for s in self.publication_store.steps(key)], ["ready", "ready"])
        self.assertEqual((sink.forecasts, sink.comments), ([], []))

    async def test_frozen_operation_hash_change_blocks_comment(self):
        bot = self.bot(publish_reports_to_metaculus=True)
        sink = bot.metaculus_client
        _, key, _ = await self.plan(bot)
        with self.publication_store.connect() as db:
            db.execute("UPDATE steps SET operation_hash = 'tampered' WHERE publication_key = ? AND position = 1", (key,))
        with self.assertRaises(ValueError):
            await recover_one_comment(bot, now=NOW)
        self.assertEqual((sink.forecasts, sink.comments), ([], []))

    async def test_state_change_after_selection_cannot_replay_forecast(self):
        bot = self.bot(publish_reports_to_metaculus=True)
        sink = bot.metaculus_client
        _, key, _ = await self.plan(bot)
        original_steps = self.publication_store.steps
        reads = 0

        def changed_steps(requested_key):
            nonlocal reads
            reads += 1
            if reads == 2:  # Fresh drain read, after preliminary candidate selection.
                with self.publication_store.connect() as db:
                    db.execute("UPDATE steps SET state = 'ready' WHERE publication_key = ? AND position = 0", (key,))
            return original_steps(requested_key)

        with patch.object(self.publication_store, "steps", changed_steps), self.assertRaises(PublicationUncertain):
            await recover_one_comment(bot, now=NOW)
        self.assertEqual((sink.forecasts, sink.comments), ([], []))

    async def test_declined_claim_without_confirmed_receipt_does_not_report_success(self):
        bot = self.bot(publish_reports_to_metaculus=True)
        sink = bot.metaculus_client
        await self.plan(bot)
        with patch.object(self.publication_store, "claim", return_value=False), self.assertRaises(PublicationUncertain):
            await recover_one_comment(bot, now=NOW)
        self.assertEqual((sink.forecasts, sink.comments), ([], []))

    async def test_saved_question_cannot_be_moved_to_another_resource(self):
        bot = self.bot(publish_reports_to_metaculus=True)
        sink = bot.metaculus_client
        _, key, _ = await self.plan(bot)
        saved = json.loads(self.publication_store.report(key))
        saved["question"]["id_of_post"] += 1
        with self.publication_store.connect() as db:
            db.execute("UPDATE publications SET report = ? WHERE key = ?", (json.dumps(saved), key))
        with self.assertRaises(PublicationUncertain):
            await recover_one_comment(bot, now=NOW)
        self.assertEqual((sink.forecasts, sink.comments), ([], []))

    async def test_only_one_comment_attempt_per_pass_even_when_rejected(self):
        class RejectingSink(LocalMetaculus):
            attempts = 0
            def post_question_comment(self, *args, **kwargs):
                self.attempts += 1
                raise PublicationRejected(429)

        sink = RejectingSink()
        bot = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        await self.plan(bot, index=1)
        await self.plan(bot, index=2)
        with self.assertRaises(PublicationRejected):
            await recover_one_comment(bot, now=NOW)
        self.assertEqual(sink.attempts, 1)
        self.assertEqual(sink.forecasts, [])

    async def test_uncertain_comment_attempt_is_not_repeated_on_later_pass(self):
        sink = LocalMetaculus(fail_comment=True)
        bot = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        _, key, _ = await self.plan(bot)
        with self.assertRaises(PublicationUncertain):
            await recover_one_comment(bot, now=NOW)
        self.assertEqual(self.publication_store.steps(key)[1]["state"], "uncertain")
        sink.fail_comment = False
        self.assertEqual((await recover_one_comment(bot, now=NOW))["status"], "no_recoverable_comment")
        self.assertEqual((sink.forecasts, sink.comments), ([], []))

    def test_five_resource_window_rotates_across_the_full_existing_registry(self):
        start = datetime(1970, 1, 1, tzinfo=timezone.utc)
        resources = frozenset(f"resource-{index:02}" for index in range(13))
        windows = [recovery_window(resources, now=start + timedelta(minutes=15 * slot))
                   for slot in range(3)]
        self.assertTrue(all(len(window) == MAX_RESOURCES_PER_PASS for window in windows))
        self.assertEqual(set().union(*map(set, windows)), resources)
        self.assertEqual(recovery_window(frozenset(), now=start), [])
        self.assertEqual(len(recovery_window(frozenset({"one"}), now=start)), 1)

    async def test_dry_run_does_not_bind_any_publication_runtime(self):
        bot = self.bot()
        with patch.object(bot, "_publication_runtime", side_effect=AssertionError("No journal in dry run")):
            self.assertEqual((await recover_one_comment(bot, now=NOW))["status"], "disabled")

    async def test_empty_remote_registry_is_read_only_and_missing_manifest_is_not_created(self):
        from github_publication import GitHubContents, GitHubPublicationStore, GitHubStateError, JournalCodec
        from .test_github_publication import BRANCH, CONTEXT, GITHUB_KEY, METACULUS_KEY, PATH, REPOSITORY, MemoryGitHub

        codec = JournalCodec(METACULUS_KEY, CONTEXT)
        remote = MemoryGitHub(codec.encode(json.dumps({"version": 1, "resources": []}).encode()))
        store = GitHubPublicationStore(
            GitHubContents(REPOSITORY, BRANCH, PATH, GITHUB_KEY, session=remote), codec)
        bot = self.bot(publish_reports_to_metaculus=True)
        bot.publication_store = store
        before = dict(remote.files)
        self.assertEqual((await recover_one_comment(bot, now=NOW))["status"], "no_recoverable_comment")
        self.assertEqual(remote.files, before)
        self.assertTrue(all(method == "GET" for method, _ in remote.calls))
        del remote.files[PATH]
        with self.assertRaises(GitHubStateError):
            await recover_one_comment(bot, now=NOW)
        self.assertEqual(remote.files, {})
        self.assertTrue(all(method == "GET" for method, _ in remote.calls))

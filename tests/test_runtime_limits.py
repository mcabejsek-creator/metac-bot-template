import asyncio
import contextvars
import signal
import subprocess
import threading
from dataclasses import replace
from unittest.mock import patch

from .support import OfflineTestCase, app, question
from publication import SafePublicationMixin
from publication_journal import PublicationUncertain, Publisher
from runtime_limits import RuntimeLimits, cancellable_cli
import bounded_run


class RuntimeTestHelpers:
    def timeout_probe(self):
        original, observed = asyncio.timeout, []

        def timeout(seconds):
            guard = original(seconds)
            observed.append((seconds, guard))
            return guard

        self.start_patch(patch.object(asyncio, "timeout", timeout))
        return observed

    @staticmethod
    def expire(observed, seconds):
        guards = [guard for budget, guard in observed if budget == seconds]
        if not guards:
            raise AssertionError(f"No deadline installed for {seconds} seconds")
        guards[-1].reschedule(asyncio.get_running_loop().time())

    async def wait_idle(self, bot):
        async with asyncio.timeout(2):
            while bot._runtime_state().workers:
                await asyncio.sleep(.001)


class RuntimeTests(RuntimeTestHelpers, OfflineTestCase):
    def test_limits_are_generous_and_reject_unbounded_values(self):
        limits = RuntimeLimits()
        self.assertEqual((limits.run_seconds, limits.question_seconds, limits.conditional_seconds), (10800, 3600, 7200))
        self.assertGreater(limits.agent_seconds, 180)
        self.assertGreaterEqual(limits.model_seconds, 2 * 60 + 60)
        for value in (0, -1, float("inf"), float("nan"), None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(limits, run_seconds=value)

    async def test_model_copy_preserves_original_settings_and_metadata(self):
        original = app.GeneralLlm(model="openrouter/openai/gpt-4o", temperature=.3, timeout=40, allowed_tries=2)
        bot = self.bot()
        bot.set_llm(original)
        before = bot.make_llm_dict()
        wrapped = bot.get_llm("default", "llm")
        self.assertIsInstance(wrapped, app.GeneralLlm)
        self.assertEqual(wrapped.to_dict(), original.to_dict())
        self.assertEqual(bot.make_llm_dict(), before)
        self.assertNotIn("invoke", original.__dict__)

    async def test_all_model_purposes_share_one_operation_limit(self):
        active = peak = 0
        entered, release = asyncio.Event(), asyncio.Event()

        async def invoke(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            entered.set()
            try:
                await release.wait()
                return "text"
            finally:
                active -= 1

        self.start_patch(patch.object(app.GeneralLlm, "invoke", invoke))
        bot = self.bot()
        tasks = [asyncio.create_task(bot.get_llm(purpose, "llm").invoke("unchanged"))
                 for purpose in ("default", "parser", "summarizer")]
        try:
            await entered.wait()
            for _ in range(15):
                await asyncio.sleep(0)
            self.assertEqual(active, 1)
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(peak, 1)

    async def test_parser_verified_type_uses_the_bounded_invoke(self):
        entered = asyncio.Event()

        async def stalled(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.start_patch(patch.object(app.GeneralLlm, "invoke", stalled))
        observed = self.timeout_probe()
        bot = self.bot()
        task = asyncio.create_task(bot.get_llm("parser", "llm").invoke_and_return_verified_type(
            "local", str, allowed_invoke_tries_for_failed_output=1))
        await entered.wait()
        self.expire(observed, bot.runtime_limits.model_seconds)
        with self.assertRaises(TimeoutError):
            await task
        self.assertEqual(bot._runtime_state().operations._value, 1)

    async def test_run_deadline_cancels_all_model_calls_and_queued_questions(self):
        entered, stopped = asyncio.Event(), asyncio.Event()

        async def stalled(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        self.start_patch(patch.object(app.GeneralLlm, "invoke", stalled))
        observed = self.timeout_probe()
        bot = self.bot()
        questions = [question("japanese_diet").model_copy(update={"id_of_question": i}) for i in range(5)]
        task = asyncio.create_task(bot.forecast_questions(questions, return_exceptions=True))
        await entered.wait()
        self.expire(observed, bot.runtime_limits.run_seconds)
        with self.assertRaises(TimeoutError):
            await task
        self.assertTrue(stopped.is_set())
        self.assertEqual(bot._note_pads, [])
        self.assertEqual(bot._runtime_state().questions._value, 1)
        self.assertEqual(bot._runtime_state().operations._value, 1)

    async def test_cancellation_leaves_no_forecast_or_parser_tasks_alive(self):
        entered = asyncio.Event()
        calls = []

        async def stalled(model, prompt, **kwargs):
            calls.append(prompt)
            entered.set()
            await asyncio.Event().wait()

        self.start_patch(patch.object(app.GeneralLlm, "invoke", stalled))
        bot = self.bot(publish_reports_to_metaculus=True)
        task = asyncio.create_task(bot.forecast_question(question("japanese_diet")))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        count = len(calls)
        for _ in range(15):
            await asyncio.sleep(0)
        self.assertEqual(len(calls), count)
        self.assertEqual(bot._note_pads, [])
        self.assertFalse(bot.metaculus_client.forecasts)
        self.assertIsNone(self.publication_store.pending(bot._publication_resource(question("japanese_diet"))))

    async def test_ordinary_batch_error_cancels_and_joins_siblings(self):
        bot = self.bot()
        bot._max_concurrent_questions = 2
        sibling_entered, sibling_stopped = asyncio.Event(), asyncio.Event()

        async def work(_, q):
            if q.id_of_question == 1:
                await sibling_entered.wait()
                raise ValueError("local failure")
            sibling_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                sibling_stopped.set()

        self.start_patch(patch.object(SafePublicationMixin, "_run_individual_question", work))
        questions = [question("japanese_diet").model_copy(update={"id_of_question": i}) for i in (1, 2)]
        with self.assertRaises(RuntimeError):
            await bot.forecast_questions(questions)
        self.assertTrue(sibling_stopped.is_set())
        self.assertEqual(bot._runtime_state().questions._value, 2)

    async def test_question_deadline_starts_after_admission_and_conditional_gets_larger_budget(self):
        from forecasting_tools import BinaryQuestion, ConditionalQuestion
        bot = self.bot()
        observed = self.timeout_probe()
        entered, release = asyncio.Event(), asyncio.Event()

        async def work(_, q):
            entered.set()
            await release.wait()
            return q

        self.start_patch(patch.object(SafePublicationMixin, "_run_individual_question", work))
        parts = [BinaryQuestion(question_text=str(i)) for i in range(4)]
        conditional = ConditionalQuestion(question_text="local", parent=parts[0], child=parts[1],
                                          question_yes=parts[2], question_no=parts[3])
        task = asyncio.create_task(bot.forecast_questions([question("japanese_diet"), conditional]))
        await entered.wait()
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual(sum(budget == 3600 for budget, _ in observed), 1)
        self.assertFalse(any(budget == 7200 for budget, _ in observed))
        release.set()
        await task
        self.assertEqual(sum(budget == 7200 for budget, _ in observed), 1)

    async def test_both_tournaments_share_one_total_budget(self):
        bot = self.bot()
        calls, observed = [], self.timeout_probe()
        identifiers = [33121, app.MetaculusClient.CURRENT_MINIBENCH_ID]
        scan_completed = []

        def scan(client, identifier, *, read_page):
            self.assertIs(client, bot.metaculus_client)
            scan_completed.append(identifier)
            return [identifier]

        async def forecast(questions, return_exceptions):
            self.assertTrue(return_exceptions)
            # All read-only scans must have succeeded before forecasting starts.
            self.assertEqual(scan_completed, identifiers)
            calls.extend(questions)
            return list(questions)

        self.start_patch(patch.object(app, "BoundedPostReader", lambda *_: object()))
        self.start_patch(patch.object(app, "scan_open_tournament", scan))
        self.start_patch(patch.object(app, "deduplicate_tournaments", lambda a, b: (a, b)))
        self.start_patch(patch.object(bot, "forecast_questions", forecast))
        reports = await app.forecast_for_mode(bot, "tournament")
        self.assertEqual(calls, identifiers)
        self.assertEqual(reports, identifiers)
        self.assertEqual(sum(budget == 10800 for budget, _ in observed), 1)

    async def test_empty_both_tournament_scans_never_initialize_publication(self):
        # A real empty snapshot must not touch the remote journal, model
        # gateway or POST path even if publishing is configured.
        bot = self.bot(publish_reports_to_metaculus=True)
        scanned = []

        def scan(client, tournament, *, read_page):
            self.assertIs(client, bot.metaculus_client)
            scanned.append(tournament)
            return []

        self.start_patch(patch.object(app, "BoundedPostReader", lambda *_: object()))
        self.start_patch(patch.object(app, "scan_open_tournament", scan))
        self.start_patch(patch.object(
            bot, "forecast_questions",
            side_effect=AssertionError("No empty forecast batch may reach the publisher")))
        self.start_patch(patch.object(
            bot, "_publication_runtime",
            side_effect=AssertionError("Remote publication journal must not open")))
        result = await app.forecast_for_mode(bot, "tournament")
        self.assertEqual(result, [])
        self.assertEqual(scanned, [33121, app.MetaculusClient.CURRENT_MINIBENCH_ID])
        self.assertEqual(self.publication_store.pending("synthetic"), None)

    async def test_only_nonempty_tournament_is_forecast(self):
        bot = self.bot()
        scans, forecast_calls = [], []

        def scan(client, tournament, *, read_page):
            scans.append(tournament)
            return [] if len(scans) == 1 else [3]

        async def forecast(questions, return_exceptions):
            self.assertEqual(scans, [33121, app.MetaculusClient.CURRENT_MINIBENCH_ID])
            self.assertTrue(return_exceptions)
            forecast_calls.append(list(questions))
            return list(questions)

        self.start_patch(patch.object(app, "BoundedPostReader", lambda *_: object()))
        self.start_patch(patch.object(app, "scan_open_tournament", scan))
        self.start_patch(patch.object(app, "deduplicate_tournaments", lambda a, b: (a, b)))
        self.start_patch(patch.object(bot, "forecast_questions", forecast))
        result = await app.forecast_for_mode(bot, "tournament")
        self.assertEqual(result, [3])
        self.assertEqual(forecast_calls, [[3]])

    async def test_failed_second_scan_never_starts_forecasting(self):
        bot = self.bot()
        scanned, forecasts_started = [], []

        def scan(client, tournament, *, read_page):
            scanned.append(tournament)
            if len(scanned) == 2:
                raise RuntimeError("Synthetic incomplete second tournament")
            return [1]

        async def forecast(*args, **kwargs):
            forecasts_started.append(True)
            raise AssertionError("No forecast is allowed after incomplete inventory")

        self.start_patch(patch.object(app, "BoundedPostReader", lambda *_: object()))
        self.start_patch(patch.object(app, "scan_open_tournament", scan))
        self.start_patch(patch.object(bot, "forecast_questions", forecast))
        with self.assertRaisesRegex(RuntimeError, "incomplete second tournament"):
            await app.forecast_for_mode(bot, "tournament")
        self.assertEqual(scanned, [33121, app.MetaculusClient.CURRENT_MINIBENCH_ID])
        self.assertEqual(forecasts_started, [])

    async def test_run_deadline_also_covers_tournament_retrieval(self):
        bot = self.bot()
        observed = self.timeout_probe()
        entered, release = threading.Event(), threading.Event()

        def fetch(identifier):
            entered.set()
            if not release.wait(2):
                raise AssertionError("Worker not released")
            return []

        bot.metaculus_client.get_all_open_questions_from_tournament = fetch
        task = asyncio.create_task(bot.forecast_on_tournament("offline"))
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            self.expire(observed, bot.runtime_limits.run_seconds)
            with self.assertRaises(TimeoutError):
                await task
            self.assertEqual(bot._runtime_state().workers, 1)
        finally:
            release.set()
            await self.wait_idle(bot)

    async def test_cancelled_blocking_worker_keeps_its_permit_until_real_exit(self):
        bot = self.bot()
        entered, release = threading.Event(), threading.Event()
        second_ran = threading.Event()

        def blocking():
            entered.set()
            if not release.wait(2):
                raise AssertionError("Worker not released")

        first = asyncio.create_task(bot._run_blocking(blocking))
        second = None
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(bot._run_blocking(second_ran.set))
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(second_ran.is_set())
            self.assertEqual(bot._runtime_state().workers, 1)
        finally:
            release.set()
            if second is not None:
                await second
            await self.wait_idle(bot)
        self.assertTrue(second_ran.is_set())

    async def test_blocking_timeout_is_absolute_and_worker_preserves_context(self):
        bot = self.bot()
        marker = contextvars.ContextVar("offline_context", default="wrong")
        token = marker.set("copied")
        try:
            self.assertEqual(await bot._run_blocking(marker.get), "copied")
        finally:
            marker.reset(token)
        entered, release = threading.Event(), threading.Event()
        observed = self.timeout_probe()

        def slow():
            entered.set()
            release.wait(2)

        task = asyncio.create_task(bot._run_blocking(slow))
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            self.expire(observed, bot.runtime_limits.blocking_seconds)
            with self.assertRaises(TimeoutError):
                await task
            self.assertEqual(bot._runtime_state().workers, 1)
        finally:
            release.set()
            await self.wait_idle(bot)

    async def test_model_cannot_overlap_blocking_research_operation(self):
        calls = self.model()
        bot = self.bot()
        entered, release = threading.Event(), threading.Event()

        def research():
            entered.set()
            release.wait(2)

        task = asyncio.create_task(bot._run_blocking(research))
        model = None
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            model = asyncio.create_task(bot.get_llm("default", "llm").invoke("local"))
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertEqual(calls, [])
        finally:
            release.set()
            await task
            if model is not None:
                await model
        self.assertEqual(len(calls), 1)

    async def test_agent_absolute_deadline_prevents_late_fact_check(self):
        import json
        import os
        import urllib.request
        from .support import agent_response
        entered, release = threading.Event(), threading.Event()
        calls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                entered.set()
                release.wait(2)
                return json.dumps(agent_response("late local data")).encode()

        def request(*args, **kwargs):
            calls.append(kwargs["timeout"])
            return Response()

        self.start_patch(patch.dict(os.environ, {"PERPLEXITY_API_KEY": "TEST_ONLY_NOT_A_REAL_CREDENTIAL"}))
        self.start_patch(patch.object(urllib.request, "urlopen", request))
        bot = self.bot()
        bot.set_llm("perplexity-agent", "researcher")
        observed = self.timeout_probe()
        task = asyncio.create_task(bot.run_research(question("japanese_diet")))
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            self.expire(observed, bot.runtime_limits.agent_seconds)
            with self.assertRaises(TimeoutError):
                await task
        finally:
            release.set()
            await self.wait_idle(bot)
        self.assertEqual(calls, [180])

    async def test_cancel_during_writer_throttle_prevents_late_http_post(self):
        from publication import SingleAttemptWriter
        bot = self.bot()
        client = app.MetaculusClient()
        entered, release = threading.Event(), threading.Event()

        def throttle():
            entered.set()
            release.wait(2)

        self.start_patch(patch.object(client, "_sleep_between_requests", throttle))
        post = self.start_patch(patch("publication.requests.post"))
        task = asyncio.create_task(bot._run_blocking(SingleAttemptWriter(client), {
            "kind": "forecast", "question_id": 1, "payload": {"probability_yes": .65}}))
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await self.wait_idle(bot)
        post.assert_not_called()

    async def test_sigterm_cancels_cli_and_restores_signal_handlers(self):
        handlers, removed = {}, []
        loop = asyncio.get_running_loop()
        entered, stopped = asyncio.Event(), asyncio.Event()

        async def work():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with patch.object(loop, "add_signal_handler", side_effect=lambda sig, fn: handlers.setdefault(sig, fn)), \
             patch.object(loop, "remove_signal_handler", side_effect=removed.append), \
             patch("runtime_limits.signal.signal"):
            task = asyncio.create_task(cancellable_cli(work()))
            await entered.wait()
            handlers[signal.SIGTERM]()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(stopped.is_set())
        self.assertEqual(set(removed), {signal.SIGTERM, signal.SIGINT})


class PublicationCancellationTests(RuntimeTestHelpers, OfflineTestCase):
    """Each fault is injected after observing its boundary, not a timed sleep."""

    async def test_expired_question_blocks_new_forecast_without_claim_or_post(self):
        from datetime import datetime, timezone
        from forecasting_tools import BinaryReport
        from publication_journal import Publisher
        now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
        q = question("japanese_diet")
        expired = q.model_copy(update={"close_time": datetime(
            2026, 11, 6, 11, 59, tzinfo=timezone.utc)})
        # A formerly valid scheduled close must never override an earlier
        # authoritative actual_close_time in the API snapshot.
        actual_expired = q.model_copy(update={"api_json": {
            "question": {"actual_close_time": "2026-11-06T11:59:00Z"}}})
        for index, stale in enumerate((expired, actual_expired)):
            report = BinaryReport(question=stale, prediction=.65, explanation="# Frozen synthetic report")
            key = f"deadline-{index}/initial"
            operations = [
                {"kind": "forecast", "question_id": stale.id_of_question,
                 "payload": {"probability_yes": .65}},
                {"kind": "comment", "post_id": stale.id_of_post,
                 "text": report.explanation, "is_private": True, "included_forecast": True},
            ]
            self.publication_store.prepare(
                key, report.model_dump_json(exclude_computed_fields=True), operations)
            writes = []
            bot = self.bot()
            with patch("question_context.utc_now", return_value=now):
                with self.assertRaisesRegex(PublicationUncertain, "no new POST"):
                    await bot._drain_publication(
                        Publisher(self.publication_store, writes.append), key, stale)
            self.assertEqual(writes, [])
            self.assertEqual(
                [step["state"] for step in self.publication_store.steps(key)],
                ["ready", "ready"],
            )

    async def test_completed_forecast_comment_can_recover_after_close_without_new_forecast(self):
        from datetime import datetime, timezone
        from forecasting_tools import BinaryReport
        from publication_journal import Publisher
        q = question("japanese_diet")
        report = BinaryReport(question=q, prediction=.65, explanation="# Frozen synthetic report")
        key = "expired-recovery/initial"
        self.publication_store.prepare(
            key, report.model_dump_json(exclude_computed_fields=True), [
                {"kind": "forecast", "question_id": q.id_of_question,
                 "payload": {"probability_yes": .65}},
                {"kind": "comment", "post_id": q.id_of_post,
                 "text": report.explanation, "is_private": True, "included_forecast": True},
            ])
        self.assertTrue(self.publication_store.claim(key, 0))
        self.publication_store.finish(key, 0, "confirmed")
        sent = []
        bot = self.bot()
        late_now = datetime(2026, 11, 7, 0, 30, tzinfo=timezone.utc)
        with patch("question_context.utc_now", return_value=late_now):
            await bot._drain_publication(
                Publisher(self.publication_store, sent.append), key, q)
        self.assertEqual([op["kind"] for op in sent], ["comment"])
        self.assertEqual(
            [step["state"] for step in self.publication_store.steps(key)],
            ["confirmed", "confirmed"],
        )

    def test_conditional_branch_deadline_must_be_open_for_new_forecasts(self):
        from datetime import datetime, timezone
        from forecasting_tools import ConditionalQuestion
        q = question("japanese_diet")
        closed_no = q.model_copy(update={"close_time": datetime(
            2026, 11, 6, 11, 59, tzinfo=timezone.utc)})
        conditional = ConditionalQuestion(
            question_text="Synthetic conditional only",
            parent=q, child=q, question_yes=q, question_no=closed_no)
        bot = self.bot()
        with self.assertRaisesRegex(PublicationUncertain, "no new POST"):
            bot._require_forecast_window_open(conditional)

    async def cancelled_boundary(self, boundary, *, expire=False, remote=False):
        if remote:
            from .test_github_publication import MemoryGitHub, JournalCodec, GitHubContents, GitHubPublicationStore
            from .test_github_publication import CONTEXT, METACULUS_KEY, REPOSITORY, BRANCH, PATH, GITHUB_KEY
            codec = JournalCodec(METACULUS_KEY, CONTEXT)
            server = MemoryGitHub(codec.encode(b'{"version":1,"resources":[]}'))

            def reopen():
                return GitHubPublicationStore(GitHubContents(REPOSITORY, BRANCH, PATH, GITHUB_KEY, session=server), codec)

            store = reopen()
        else:
            store = self.publication_store

            def reopen():
                from publication_journal import PublicationStore
                return PublicationStore(store.path)

        key = "local-question/intent"
        from forecasting_tools import BinaryReport
        q = question("japanese_diet")
        report = BinaryReport(question=q, prediction=.65, explanation="# Frozen local report")
        store.prepare(key, report.model_dump_json(exclude_computed_fields=True), [
            {"kind": "forecast", "question_id": q.id_of_question, "payload": {"probability_yes": .65}},
            {"kind": "comment", "post_id": q.id_of_post, "text": report.explanation,
             "is_private": True, "included_forecast": True}])
        bot = self.bot()
        entered, release = threading.Event(), threading.Event()
        writes = []
        original_claim, original_finish = store.claim, store.finish

        def pause():
            entered.set()
            if not release.wait(2):
                raise AssertionError("Worker not released")

        def claim(*args):
            result = original_claim(*args)
            if boundary == "claim":
                pause()
            return result

        def writer(operation):
            writes.append(operation["kind"])
            if boundary == operation["kind"]:
                pause()

        def finish(*args):
            original_finish(*args)
            if boundary == "finish":
                pause()

        store.claim, store.finish = claim, finish
        observed = self.timeout_probe()
        task = asyncio.create_task(bot._drain_publication(Publisher(store, writer), key, q))
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            if expire:
                self.expire(observed, bot.runtime_limits.blocking_seconds)
                with self.assertRaises(PublicationUncertain):
                    await task
            else:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
            await self.wait_idle(bot)
        return reopen(), key, writes

    async def test_cancelled_claim_ack_never_turns_into_a_late_post(self):
        store, key, writes = await self.cancelled_boundary("claim")
        self.assertEqual(writes, [])
        self.assertEqual(store.steps(key)[0]["state"], "sending")
        with self.assertRaises(PublicationUncertain):
            Publisher(store, writes.append).drain(key)
        self.assertEqual(writes, [])

    async def test_cancelled_forecast_post_stays_sending_after_worker_exit(self):
        store, key, writes = await self.cancelled_boundary("forecast")
        self.assertEqual(writes, ["forecast"])
        self.assertEqual([s["state"] for s in store.steps(key)], ["sending", "ready"])

    async def test_timed_out_forecast_does_not_wait_for_worker_to_mark_uncertain(self):
        store, key, writes = await self.cancelled_boundary("forecast", expire=True)
        self.assertEqual(writes, ["forecast"])
        self.assertEqual(store.steps(key)[0]["state"], "sending")

    async def test_cancelled_comment_preserves_confirmed_forecast_and_blocks_repeat(self):
        store, key, writes = await self.cancelled_boundary("comment")
        self.assertEqual(writes, ["forecast", "comment"])
        self.assertEqual([s["state"] for s in store.steps(key)], ["confirmed", "sending"])
        with self.assertRaises(PublicationUncertain):
            Publisher(store, writes.append).drain(key)
        self.assertEqual(writes, ["forecast", "comment"])

    async def test_cancelled_confirm_ack_preserves_remote_confirmation(self):
        store, key, writes = await self.cancelled_boundary("finish", remote=True)
        self.assertEqual(writes, ["forecast"])
        Publisher(store, lambda op: writes.append(op["kind"])).drain(key)
        self.assertEqual(writes, ["forecast", "comment"])

    async def test_independent_runner_cannot_repeat_cancelled_remote_post(self):
        store, key, writes = await self.cancelled_boundary("forecast", remote=True)
        with self.assertRaises(PublicationUncertain):
            Publisher(store, writes.append).drain(key)
        self.assertEqual(writes, ["forecast"])

    async def test_cancelled_plan_ack_resumes_frozen_report_without_new_model_calls(self):
        calls = self.model()
        bot = self.bot(publish_reports_to_metaculus=True)
        entered, release = threading.Event(), threading.Event()
        store = self.publication_store
        original = store.prepare_for_run

        def prepare(*args):
            result = original(*args)
            entered.set()
            release.wait(2)
            return result

        self.start_patch(patch.object(store, "prepare_for_run", prepare))
        q = question("japanese_diet")
        task = asyncio.create_task(bot.forecast_question(q))
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await self.wait_idle(bot)
        self.assertFalse(bot.metaculus_client.forecasts)
        before = len(calls)
        restored = await bot.forecast_question(q)
        self.assertEqual(restored.prediction, .65)
        self.assertEqual(len(calls), before)
        self.assertEqual((len(bot.metaculus_client.forecasts), len(bot.metaculus_client.comments)), (1, 1))


class SupervisorTests(OfflineTestCase):
    def test_success_and_nonzero_exit_are_forwarded_without_signals(self):
        from unittest.mock import Mock
        for status in (0, 7):
            child = Mock(pid=90001)
            child.wait.return_value, child.poll.return_value = status, status
            with patch.object(bounded_run.subprocess, "Popen", return_value=child) as spawn, \
                 patch.object(bounded_run.os, "killpg") as kill, patch.object(bounded_run.signal, "signal"):
                self.assertEqual(bounded_run.supervise(["offline-program"]), status)
                spawn.assert_called_once_with(["offline-program"], start_new_session=True)
                kill.assert_not_called()

    def test_timeout_escalates_term_to_group_kill_and_returns_124(self):
        from unittest.mock import Mock
        child = Mock(pid=90001)
        child.wait.side_effect = [subprocess.TimeoutExpired("offline", .5), subprocess.TimeoutExpired("offline", 60), 0]
        child.poll.side_effect = [None, 0]
        with patch.object(bounded_run.subprocess, "Popen", return_value=child), \
             patch.object(bounded_run.time, "monotonic", side_effect=[0, 0, 10800]), \
             patch.object(bounded_run.os, "killpg") as kill, patch.object(bounded_run.signal, "signal"):
            self.assertEqual(bounded_run.supervise(["offline-program"]), 124)
        self.assertEqual([call.args for call in kill.call_args_list], [(90001, signal.SIGTERM), (90001, signal.SIGKILL)])
        self.assertEqual(child.wait.call_args_list[1].kwargs, {"timeout": 60})

    def test_cancel_while_spawning_cleans_group_and_preserves_signal_status(self):
        from unittest.mock import Mock
        handlers = {}
        child = Mock(pid=90001)
        child.poll.return_value, child.wait.return_value = 0, 0

        def register(sig, handler):
            handlers[sig] = handler
            return signal.SIG_DFL

        def spawn(*args, **kwargs):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return child

        with patch.object(bounded_run.subprocess, "Popen", side_effect=spawn), \
             patch.object(bounded_run.os, "killpg") as kill, patch.object(bounded_run.signal, "signal", side_effect=register):
            self.assertEqual(bounded_run.supervise(["offline-program"]), 143)
        self.assertEqual([call.args[1] for call in kill.call_args_list], [signal.SIGTERM, signal.SIGKILL])

    def test_real_local_process_is_killed_without_running_a_bot_or_api_client(self):
        import sys
        # Pure stdlib child, no app imports, credentials or network operations.
        code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
        self.assertEqual(bounded_run.supervise([sys.executable, "-I", "-c", code], seconds=.15, grace=.02), 124)

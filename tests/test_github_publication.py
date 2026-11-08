import base64
import json
import os
from unittest.mock import patch
from urllib.parse import unquote

import requests

from .support import LocalMetaculus, OfflineTestCase, question
from github_publication import (
    GitHubContents, GitHubPublicationStore, GitHubStateError, JournalCodec,
    StateConflict, configured_store, git_blob_sha, initial_legacy_frozen_manifest,
    main as journal_initialization_main,
)
from publication_journal import PublicationRejected, PublicationUncertain, Publisher


REPOSITORY = "offline-owner/offline-repo"
BRANCH = "metaculus-publication-state"
PATH = "publication.enc"
SCOPE = "default-bot"
# Synthetic keys, never usable against any real service.
METACULUS_KEY = "OFFLINE-ONLY-NOT-A-REAL-METACULUS-TOKEN-0123456789"
GITHUB_KEY = "OFFLINE-ONLY-NOT-A-REAL-GITHUB-TOKEN"
CONTEXT = [REPOSITORY, BRANCH, PATH, SCOPE]


class Response:
    def __init__(self, status, data):
        self.status_code, self.data = status, data
        self.closed = False

    def json(self):
        return self.data

    def close(self):
        self.closed = True


class MemoryGitHub:
    """Fake remote repository; runners share only this service, not local files.

    Tests exercise production GitHubContents request/response validation. The
    fake models the documented conditional-file-SHA contract, not a live GitHub.
    """

    def __init__(self, content):
        self.files = {PATH: content}
        self.focus = PATH
        self.calls = []
        self.stale_reads = []
        self.on_put = None
        self.on_root_put = None
        self.lose_ack = None
        self.lose_root_ack = None
        self.status = 200

    @property
    def content(self):
        return self.files[self.focus]

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        path = unquote(url.split("/contents/", 1)[1])
        if method == "GET":
            if self.status != 200:
                return Response(self.status, {})
            if path not in self.files:
                return Response(404, {})
            content = self.stale_reads.pop(0) if self.stale_reads and path != PATH else self.files[path]
            return Response(200, {"type": "file", "path": path, "encoding": "base64",
                "size": len(content), "sha": git_blob_sha(content), "content": base64.b64encode(content).decode()})
        if method != "PUT":
            raise AssertionError("Runtime must never initialize a missing journal")
        payload = kwargs["json"]
        hook_name = "on_root_put" if path == PATH else "on_put"
        if getattr(self, hook_name):
            hook = getattr(self, hook_name)
            setattr(self, hook_name, None)
            hook()
        fault = "lose_root_ack" if path == PATH else "lose_ack"
        if getattr(self, fault) == "before":
            setattr(self, fault, None)
            raise requests.Timeout("Synthetic state request not applied")
        exists = path in self.files
        if "sha" not in payload and exists:
            return Response(422, {})
        if "sha" in payload and (not exists or payload["sha"] != git_blob_sha(self.files[path])):
            return Response(409, {})
        if payload["branch"] != BRANCH:
            raise AssertionError("Wrong state branch")
        self.files[path] = base64.b64decode(payload["content"], validate=True)
        if path != PATH:
            self.focus = path
        if getattr(self, fault) == "after":
            setattr(self, fault, None)
            raise requests.Timeout("Synthetic state commit applied but acknowledgement lost")
        return Response(200 if exists else 201, {"content": {"sha": git_blob_sha(self.files[path])}, "commit": {"sha": "c" * 40}})


class GitHubTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.codec = JournalCodec(METACULUS_KEY, CONTEXT)
        self.server = MemoryGitHub(self.codec.encode(json.dumps({"version": 1, "resources": []}).encode()))

    def runner(self, *, codec=None):
        contents = GitHubContents(REPOSITORY, BRANCH, PATH, GITHUB_KEY, session=self.server)
        return GitHubPublicationStore(contents, codec or self.codec)

    def plan(self, store=None):
        store = store or self.runner()
        operations = [{"kind": "forecast"}, {"kind": "comment"}]
        store.prepare("question/intent", '{"private":"synthetic-report"}', operations)
        return store


class RemoteJournalTests(GitHubTestCase):
    def test_registered_missing_question_journal_is_not_recreated(self):
        self.plan()
        del self.server.files[self.server.focus]
        count = len(self.server.calls)
        with self.assertRaises(GitHubStateError):
            self.runner().prepare("question/intent", "{}", [{"kind": "forecast"}])
        self.assertTrue(all(method == "GET" for method, _ in self.server.calls[count:]))

    def test_resource_registration_ack_lost_after_commit_can_resume(self):
        self.server.lose_root_ack = "after"
        with self.assertRaises(GitHubStateError):
            self.plan()
        writes = []
        Publisher(self.runner(), writes.append).drain("question/intent")
        self.assertEqual([op["kind"] for op in writes], ["forecast", "comment"])

    def test_resource_registration_not_committed_recovers_from_positive_shard(self):
        self.server.lose_root_ack = "before"
        with self.assertRaises(GitHubStateError):
            self.plan()
        writes = []
        Publisher(self.runner(), writes.append).drain("question/intent")
        self.assertEqual([op["kind"] for op in writes], ["forecast", "comment"])

    def test_manifest_cas_preserves_concurrent_registrations(self):
        second = self.runner()
        self.server.on_root_put = lambda: second.prepare("another/intent", "{}", [{"kind": "forecast"}])
        self.plan()
        manifest = json.loads(self.codec.decode(self.server.files[PATH]))
        self.assertEqual(set(manifest["resources"]), {"question", "another"})

    def test_question_checkpoint_does_not_rewrite_other_reports_or_manifest(self):
        store = self.plan()
        store.prepare("another/intent", "{}", [{"kind": "forecast"}])
        another = PATH + ".d/" + self.codec.resource_name("another") + ".enc"
        manifest_before, another_before = self.server.files[PATH], self.server.files[another]
        Publisher(store, lambda operation: None).drain("question/intent")
        self.assertEqual(self.server.files[PATH], manifest_before)
        self.assertEqual(self.server.files[another], another_before)
        self.assertNotIn(b"synthetic-report", self.codec.decode(self.server.files[PATH]))

    def test_create_race_preserves_first_frozen_report(self):
        second = self.runner()
        self.server.on_put = lambda: second.prepare("question/intent", '{"winner":true}', [{"kind": "forecast"}])
        saved, created = self.runner().prepare("question/intent", '{"loser":true}', [{"kind": "forecast"}])
        self.assertEqual(saved, '{"winner":true}')
        self.assertFalse(created)
        self.assertEqual(second.report("question/intent"), '{"winner":true}')

    def test_copying_another_questions_ciphertext_is_rejected(self):
        store = self.plan()
        question_path = self.server.focus
        store.prepare("another/intent", "{}", [{"kind": "forecast"}])
        self.server.files[question_path] = self.server.content
        with self.assertRaises(GitHubStateError):
            Publisher(self.runner(), lambda operation: self.fail("No write is allowed")).drain("question/intent")

    def test_authenticated_malformed_manifest_is_not_an_empty_index(self):
        self.server.files[PATH] = self.codec.encode(b'{"version":1,"resources":"invalid"}')
        with self.assertRaises(GitHubStateError):
            self.plan()
        self.assertTrue(all(method == "GET" for method, _ in self.server.calls))

    def test_independent_runners_restore_confirmed_steps_without_local_files(self):
        first = self.plan()
        writes = []
        Publisher(first, writes.append).drain("question/intent")
        second = self.runner()
        self.assertFalse(hasattr(first, "path"))
        self.assertFalse(hasattr(second, "path"))
        Publisher(second, writes.append).drain("question/intent")
        self.assertEqual([op["kind"] for op in writes], ["forecast", "comment"])
        self.assertEqual(second.report("question/intent"), '{"private":"synthetic-report"}')

    def test_competing_runner_wins_cas_and_loser_does_not_duplicate(self):
        first = self.plan()
        second = self.runner()
        writes = []
        # Both saw an unclaimed snapshot; only the conditional update may win.
        self.server.on_put = lambda: Publisher(second, writes.append).drain("question/intent")
        Publisher(first, writes.append).drain("question/intent")
        self.assertEqual([op["kind"] for op in writes], ["forecast", "comment"])

    def test_stale_read_cannot_overwrite_a_claim_from_another_runner(self):
        first = self.plan()
        old = self.server.content
        self.assertTrue(first.claim("question/intent", 0))
        claimed = self.server.content
        self.server.stale_reads = [old, old, old, old]  # steps() + three rejected claim attempts
        writes = []
        with self.assertRaises(GitHubStateError):
            Publisher(self.runner(), writes.append).drain("question/intent")
        self.assertEqual(writes, [])
        self.assertEqual(self.server.content, claimed)

    def test_claim_ack_lost_after_commit_never_authorizes_metaculus_post(self):
        first = self.plan()
        self.server.lose_ack = "after"
        writes = []
        with self.assertRaises(GitHubStateError):
            Publisher(first, writes.append).drain("question/intent")
        with self.assertRaises(PublicationUncertain):
            Publisher(self.runner(), writes.append).drain("question/intent")
        self.assertEqual(writes, [])
        self.assertEqual(self.runner().steps("question/intent")[0]["state"], "sending")

    def test_claim_not_committed_can_be_attempted_by_fresh_runner(self):
        first = self.plan()
        self.server.lose_ack = "before"
        writes = []
        with self.assertRaises(GitHubStateError):
            Publisher(first, writes.append).drain("question/intent")
        Publisher(self.runner(), writes.append).drain("question/intent")
        self.assertEqual([op["kind"] for op in writes], ["forecast", "comment"])

    def test_prepare_ack_lost_preserves_frozen_plan_and_safe_resume(self):
        self.server.lose_ack = "after"
        with self.assertRaises(GitHubStateError):
            self.plan()
        writes = []
        Publisher(self.runner(), writes.append).drain("question/intent")
        self.assertEqual([op["kind"] for op in writes], ["forecast", "comment"])

    def test_confirm_ack_lost_after_commit_is_restored_without_reposting(self):
        store = self.plan()
        writes = []

        def accepted(operation):
            writes.append(operation)
            self.server.lose_ack = "after"  # Lose GitHub acknowledgement, not Metaculus.

        with self.assertRaises(GitHubStateError):
            Publisher(store, accepted).drain("question/intent")
        Publisher(self.runner(), writes.append).drain("question/intent")
        self.assertEqual([op["kind"] for op in writes], ["forecast", "comment"])

    def test_confirm_not_committed_keeps_accepted_write_blocked(self):
        store = self.plan()
        writes = []

        def accepted(operation):
            writes.append(operation)
            self.server.lose_ack = "before"

        with self.assertRaises(GitHubStateError):
            Publisher(store, accepted).drain("question/intent")
        with self.assertRaises(PublicationUncertain):
            Publisher(self.runner(), writes.append).drain("question/intent")
        self.assertEqual([op["kind"] for op in writes], ["forecast"])

    def test_metaculus_accepted_then_timeout_stays_unknown_across_runners(self):
        store = self.plan()
        writes = []

        def lost_ack(operation):
            writes.append(operation)
            raise requests.Timeout("Synthetic Metaculus acknowledgement lost")

        with self.assertRaises(PublicationUncertain):
            Publisher(store, lost_ack).drain("question/intent")
        with self.assertRaises(PublicationUncertain):
            Publisher(self.runner(), writes.append).drain("question/intent")
        self.assertEqual(len(writes), 1)

    def test_explicit_reconciliation_is_durable_and_hash_guarded_across_runners(self):
        first = self.plan()
        self.assertTrue(first.claim("question/intent", 0))
        second = self.runner()
        with self.assertRaises(ValueError):
            second.reconcile("question/intent", 0, "wrong-hash", accepted=True)
        step = second.steps("question/intent")[0]
        # Represents operator verification, not an automatic inference from API history.
        second.reconcile("question/intent", 0, step["operation_hash"], accepted=True)
        writes = []
        Publisher(self.runner(), writes.append).drain("question/intent")
        self.assertEqual([op["kind"] for op in writes], ["comment"])

    def test_missing_or_inaccessible_journal_never_becomes_empty(self):
        for status in (404, 403, 429, 500):
            with self.subTest(status=status):
                self.server.status = status
                writes = []
                with self.assertRaises(GitHubStateError):
                    Publisher(self.runner(), writes.append).drain("question/intent")
                self.assertFalse(writes)
        self.assertTrue(all(method == "GET" for method, _ in self.server.calls))

    def test_changed_key_or_scope_blocks_before_any_write(self):
        for codec in (JournalCodec("DIFFERENT-OFFLINE-NOT-A-REAL-TOKEN-0123456789", CONTEXT),
                      JournalCodec(METACULUS_KEY, [*CONTEXT[:-1], "wrong-scope"])):
            with self.subTest(codec=codec):
                with self.assertRaises(GitHubStateError):
                    self.plan(self.runner(codec=codec))
        self.assertTrue(all(method == "GET" for method, _ in self.server.calls))

    def test_remote_state_exposes_neither_report_text_nor_key(self):
        self.plan()
        self.assertNotIn(b"synthetic-report", self.server.content)
        self.assertNotIn(METACULUS_KEY.encode(), self.server.content)
        self.assertTrue(self.codec.decode(self.server.content, "question").startswith(b"SQLite format 3\0"))
        damaged = bytearray(self.server.content)
        data = json.loads(damaged)
        sealed = bytearray(base64.b64decode(data["data"]))
        sealed[-1] ^= 1
        data["data"] = base64.b64encode(sealed).decode()
        with self.assertRaises(GitHubStateError):
            self.codec.decode(json.dumps(data).encode(), "question")

    def test_size_limit_stops_before_remote_commit(self):
        from unittest.mock import patch
        original = self.server.content
        with patch("github_publication.MAX_FILE", len(original) + 30):
            # Incompressible bytes exceed the encrypted inline-file budget.
            with self.assertRaises(GitHubStateError):
                self.codec.encode(os.urandom(20_000))
        self.assertEqual(self.server.content, original)
        self.assertEqual(self.server.calls, [])


class RemotePipelineTests(GitHubTestCase):
    async def test_complete_forecast_encrypted_checkpoint_then_fresh_runner_restore(self):
        """Model -> fake Metaculus -> encrypted fake GitHub ACK -> cold restore.

        There are no real credentials, HTTP requests or model invocations.
        The second runner shares ONLY the fake remote GitHub and Metaculus,
        never the first runner's memory or local publication database.
        """
        model_calls = self.model()
        sink = LocalMetaculus()
        q = question("japanese_diet")
        first = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        first.publication_store = self.runner()
        report = await first.forecast_question(q)
        key = first._publication_key(q)
        self.assertEqual([step["state"] for step in self.runner().steps(key)],
                         ["confirmed", "confirmed"])
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))
        self.assertGreater(len(model_calls), 0)
        model_count = len(model_calls)

        # The actual (synthetic) report must be encrypted at rest in every
        # fake remote GitHub file, including per-question shards.
        for encrypted in self.server.files.values():
            self.assertNotIn(report.explanation.encode(), encrypted)
            self.assertNotIn(METACULUS_KEY.encode(), encrypted)

        second = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        second.publication_store = self.runner()
        self.assertIsNot(first.publication_store, second.publication_store)
        self.assertFalse(hasattr(second.publication_store, "path"))
        recovered = await second.forecast_question(q)
        self.assertEqual(recovered.model_dump(), report.model_dump())
        self.assertEqual(len(model_calls), model_count,
                         "Confirmed plan must not trigger another paid-model simulation")
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1),
                         "Confirmed forecast and comment must never be re-POSTed")
        self.assertEqual([step["state"] for step in self.runner().steps(key)],
                         ["confirmed", "confirmed"])

    async def test_lost_github_confirm_ack_after_forecast_acceptance_recovers_comment_only(self):
        """GitHub persisted the confirmed forecast but its ACK was lost.

        An independent runner must detect that checkpoint and finish ONLY the
        remaining comment, with zero duplicate forecasts or model work.
        """
        model_calls = self.model()
        class AcceptedForecastLostGitHubAck(LocalMetaculus):
            def post_binary_question_prediction(inner, question_id, prediction):
                super().post_binary_question_prediction(question_id, prediction)
                self.server.lose_ack = "after"  # Next GitHub confirmation PUT

        sink = AcceptedForecastLostGitHubAck()
        q = question("japanese_diet")
        first = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        first.publication_store = self.runner()
        result = await first.forecast_questions([q], return_exceptions=True)
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], Exception)
        key = first._publication_key(q)
        self.assertEqual([step["state"] for step in self.runner().steps(key)],
                         ["confirmed", "ready"])
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 0))
        count = len(model_calls)

        second = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        second.publication_store = self.runner()
        restored = await second.forecast_question(q)
        self.assertIsNotNone(restored)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))
        self.assertEqual(len(model_calls), count)
        self.assertEqual([step["state"] for step in self.runner().steps(key)],
                         ["confirmed", "confirmed"])

    async def test_tournament_only_recovers_comment_from_previous_cup_intent(self):
        """Release gate: skip=True must remain true across intent recovery.

        Cup/test intentionally reforecast, but repairing their rejected comment
        in tournament mode is not permission to reforecast an already-seen
        question. Independent runners share only the simulated GitHub service.
        """
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
        cup = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink,
                       skip_previously_forecasted_questions=False)
        cup.publication_store = self.runner()
        cup.publication_intent = "offline-prior-cup-run"
        result = await cup.forecast_questions([q], return_exceptions=True)
        self.assertIsInstance(result[0], Exception)
        self.assertEqual([step["state"] for step in cup._publication_store.steps(cup._publication_key(q))],
                         ["confirmed", "rejected"])
        count = len(calls)

        tournament = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        tournament.publication_store = self.runner()
        await tournament.forecast_questions([q.model_copy(update={"already_forecasted": True})])
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1),
                         "Tournament must recover only the prior comment; a new initial intent duplicates the forecast")
        self.assertEqual(len(calls), count, "Comment recovery must not create new model forecasts")

    async def test_rejected_comment_recovers_on_runner_without_original_disk(self):
        class RejectOnce(LocalMetaculus):
            attempts = 0

            def post_question_comment(self, post_id, text, **kwargs):
                self.attempts += 1
                if self.attempts == 1:
                    raise PublicationRejected(403)
                return super().post_question_comment(post_id, text, **kwargs)

        calls = self.model()
        sink = RejectOnce()
        first = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        first.publication_store = self.runner()
        q = question("japanese_diet")
        self.assertIsInstance((await first.forecast_questions([q], return_exceptions=True))[0], Exception)
        count = len(calls)
        second = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        second.publication_store = self.runner()
        await second.forecast_questions([q.model_copy(update={"already_forecasted": True})])
        self.assertEqual(len(calls), count)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))

    async def test_matching_or_absent_history_is_not_proof_for_unknown_operation(self):
        class LostAck(LocalMetaculus):
            def post_binary_question_prediction(self, question_id, prediction):
                super().post_binary_question_prediction(question_id, prediction)
                raise requests.Timeout("Metaculus acknowledgement lost")

        self.model()
        sink = LostAck()
        q = question("japanese_diet")
        first = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        first.publication_store = self.runner()
        await first.forecast_questions([q], return_exceptions=True)
        for history, latest in (([{"forecast_values": [.35, .65]}], {"forecast_values": [.35, .65]}), ([], None)):
            evidence = q.model_copy(update={"already_forecasted": bool(history), "api_json": {
                "question": {"my_forecasts": {"latest": latest, "history": history}}
            }})
            second = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
            second.publication_store = self.runner()
            self.assertIsInstance((await second.forecast_questions([evidence], return_exceptions=True))[0], Exception)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 0))

    async def test_matching_comment_text_does_not_automatically_clear_unknown_receipt(self):
        class LostAck(LocalMetaculus):
            def post_question_comment(self, post_id, text, **kwargs):
                super().post_question_comment(post_id, text, **kwargs)
                raise requests.Timeout("Metaculus comment acknowledgement lost")

        self.model()
        sink = LostAck()
        q = question("japanese_diet")
        first = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
        first.publication_store = self.runner()
        await first.forecast_questions([q], return_exceptions=True)
        for comments in ([], [{"text": sink.comments[0][1]}]):
            second = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink)
            second.publication_store = self.runner()
            evidence = q.model_copy(update={"already_forecasted": True, "api_json": {"comments": comments}})
            self.assertIsInstance((await second.forecast_questions([evidence], return_exceptions=True))[0], Exception)
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (1, 1))

    async def test_github_rerun_attempt_reuses_intent_but_new_run_can_reforecast(self):
        self.model()
        sink = LocalMetaculus()
        for run_id, attempt in (("100", "1"), ("100", "2"), ("101", "1")):
            bot = self.bot(publish_reports_to_metaculus=True, metaculus_client=sink,
                           skip_previously_forecasted_questions=False)
            bot.publication_store = self.runner()
            with patch.dict(os.environ, {"GITHUB_RUN_ID": run_id, "GITHUB_RUN_ATTEMPT": attempt,
                                        "METACULUS_PUBLICATION_INTENT": ""}):
                await bot.forecast_question(question("japanese_diet"))
        self.assertEqual((len(sink.forecasts), len(sink.comments)), (2, 2))

    async def test_actions_cannot_silently_fall_back_to_an_ephemeral_local_db(self):
        bot = self.bot(publish_reports_to_metaculus=True)
        del bot.publication_store
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "METACULUS_PUBLICATION_BACKEND": "local"}):
            with self.assertRaises(RuntimeError):
                await bot.forecast_questions([question("japanese_diet")])

    async def test_empty_question_batch_still_checks_journal_readiness(self):
        bot = self.bot(publish_reports_to_metaculus=True)
        bot.publication_store = self.runner()
        self.server.status = 404
        with self.assertRaises(GitHubStateError):
            await bot.forecast_questions([])


class ScriptedSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected extra GitHub request")
        return self.responses.pop(0)


class GitHubProtocolTests(OfflineTestCase):
    def client(self, responses):
        session = ScriptedSession(responses)
        return GitHubContents(REPOSITORY, BRANCH, PATH, GITHUB_KEY, session=session), session

    def test_conditional_put_binds_exact_blob_sha_and_branch(self):
        content = b"encrypted fixture"
        client, session = self.client([Response(200, {"content": {"sha": git_blob_sha(content)}, "commit": {"sha": "d" * 40}})])
        client.compare_and_swap("a" * 40, content)
        method, url, request = session.calls[0]
        self.assertEqual(method, "PUT")
        self.assertEqual(url, f"https://api.github.com/repos/{REPOSITORY}/contents/{PATH}")
        self.assertEqual(request["json"]["sha"], "a" * 40)
        self.assertEqual(request["json"]["branch"], BRANCH)
        self.assertEqual(base64.b64decode(request["json"]["content"]), content)
        self.assertFalse(request["allow_redirects"])
        self.assertEqual(request["timeout"], (10, 30))

    def test_wrong_blob_acknowledgement_is_not_success(self):
        client, _ = self.client([Response(200, {"content": {"sha": "a" * 40}, "commit": {"sha": "b" * 40}})])
        with self.assertRaises(GitHubStateError):
            client.compare_and_swap("a" * 40, b"a different encrypted checkpoint")

    def test_create_response_does_not_acknowledge_a_conditional_update(self):
        content = b"encrypted fixture"
        client, _ = self.client([Response(201, {"content": {"sha": git_blob_sha(content)}, "commit": {"sha": "b" * 40}})])
        with self.assertRaises(GitHubStateError):
            client.compare_and_swap("a" * 40, content)

    def test_non_object_json_acknowledgement_is_rejected(self):
        client, _ = self.client([Response(200, [])])
        with self.assertRaises(GitHubStateError):
            client.compare_and_swap("a" * 40, b"encrypted fixture")

    def test_corrupt_git_blob_read_is_rejected_before_decryption(self):
        data = {"type": "file", "path": PATH, "encoding": "base64", "size": 1,
                "sha": "0" * 40, "content": base64.b64encode(b"x").decode()}
        client, _ = self.client([Response(200, data)])
        with self.assertRaises(GitHubStateError):
            client.read()

    def test_explicit_conflict_is_distinct_from_unavailable_state(self):
        client, _ = self.client([Response(409, {})])
        with self.assertRaises(StateConflict):
            client.compare_and_swap("a" * 40, b"fixture")

    def test_initialize_existing_or_unreadable_ref_never_overwrites(self):
        for status in (200, 403, 500):
            client, session = self.client([Response(status, {})])
            with self.assertRaises(GitHubStateError):
                client.initialize(b"encrypted fixture", "a" * 40)
            self.assertEqual([method for method, _, _ in session.calls], ["GET"])

    def test_initialize_creates_only_a_new_orphan_state_ref(self):
        client, session = self.client([
            Response(404, {}), Response(201, {"sha": "b" * 40}),
            Response(201, {"sha": "c" * 40}), Response(201, {"sha": "d" * 40}),
            Response(201, {"object": {"sha": "d" * 40}}),
        ])
        client.initialize(b"encrypted fixture", "a" * 40)
        self.assertEqual([method for method, _, _ in session.calls], ["GET", "POST", "POST", "POST", "POST"])
        tree = session.calls[2][2]["json"]
        self.assertEqual(tree["tree"], [{"path": PATH, "mode": "100644", "type": "blob", "sha": "b" * 40}])
        self.assertEqual(session.calls[3][2]["json"]["parents"], [])
        self.assertEqual(session.calls[4][2]["json"], {"ref": "refs/heads/" + BRANCH, "sha": "d" * 40})
        self.assertFalse(any(method in ("PUT", "PATCH", "DELETE") for method, _, _ in session.calls))

    def test_initialization_conflict_does_not_reset_the_ref(self):
        client, session = self.client([
            Response(404, {}), Response(201, {"sha": "b" * 40}),
            Response(201, {"sha": "c" * 40}), Response(201, {"sha": "d" * 40}), Response(422, {}),
        ])
        with self.assertRaises(GitHubStateError):
            client.initialize(b"encrypted fixture", "a" * 40)
        self.assertEqual(len(session.calls), 5)
        self.assertFalse(any(method in ("PATCH", "DELETE") for method, _, _ in session.calls))

    def test_github_configuration_is_constructed_without_network_io(self):
        values = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_TOKEN": GITHUB_KEY,
                  "METACULUS_TOKEN": METACULUS_KEY, "METACULUS_PUBLICATION_SCOPE": SCOPE,
                  "METACULUS_PUBLICATION_STATE_BRANCH": BRANCH, "METACULUS_PUBLICATION_STATE_PATH": PATH}
        with patch.dict(os.environ, values):
            store = configured_store()
        self.assertIsInstance(store, GitHubPublicationStore)

    def test_codec_rejects_short_placeholder_keys(self):
        with self.assertRaises(GitHubStateError):
            JournalCodec("REPLACE_ME", CONTEXT)
    def test_legacy_frozen_initial_manifest_is_truthful(self):
        manifest = json.loads(initial_legacy_frozen_manifest())
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["resources"], [])
        self.assertEqual(manifest["legacy_cutover"]["mode"], "legacy-frozen-no-retries")
        self.assertEqual(manifest["legacy_cutover"]["historic_outcomes"], "not-fully-reconciled")
        self.assertEqual(manifest["legacy_cutover"]["disposition"], "abandoned-without-replay")
        self.assertNotIn("reconciled", str(manifest).replace("not-fully-reconciled", ""))

    def test_legacy_frozen_manifest_is_encrypted_before_git_ref_creation(self):
        codec = JournalCodec(METACULUS_KEY, CONTEXT)
        plaintext = initial_legacy_frozen_manifest()
        sealed = codec.encode(plaintext)
        self.assertNotIn(b"legacy-frozen-no-retries", sealed)
        self.assertEqual(json.loads(codec.decode(sealed)), json.loads(plaintext))

    def test_legacy_freeze_initializer_rejects_missing_ack_without_store(self):
        with patch("sys.argv", ["github_publication", "--initialize"]):
            with patch("github_publication.configured_store", side_effect=AssertionError("No store permitted")):
                with self.assertRaises(SystemExit) as caught:
                    journal_initialization_main()
        self.assertEqual(caught.exception.code, 2)

    def test_legacy_freeze_initializer_rejects_obsolete_reconciled_flag_without_store(self):
        with patch("sys.argv", ["github_publication", "--initialize",
                                "--confirm-prior-runs-reconciled"]):
            with patch("github_publication.configured_store", side_effect=AssertionError("No store permitted")):
                with self.assertRaises(SystemExit) as caught:
                    journal_initialization_main()
        self.assertEqual(caught.exception.code, 2)

    def test_legacy_freeze_initializer_creates_only_new_encrypted_manifest(self):
        from unittest.mock import Mock
        codec = JournalCodec(METACULUS_KEY, CONTEXT)
        contents = Mock()
        test_store = Mock(contents=contents, codec=codec)
        source_sha = "a" * 40
        with patch("sys.argv", ["github_publication", "--initialize",
                                "--confirm-legacy-frozen-no-retries"]):
            with patch("github_publication.configured_store", return_value=test_store):
                with patch.dict(os.environ, {"GITHUB_SHA": source_sha}):
                    journal_initialization_main()
        contents.initialize.assert_called_once()
        sealed, used_sha = contents.initialize.call_args.args
        self.assertEqual(used_sha, source_sha)
        manifest = json.loads(codec.decode(sealed))
        self.assertEqual(manifest, json.loads(initial_legacy_frozen_manifest()))
        contents.read.assert_not_called()
        contents.create.assert_not_called()
        contents.compare_and_swap.assert_not_called()


"""Strictly offline first-live one-atomic-question selection and workflow gates."""
from pathlib import Path
from unittest.mock import patch

from .support import OfflineTestCase, app, question


ROOT = Path(__file__).resolve().parents[1]


class FirstLiveAtomicGuardTests(OfflineTestCase):
    def cases(self):
        base = question("japanese_diet")
        return (base.model_copy(update={"id_of_question": 70101, "id_of_post": 90101, "already_forecasted": False}),
                base.model_copy(update={"id_of_question": 70102, "id_of_post": 90102, "already_forecasted": False}))

    def test_exactly_one_selected_without_models_or_network(self):
        a, b = self.cases()
        self.assertEqual(app.select_first_live_atomic_question([a], [b], 90101), ([a], []))
        self.assertEqual(app.select_first_live_atomic_question([a], [b], 90102), ([], [b]))

    def test_manual_preflight_can_check_same_candidate_without_logging_its_id(self):
        import json
        from tools.preflight_tournament_inventory_readonly import _candidate_check
        a, b = self.cases()
        ready = _candidate_check([a], [b], 90101)
        self.assertTrue(ready["present_once_in_validated_open_inventory"])
        self.assertTrue(ready["binary_with_empty_token_forecast_history_in_snapshot"])
        self.assertEqual(ready["observed_question_type"], "binary")
        self.assertTrue(ready["single_supported_atomic_unforecasted_in_token_snapshot"])
        self.assertFalse(ready["historical_outcomes_fully_reconciled"])
        self.assertFalse(ready["live_publication_authorized"])
        self.assertNotIn("90101", json.dumps(ready))
        prior = a.model_copy(update={"already_forecasted": True})
        self.assertFalse(_candidate_check([prior], [b], 90101)["binary_with_empty_token_forecast_history_in_snapshot"])
        self.assertFalse(_candidate_check([a], [a], 90101)["present_once_in_validated_open_inventory"])
        self.assertFalse(_candidate_check([a], [b], 79999)["present_once_in_validated_open_inventory"])

    def test_missing_duplicate_and_invalid_ids_fail_closed(self):
        a, b = self.cases()
        for value, first, second in (
            (90103, [a], [b]), (90101, [a], [a]), (True, [a], [b]),
            (0, [a], [b]), ("90101", [a], [b]),
        ):
            with self.subTest(value=value, count=len(first)+len(second)):
                with self.assertRaises(RuntimeError):
                    app.select_first_live_atomic_question(first, second, value)

    def test_previously_forecasted_or_malformed_type_never_selected(self):
        a, _ = self.cases()
        prior = a.model_copy(update={"already_forecasted": True})
        malformed = a.model_copy(update={"already_forecasted": None})
        different_type = a.model_copy(update={"question_type": "conditional"})
        for candidate in (prior, malformed, different_type):
            with self.subTest(candidate=candidate):
                with self.assertRaises(RuntimeError):
                    app.select_first_live_atomic_question([candidate], [], 90101)

    def test_all_five_supported_atomic_types_accepted_with_valid_source_metadata(self):
        # Synthetic post JSON is parsed by the PINNED SDK; no API, models, or POST.
        from forecasting_tools.data_models.data_organizer import DataOrganizer
        from .test_forecast_validation import api_post
        types = ("binary", "multiple_choice", "numeric", "discrete", "date")
        for index, kind in enumerate(types):
            with self.subTest(kind=kind):
                post_id = 93001 + index
                internal_id = 73001 + index
                q = DataOrganizer.get_question_from_post_json(
                    api_post(kind, qid=internal_id, post_id=post_id)
                )
                self.assertEqual(q.already_forecasted, False)
                left, right = app.select_first_live_atomic_question([q], [], post_id)
                self.assertEqual(left, [q])
                self.assertEqual(right, [])
                self.assertEqual(q.id_of_question, internal_id)
                self.assertEqual(q.id_of_post, post_id)
                from tools.preflight_tournament_inventory_readonly import _candidate_check
                status = _candidate_check([q], [], post_id)
                self.assertEqual(status["observed_question_type"], kind)
                self.assertTrue(status["single_supported_atomic_unforecasted_in_token_snapshot"])
                self.assertFalse(status["live_publication_authorized"])

    def test_unknown_type_and_invalid_question_shape_fail_before_models(self):
        from forecasting_tools.data_models.data_organizer import DataOrganizer
        from .test_forecast_validation import api_post
        from forecasting_tools import ConditionalQuestion
        original = DataOrganizer.get_question_from_post_json(api_post(
            "multiple_choice", qid=71020, post_id=91020
        ))
        invalid = [
            original.model_copy(update={"options": ["A", "A", "C"]}),
            original.model_copy(update={"id_of_question": 0}),
            original.model_copy(update={"question_type": "conditional"}),
            original.model_copy(update={"already_forecasted": True}),
            original.model_copy(update={"already_forecasted": None}),
        ]
        for q in invalid:
            with self.subTest(shape=q):
                with self.assertRaises(RuntimeError):
                    app.select_first_live_atomic_question([q], [], 91020)
        # A conditional pair can result in more than one outbound forecast.
        # The first-live selector rejects it even with just one post match.
        from .test_forecast_validation import conditional
        q = conditional()
        with self.assertRaises(RuntimeError):
            app.select_first_live_atomic_question([q], [], 201)

    def test_group_with_two_questions_same_public_post_is_ambiguous(self):
        a, b = self.cases()
        b = b.model_copy(update={"id_of_post": a.id_of_post})
        with self.assertRaisesRegex(RuntimeError, "not uniquely present"):
            app.select_first_live_atomic_question([a, b], [], a.id_of_post)

    async def test_two_scans_finish_before_single_forecast_and_others_untouched(self):
        a, b = self.cases()
        bot = self.bot()
        scanned, published_batches = [], []

        def scan(client, tournament, *, read_page):
            scanned.append(tournament)
            return [a] if len(scanned) == 1 else [b]

        async def forecast(questions, return_exceptions):
            self.assertEqual(scanned, [33121, app.MetaculusClient.CURRENT_MINIBENCH_ID])
            self.assertTrue(return_exceptions)
            published_batches.append(list(questions))
            return list(questions)

        self.start_patch(patch.object(app, "BoundedPostReader", lambda *_: object()))
        self.start_patch(patch.object(app, "scan_open_tournament", scan))
        self.start_patch(patch.object(app, "deduplicate_tournaments", lambda a, b: (a, b)))
        self.start_patch(patch.object(bot, "forecast_questions", forecast))
        # The production manual path now requires read-only donated funding
        # evidence and explicit bounded Perplexity approval BEFORE forecasting.
        verified = []
        def funded(key, *, expected_suffix):
            verified.append((key, expected_suffix))
            return {"remaining_usd": "34.29481"}
        self.start_patch(patch.object(app, "verify_sponsored_openrouter_credit", funded))
        self.start_patch(patch("perplexity_run_budget.PerplexityAutoRunBudget",
                               return_value=object()))
        self.start_patch(patch.dict("os.environ", {
            "OPENROUTER_API_KEY": "synthetic-donated-9fc",
            "METACULUS_SPONSORED_KEY_SUFFIX": "9fc",
            "PERPLEXITY_MANUAL_RESEARCH_APPROVAL": "APPROVE_FOUR_AGENT_REQUESTS",
        }))
        result = await app.forecast_for_mode(bot, "tournament", first_live_post_id=90102)
        self.assertEqual(result, [b])
        self.assertEqual(published_batches, [[b]])
        self.assertEqual(verified, [("synthetic-donated-9fc", "9fc")])
        self.assertIsNone(bot._automatic_perplexity_budget)

    async def test_supervised_missing_wallet_approval_stops_before_paid_forecast(self):
        from perplexity_run_budget import PerplexityBudgetUnavailable
        a, _ = self.cases()
        bot = self.bot()
        scans = []
        verified = []

        def scan(client, tournament, *, read_page):
            scans.append(tournament)
            return [a] if tournament == 33121 else []

        def funded(key, *, expected_suffix):
            verified.append((key, expected_suffix))
            return {"remaining_usd": "34.29481"}

        async def forbidden(*args, **kwargs):
            raise AssertionError("Missing operator wallet approval must not run models")

        self.start_patch(patch.object(app, "BoundedPostReader", lambda *_: object()))
        self.start_patch(patch.object(app, "scan_open_tournament", scan))
        self.start_patch(patch.object(app, "deduplicate_tournaments", lambda a, b: (a, b)))
        self.start_patch(patch.object(app, "verify_sponsored_openrouter_credit", funded))
        self.start_patch(patch.object(bot, "forecast_questions", forbidden))
        self.start_patch(patch.dict("os.environ", {
            "OPENROUTER_API_KEY": "synthetic-donated-9fc",
            "METACULUS_SPONSORED_KEY_SUFFIX": "9fc",
            "PERPLEXITY_MANUAL_RESEARCH_APPROVAL": "DO_NOT_RUN",
            "PERPLEXITY_VERIFIED_BALANCE_USD": "",
            "PERPLEXITY_VERIFIED_AT_UTC": "",
            "PERPLEXITY_MAX_AUTO_RUN_USD": "",
            "PERPLEXITY_AUTO_RECHARGE_CONFIRMED_OFF": "NO",
        }))
        with self.assertRaises(PerplexityBudgetUnavailable):
            await app.forecast_for_mode(bot, "tournament", first_live_post_id=90101)
        self.assertEqual(scans, [33121, app.MetaculusClient.CURRENT_MINIBENCH_ID])
        self.assertEqual(verified, [("synthetic-donated-9fc", "9fc")])

    async def test_missing_first_live_id_blocks_all_forecasts(self):
        a, b = self.cases()
        bot = self.bot()
        scans = []
        self.start_patch(patch.object(app, "BoundedPostReader", lambda *_: object()))

        def scan(client, tournament, *, read_page):
            scans.append(tournament)
            return [a] if len(scans) == 1 else [b]

        self.start_patch(patch.object(app, "scan_open_tournament", scan))
        self.start_patch(patch.object(app, "deduplicate_tournaments", lambda a, b: (a, b)))
        self.start_patch(patch.object(bot, "forecast_questions",
                                      side_effect=AssertionError("Forecast must remain blocked")))
        with self.assertRaisesRegex(RuntimeError, "not uniquely present"):
            await app.forecast_for_mode(bot, "tournament", first_live_post_id=90103)
        self.assertEqual(scans, [33121, app.MetaculusClient.CURRENT_MINIBENCH_ID])

    def test_draft_tournament_workflow_requires_id_and_prior_approval(self):
        source = (ROOT / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        self.assertIn("default: DO_NOT_RUN", source)
        self.assertIn("inputs.publication_approval == 'APPROVE_ONE_LIVE_PUBLICATION_RUN'", source)
        self.assertIn("inputs.first_live_post_id != 'DO_NOT_RUN'", source)
        self.assertIn("inputs.first_live_post_id != ''", source)
        self.assertIn("github.run_attempt == 1", source)
        self.assertIn('FIRST_LIVE_POST_ID: ${{ inputs.first_live_post_id }}', source)
        self.assertIn('--first-live-post-id "$FIRST_LIVE_POST_ID"', source)
        self.assertNotIn("\\n  schedule:", source)
        self.assertIn("inputs.research_approval == 'APPROVE_FOUR_AGENT_REQUESTS'", source)
        self.assertIn("inputs.perplexity_auto_recharge_off == 'YES'", source)
        self.assertIn("PERPLEXITY_MANUAL_RESEARCH_APPROVAL:", source)
        self.assertIn("PERPLEXITY_VERIFIED_BALANCE_USD:", source)
        self.assertIn("PERPLEXITY_VERIFIED_AT_UTC:", source)
        self.assertIn("PERPLEXITY_MAX_AUTO_RUN_USD:", source)
        self.assertIn("METACULUS_SPONSORED_KEY_SUFFIX:", source)


if __name__ == "__main__":
    import unittest
    unittest.main()

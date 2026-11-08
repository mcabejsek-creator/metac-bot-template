"""Strictly offline first-live one-binary-question selection and workflow gates."""
from pathlib import Path
from unittest.mock import patch

from .support import OfflineTestCase, app, question


ROOT = Path(__file__).resolve().parents[1]


class FirstLiveBinaryGuardTests(OfflineTestCase):
    def cases(self):
        base = question("japanese_diet")
        return (base.model_copy(update={"id_of_question": 70101, "already_forecasted": False}),
                base.model_copy(update={"id_of_question": 70102, "already_forecasted": False}))

    def test_exactly_one_selected_without_models_or_network(self):
        a, b = self.cases()
        self.assertEqual(app.select_first_live_binary_question([a], [b], 70101), ([a], []))
        self.assertEqual(app.select_first_live_binary_question([a], [b], 70102), ([], [b]))

    def test_missing_duplicate_and_invalid_ids_fail_closed(self):
        a, b = self.cases()
        for value, first, second in (
            (70103, [a], [b]), (70101, [a], [a]), (True, [a], [b]),
            (0, [a], [b]), ("70101", [a], [b]),
        ):
            with self.subTest(value=value, count=len(first)+len(second)):
                with self.assertRaises(RuntimeError):
                    app.select_first_live_binary_question(first, second, value)

    def test_previously_forecasted_or_nonbinary_never_selected(self):
        a, _ = self.cases()
        prior = a.model_copy(update={"already_forecasted": True})
        malformed = a.model_copy(update={"already_forecasted": None})
        different_type = a.model_copy(update={"question_type": "conditional"})
        for candidate in (prior, malformed, different_type):
            with self.subTest(candidate=candidate):
                with self.assertRaises(RuntimeError):
                    app.select_first_live_binary_question([candidate], [], 70101)

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
        result = await app.forecast_for_mode(bot, "tournament", first_live_binary_id=70102)
        self.assertEqual(result, [b])
        self.assertEqual(published_batches, [[b]])

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
            await app.forecast_for_mode(bot, "tournament", first_live_binary_id=70103)
        self.assertEqual(scans, [33121, app.MetaculusClient.CURRENT_MINIBENCH_ID])

    def test_draft_tournament_workflow_requires_id_and_prior_approval(self):
        source = (ROOT / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        self.assertIn("default: DO_NOT_RUN", source)
        self.assertIn("inputs.publication_approval == 'APPROVE_ONE_LIVE_PUBLICATION_RUN'", source)
        self.assertIn("inputs.first_live_binary_id != 'DO_NOT_RUN'", source)
        self.assertIn("inputs.first_live_binary_id != ''", source)
        self.assertIn("github.run_attempt == 1", source)
        self.assertIn('FIRST_LIVE_BINARY_ID: ${{ inputs.first_live_binary_id }}', source)
        self.assertIn('--first-live-binary-id "$FIRST_LIVE_BINARY_ID"', source)
        self.assertNotIn("\\n  schedule:", source)


if __name__ == "__main__":
    import unittest
    unittest.main()

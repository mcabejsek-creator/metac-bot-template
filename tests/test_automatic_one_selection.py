"""Offline-only automatic candidate selection; never runs live Metaculus/models."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from forecasting_tools.data_models.data_organizer import DataOrganizer

from .support import OfflineTestCase, app
from .test_forecast_validation import api_post
from automatic_one_selection import (
    MINIMUM_REMAINING, UnsafeAutomaticInventory,
    select_one_automatic_candidate,
)


NOW = datetime(2026, 11, 8, 11, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def atomic(kind="binary", *, post_id=93001, qid=73001, hours_left=5, prior=False):
    question = DataOrganizer.get_question_from_post_json(
        api_post(kind, qid=qid, post_id=post_id)
    )
    return question.model_copy(update={
        "open_time": NOW - timedelta(days=2),
        "close_time": NOW + timedelta(hours=hours_left),
        "state": "open",
        "already_forecasted": prior,
    })


class AutomaticOneSelectionTests(OfflineTestCase):
    def test_five_valid_individual_types_select_exactly_one(self):
        for i, kind in enumerate(("binary", "multiple_choice", "numeric", "discrete", "date")):
            with self.subTest(kind=kind):
                q = atomic(kind, post_id=94000+i, qid=74000+i)
                self.assertEqual(select_one_automatic_candidate([q], [], now=NOW), ([q], []))

    def test_closing_soonest_first_and_deterministic_tiebreaks(self):
        late = atomic(post_id=901, qid=101, hours_left=12)
        early = atomic(post_id=902, qid=102, hours_left=4)
        mini = atomic(post_id=903, qid=103, hours_left=3)
        self.assertEqual(select_one_automatic_candidate([late, early], [mini], now=NOW), ([], [mini]))
        equal = mini.model_copy(update={"close_time": early.close_time})
        self.assertEqual(select_one_automatic_candidate([late, early], [equal], now=NOW), ([early], []))
        earlier_public_id = atomic(post_id=899, qid=105, hours_left=4)
        self.assertEqual(select_one_automatic_candidate([early, earlier_public_id], [], now=NOW), ([earlier_public_id], []))

    def test_too_close_and_already_forecasted_are_skipped_without_any_work(self):
        urgent = atomic(post_id=931, qid=731, hours_left=1)
        prior = atomic(post_id=932, qid=732, prior=True)
        self.assertEqual(select_one_automatic_candidate([urgent], [prior], now=NOW), ([], []))
        at_margin = atomic(post_id=933, qid=733, hours_left=2)
        self.assertEqual(MINIMUM_REMAINING, timedelta(hours=2))
        self.assertEqual(select_one_automatic_candidate([at_margin], [], now=NOW), ([at_margin], []))

    def test_grouped_post_conditional_and_unforecasted_post_ambiguity_skipped(self):
        group = atomic(post_id=941, qid=741)
        group = group.model_copy(update={"group_question_option": "group-member"})
        safe = atomic(post_id=944, qid=744, hours_left=8)
        conditional_marked = atomic(post_id=942, qid=742)
        conditional_marked = conditional_marked.model_copy(update={"question_type": "conditional"})
        doubled = atomic(post_id=943, qid=743)
        other_member = atomic(post_id=943, qid=745)
        # Even a single group member or conditional-looking atomic leaf is ineligible.
        self.assertEqual(select_one_automatic_candidate(
            [group, conditional_marked, doubled, other_member, safe], [], now=NOW
        ), ([safe], []))

    def test_missing_histories_bad_ids_and_duplicate_question_ids_fail_closed(self):
        good = atomic(post_id=951, qid=751)
        variants = (
            good.model_copy(update={"already_forecasted": None}),
            good.model_copy(update={"id_of_post": None}),
            good.model_copy(update={"id_of_post": True}),
            good.model_copy(update={"id_of_question": -1}),
        )
        for bad in variants:
            with self.subTest(bad=bad), self.assertRaises(UnsafeAutomaticInventory):
                select_one_automatic_candidate([bad], [], now=NOW)
        repeated = good.model_copy(update={"id_of_post": 952})
        with self.assertRaises(UnsafeAutomaticInventory):
            select_one_automatic_candidate([good], [repeated], now=NOW)
        with self.assertRaises(UnsafeAutomaticInventory):
            select_one_automatic_candidate(None, [], now=NOW)

    def test_reject_invalid_source_distribution_before_paid_models(self):
        invalid = atomic("multiple_choice", post_id=961, qid=761)
        invalid = invalid.model_copy(update={"options": ["A", "A", "C"]})
        with self.assertRaises(UnsafeAutomaticInventory):
            select_one_automatic_candidate([invalid], [], now=NOW)

    async def test_integration_scans_both_tournaments_before_one_forecast(self):
        early = atomic(post_id=971, qid=771, hours_left=4)
        later = atomic(post_id=972, qid=772, hours_left=6)
        bot = self.bot()
        scanned, submitted = [], []

        def scan(client, tournament, *, read_page):
            scanned.append(tournament)
            return [later] if len(scanned) == 1 else [early]

        async def forecast(questions, return_exceptions):
            self.assertEqual(scanned, [33121, app.MetaculusClient.CURRENT_MINIBENCH_ID])
            self.assertEqual(len(questions), 1)
            submitted.extend(questions)
            return list(questions)

        self.start_patch(patch.object(app, "BoundedPostReader", lambda *_: object()))
        self.start_patch(patch.object(app, "scan_open_tournament", scan))
        self.start_patch(patch.object(app, "deduplicate_tournaments", lambda a, b: (a, b)))
        self.start_patch(patch.object(app, "select_one_automatic_candidate",
                                      lambda a, b: select_one_automatic_candidate(a, b, now=NOW)))
        self.start_patch(patch.object(bot, "forecast_questions", forecast))
        result = await app.forecast_for_mode(bot, "tournament", automatic_one=True)
        self.assertEqual(result, [early])
        self.assertEqual(submitted, [early])

    def test_no_production_trigger_or_cost_unverified_activation(self):
        main_source = (ROOT / "main.py").read_text()
        workflow_source = (ROOT / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        self.assertNotIn("parser.add_argument(\"--automatic-one\"", main_source)
        self.assertNotIn("--automatic-one", workflow_source)
        self.assertNotIn("\\n  schedule:", workflow_source)
        self.assertIn("default: DO_NOT_RUN", workflow_source)


if __name__ == "__main__":
    import unittest
    unittest.main()

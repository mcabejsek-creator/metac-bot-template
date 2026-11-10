"""Offline-only automatic candidate selection; never runs live Metaculus/models."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
    def fixed_search_client(self, *, blocked=()):
        client = SimpleNamespace(
            preflight=Mock(return_value={"blocked_post_ids": sorted(blocked),
                                         "remaining_usd": "5.00",
                                         "reserved_last_seven_days_usd": "0.00",
                                         "weekly_target_usd": "5.00"}),
            blocked_post_ids=Mock(return_value=set(blocked)),
            remaining_usd=Mock(return_value=Decimal("5.00")),
        )
        self.start_patch(patch(
            "fixed_search_research.FixedSearchClient.from_environment",
            return_value=client,
        ))
        return client

    def test_five_valid_individual_types_select_exactly_one(self):
        for i, kind in enumerate(("binary", "multiple_choice", "numeric", "discrete", "date")):
            with self.subTest(kind=kind):
                q = atomic(kind, post_id=94000+i, qid=74000+i)
                self.assertEqual(select_one_automatic_candidate([q], [], now=NOW), ([q], []))

    def test_old_still_open_question_is_not_excluded_by_publication_age(self):
        old = atomic(post_id=94011, qid=74011, hours_left=3).model_copy(
            update={"open_time": NOW - timedelta(days=45)},
        )
        new = atomic(post_id=94012, qid=74012, hours_left=12).model_copy(
            update={"open_time": NOW - timedelta(minutes=5)},
        )
        self.assertEqual(select_one_automatic_candidate([old, new], [], now=NOW), ([old], []))

    def test_atomic_null_variant_placeholders_are_not_group_or_notebook(self):
        candidate = atomic(post_id=94013, qid=74013)
        source = dict(candidate.api_json,
                      notebook=None, group_of_questions=None, conditional=None)
        candidate = candidate.model_copy(update={"api_json": source})
        self.assertEqual(select_one_automatic_candidate([candidate], [], now=NOW),
                         ([candidate], []))
        for variant in ("notebook", "group_of_questions", "conditional"):
            marked = candidate.model_copy(update={
                "api_json": dict(source, **{variant: {}}),
            })
            with self.subTest(variant=variant):
                self.assertEqual(select_one_automatic_candidate([marked], [], now=NOW),
                                 ([], []))

    def test_automatic_bot_uses_fixed_search_with_one_report_and_three_forecasts(self):
        bot = app.build_template_bot(automatic_one=True)
        self.assertEqual(bot.get_llm("default", "llm").model, "openrouter/openai/gpt-4.1")
        self.assertEqual(bot.research_reports_per_question, 1)
        self.assertEqual(bot.predictions_per_research_report, 3)
        self.assertEqual(bot.get_llm("researcher"), "perplexity-search")
        self.assertTrue(bot.publish_reports_to_metaculus)
        self.assertTrue(bot.skip_previously_forecasted_questions)
        manual = app.build_template_bot(automatic_one=False)
        self.assertEqual(manual.get_llm("default", "llm").model, "openrouter/openai/gpt-4o")
        self.assertEqual(manual.research_reports_per_question, 2)
        self.assertEqual(manual.get_llm("researcher"), "perplexity-agent")

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
        at_margin = atomic(post_id=933, qid=733, hours_left=1.5)
        self.assertEqual(MINIMUM_REMAINING, timedelta(minutes=90))
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
        search_client = self.fixed_search_client()
        scanned, submitted = [], []

        def scan(client, tournament, *, read_page):
            scanned.append(tournament)
            return [later] if len(scanned) == 1 else [early]

        async def forecast(questions, return_exceptions):
            self.assertEqual(scanned, [33121, app.MetaculusClient.CURRENT_MINIBENCH_ID])
            self.assertEqual(len(questions), 1)
            self.assertIs(bot._fixed_search_client, search_client)
            submitted.extend(questions)
            return list(questions)

        self.start_patch(patch.object(app, "BoundedPostReader", lambda *_: object()))
        self.start_patch(patch.object(app, "scan_open_tournament", scan))
        self.start_patch(patch.object(app, "deduplicate_tournaments", lambda a, b: (a, b)))
        self.start_patch(patch.object(app, "select_one_automatic_candidate",
                                      lambda a, b: select_one_automatic_candidate(a, b, now=NOW)))
        self.start_patch(patch.object(bot, "forecast_questions", forecast))
        self.start_patch(patch.dict("os.environ", {
            "OPENROUTER_API_KEY": "sk-or-v1-test-key-9fc",
            "METACULUS_SPONSORED_KEY_SUFFIX": "9fc",
        }))
        verified = []
        def fake_credit_check(key, *, expected_suffix):
            verified.append((key.endswith("9fc"), expected_suffix))
            return {"remaining_usd": "34.29481"}
        self.start_patch(patch.object(app, "verify_sponsored_openrouter_credit", fake_credit_check))
        result = await app.forecast_for_mode(bot, "tournament", automatic_one=True)
        self.assertIsNone(bot._fixed_search_client)
        self.assertEqual(verified, [(True, "9fc")])
        self.assertEqual(result, [early])
        self.assertEqual(submitted, [early])

    async def test_no_candidate_calls_no_credit_api_or_model(self):
        bot = self.bot()
        self.fixed_search_client()
        scanned = []
        def scan(client, tournament, *, read_page):
            scanned.append(tournament)
            return []
        def forbidden_credit(*args, **kwargs):
            raise AssertionError("No candidate must never query credits")
        async def forbidden_forecast(*args, **kwargs):
            raise AssertionError("No candidate must never call a paid model")
        self.start_patch(patch.object(app, "BoundedPostReader", lambda *_: object()))
        self.start_patch(patch.object(app, "scan_open_tournament", scan))
        self.start_patch(patch.object(app, "deduplicate_tournaments", lambda a, b: (a, b)))
        self.start_patch(patch.object(app, "verify_sponsored_openrouter_credit", forbidden_credit))
        self.start_patch(patch.object(bot, "forecast_questions", forbidden_forecast))
        result = await app.forecast_for_mode(bot, "tournament", automatic_one=True)
        self.assertEqual(result, [])
        self.assertEqual(scanned, [33121, app.MetaculusClient.CURRENT_MINIBENCH_ID])

    async def test_credit_denial_precedes_all_model_calls(self):
        candidate = atomic(post_id=973, qid=773, hours_left=4)
        bot = self.bot()
        self.fixed_search_client()
        def scan(client, tournament, *, read_page):
            return [candidate] if tournament == 33121 else []
        def denied(*args, **kwargs):
            from sponsored_credit_guard import SponsoredCreditUnavailable
            raise SponsoredCreditUnavailable("Donation exhausted")
        async def forbidden_forecast(*args, **kwargs):
            raise AssertionError("Credit denial must prevent all model and journal calls")
        self.start_patch(patch.object(app, "BoundedPostReader", lambda *_: object()))
        self.start_patch(patch.object(app, "scan_open_tournament", scan))
        self.start_patch(patch.object(app, "deduplicate_tournaments", lambda a, b: (a, b)))
        # Fix the synthetic clock: test data closes relative to NOW, never
        # relative to the real runner clock. Otherwise the test starts
        # failing hours later although no production behavior changed.
        self.start_patch(patch.object(app, "select_one_automatic_candidate",
                                      lambda a, b: select_one_automatic_candidate(a, b, now=NOW)))
        self.start_patch(patch.object(app, "verify_sponsored_openrouter_credit", denied))
        self.start_patch(patch.object(bot, "forecast_questions", forbidden_forecast))
        self.start_patch(patch.dict("os.environ", {
            "OPENROUTER_API_KEY": "sk-or-v1-test-key-9fc",
            "METACULUS_SPONSORED_KEY_SUFFIX": "9fc",
        }))
        from sponsored_credit_guard import SponsoredCreditUnavailable
        with self.assertRaises(SponsoredCreditUnavailable):
            await app.forecast_for_mode(bot, "tournament", automatic_one=True)

    async def test_uncertain_first_publication_does_not_starve_next_candidate(self):
        self.start_patch(patch.object(app, "select_one_automatic_candidate",
                                      lambda a, b: select_one_automatic_candidate(a, b, now=NOW)))
        for offset, state in enumerate(("sending", "uncertain")):
            with self.subTest(state=state):
                blocked = atomic(post_id=980 + offset, qid=780 + offset, hours_left=3)
                next_question = atomic(post_id=990 + offset, qid=790 + offset, hours_left=5)
                bot = self.bot(publish_reports_to_metaculus=True)
                bot._publication_runtime()
                key = bot._publication_key(blocked)
                self.publication_store.prepare(key, "Frozen report", [{
                    "kind": "forecast", "question_id": blocked.id_of_question,
                    "payload": {"probability_yes": 0.5},
                }])
                self.publication_store.claim(key, 0)
                if state == "uncertain":
                    self.publication_store.finish(key, 0, state)
                selected = await app.select_automatic_publishable(
                    bot, [blocked], [next_question], self.fixed_search_client(),
                )
                self.assertEqual(selected, ([], [next_question]))
                self.assertEqual(self.publication_store.steps(key)[0]["state"], state)

    async def test_confirmed_receipt_prevents_repeat_when_inventory_history_is_stale(self):
        old = atomic(post_id=984, qid=784, hours_left=3, prior=False)
        new = atomic(post_id=994, qid=794, hours_left=5)
        bot = self.bot(publish_reports_to_metaculus=True)
        bot._publication_runtime()
        key = bot._publication_key(old)
        self.publication_store.prepare(key, "Confirmed report", [{
            "kind": "forecast", "question_id": old.id_of_question,
            "payload": {"probability_yes": 0.5},
        }])
        self.publication_store.claim(key, 0)
        self.publication_store.finish(key, 0, "confirmed")
        self.start_patch(patch.object(app, "select_one_automatic_candidate",
                                      lambda a, b: select_one_automatic_candidate(a, b, now=NOW)))
        selected = await app.select_automatic_publishable(
            bot, [old, new], [], self.fixed_search_client(),
        )
        self.assertEqual(selected, ([new], []))
        self.assertEqual(self.publication_store.steps(key)[0]["state"], "confirmed")

    async def test_weekly_budget_exhaustion_calls_no_forecast(self):
        from fixed_search_research import WeeklyBudgetExhausted
        candidate = atomic(post_id=985, qid=785, hours_left=3)
        bot = self.bot()
        client = self.fixed_search_client()

        def budget_status(post_id=None):
            if post_id is not None:
                raise WeeklyBudgetExhausted("Synthetic weekly budget spent")
            return {"blocked_post_ids": [], "remaining_usd": "0.00"}

        client.preflight.side_effect = budget_status
        self.start_patch(patch.object(app, "BoundedPostReader", lambda *_: object()))
        self.start_patch(patch.object(app, "scan_open_tournament",
                                      lambda client, tournament, **kw: [candidate] if tournament == 33121 else []))
        self.start_patch(patch.object(app, "deduplicate_tournaments", lambda a, b: (a, b)))
        self.start_patch(patch.object(app, "select_one_automatic_candidate",
                                      lambda a, b: select_one_automatic_candidate(a, b, now=NOW)))
        self.start_patch(patch.object(app, "verify_sponsored_openrouter_credit", lambda *a, **kw: {}))

        async def forbidden_forecast(*args, **kwargs):
            raise AssertionError("Exhausted research budget must prevent all forecasting")

        self.start_patch(patch.object(bot, "forecast_questions", forbidden_forecast))
        result = await app.forecast_for_mode(bot, "tournament", automatic_one=True)
        self.assertEqual(result, [])
        self.assertIsNone(bot._fixed_search_client)

    def test_automatic_cli_is_authorized_without_changing_manual_approval(self):
        main_source = (ROOT / "main.py").read_text()
        manual = (ROOT / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        automatic = (ROOT / ".github/workflows/run_bot_automatic.yaml").read_text()
        self.assertIn('"--automatic-one"', main_source)
        self.assertIn("--automatic-one", automatic)
        self.assertIn("\n  schedule:", automatic)
        self.assertNotIn("--automatic-one", manual)
        self.assertIn("default: DO_NOT_RUN", manual)

    async def test_automatic_and_specific_post_modes_are_mutually_exclusive(self):
        with self.assertRaises(RuntimeError):
            await app.forecast_for_mode(
                self.bot(), "tournament", automatic_one=True, first_live_post_id=93001,
            )
        with self.assertRaises(RuntimeError):
            await app.forecast_for_mode(self.bot(), "metaculus_cup", automatic_one=True)


if __name__ == "__main__":
    import unittest
    unittest.main()


"""No-network tests for dormant scheduler and three-hour question windows."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .support import OfflineTestCase
from .test_automatic_one_selection import atomic
from automatic_one_selection import MINIMUM_REMAINING, select_one_automatic_candidate
from scheduler_policy import (
    DailySpendEvidence, DispatchSnapshot, POLL_INTERVAL, evaluate_dispatch,
)


NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def accepted(**changes):
    fields = {
        "now": NOW,
        "github_status_checked_at": NOW,
        "last_dispatch_at": NOW - timedelta(minutes=15),
        "active_publisher_runs": 0,
        "queued_publisher_runs": 0,
        "explicit_automation_approved": True,
        "activation_switch_enabled": True,
        "sponsor_budget_guard_available": True,
        "perplexity_spend_guard_available": True,
        "previous_publication_outcome_certain": True,
        "first_attempt": True,
        "daily_spend": DailySpendEvidence(
            day_utc=NOW.date(), checked_at=NOW,
            charged_usd="0.10", approved_day_cap_usd="1.00",
            approved_run_reserve_usd="0.40",
            attempted_dispatches=1, max_daily_dispatches=2,
            complete=True,
        ),
    }
    fields.update(changes)
    return DispatchSnapshot(**fields)


class FutureSchedulerPolicyTests(OfflineTestCase):
    def test_unapproved_defaults_fail_closed(self):
        result = evaluate_dispatch(DispatchSnapshot(now=NOW))
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "activation_or_budget_not_authorized")

    def test_no_scheduler_is_activated_by_this_draft(self):
        publisher = (ROOT / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        main = (ROOT / "main.py").read_text()
        self.assertNotIn("\n  schedule:", publisher)
        self.assertIn("default: DO_NOT_RUN", publisher)
        self.assertIn("APPROVE_ONE_LIVE_PUBLICATION_RUN", publisher)
        self.assertNotIn("--automatic-one", publisher)
        self.assertNotIn('parser.add_argument("--automatic-one"', main)
        self.assertEqual(POLL_INTERVAL, timedelta(minutes=15))

    def test_full_verified_manual_policy_can_only_return_a_decision(self):
        result = evaluate_dispatch(accepted())
        self.assertTrue(result.allowed)
        self.assertEqual(result.reason, "eligible_for_separately_authorized_dispatch")
        self.assertFalse(hasattr(result, "dispatch"))

    def test_daily_spend_missing_or_unverified_denies(self):
        base = accepted().daily_spend
        self.assertEqual(
            evaluate_dispatch(accepted(daily_spend=None)).reason,
            "verified_daily_spend_unavailable",
        )
        for wrong in (False, "yes", 1, None):
            with self.subTest(wrong=wrong):
                self.assertEqual(
                    evaluate_dispatch(accepted(daily_spend=replace(
                        base, complete=wrong,
                    ))).reason,
                    "verified_daily_spend_unavailable",
                )

    def test_daily_day_and_freshness_mandatory(self):
        base = accepted().daily_spend
        self.assertEqual(
            evaluate_dispatch(accepted(daily_spend=replace(
                base, day_utc=date(2026, 11, 7),
            ))).reason,
            "daily_spend_day_mismatch",
        )
        for checked in (
            NOW - timedelta(minutes=5, seconds=1),
            NOW + timedelta(seconds=1),
        ):
            with self.subTest(checked=checked):
                self.assertEqual(
                    evaluate_dispatch(accepted(daily_spend=replace(
                        base, checked_at=checked,
                    ))).reason,
                    "daily_spend_snapshot_stale",
                )
        self.assertTrue(evaluate_dispatch(accepted(
            daily_spend=replace(base, checked_at=NOW-timedelta(minutes=5)),
        )).allowed)

    def test_daily_count_quota_includes_failed_or_uncertain_attempts(self):
        base = accepted().daily_spend
        for attempted, maximum, expected in (
            (2, 2, "daily_dispatch_quota_exhausted"),
            (3, 2, "daily_dispatch_quota_exhausted"),
            (-1, 2, "daily_dispatch_count_unverified"),
            (True, 2, "daily_dispatch_count_unverified"),
            ("1", 2, "daily_dispatch_count_unverified"),
            (1, 0, "daily_dispatch_count_unverified"),
            (1, "2", "daily_dispatch_count_unverified"),
        ):
            with self.subTest(attempted=attempted, maximum=maximum):
                result = evaluate_dispatch(accepted(daily_spend=replace(
                    base, attempted_dispatches=attempted,
                    max_daily_dispatches=maximum,
                )))
                self.assertFalse(result.allowed)
                self.assertEqual(result.reason, expected)

    def test_reserved_cost_never_exceeds_daily_remaining_allowance(self):
        base = accepted().daily_spend
        self.assertTrue(evaluate_dispatch(accepted(daily_spend=replace(
            base, charged_usd="0.60", approved_run_reserve_usd="0.40",
        ))).allowed)
        self.assertEqual(evaluate_dispatch(accepted(daily_spend=replace(
            base, charged_usd="0.600001", approved_run_reserve_usd="0.40",
        ))).reason, "daily_spend_allowance_exhausted")
        self.assertEqual(evaluate_dispatch(accepted(daily_spend=replace(
            base, charged_usd="0.60", approved_run_reserve_usd="0.41",
        ))).reason, "daily_spend_allowance_exhausted")

    def test_invalid_daily_money_values_are_rejected(self):
        base = accepted().daily_spend
        for field in ("charged_usd", "approved_day_cap_usd",
                      "approved_run_reserve_usd"):
            for invalid in (None, 1.0, True, "-1", "NaN", "1e2", " 0.01",
                            "0.1234567", ""):
                with self.subTest(field=field, invalid=invalid):
                    result = evaluate_dispatch(accepted(
                        daily_spend=replace(base, **{field: invalid}),
                    ))
                    self.assertEqual(result.reason, "daily_spend_values_invalid")
        for field, invalid in (
            ("approved_day_cap_usd", "0"),
            ("approved_run_reserve_usd", "0"),
            ("approved_run_reserve_usd", "2.00"),
        ):
            with self.subTest(field=field, invalid=invalid):
                self.assertEqual(evaluate_dispatch(accepted(
                    daily_spend=replace(base, **{field: invalid}),
                )).reason, "daily_spend_values_invalid")

    def test_daily_budget_never_activates_real_publisher(self):
        result = evaluate_dispatch(accepted())
        self.assertTrue(result.allowed)
        self.assertFalse(hasattr(result, "dispatch"))
        publisher = (ROOT / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        self.assertNotIn("\n  schedule:", publisher)
        self.assertIn("default: DO_NOT_RUN", publisher)

    def test_each_approval_and_budget_signal_is_mandatory(self):
        for name in (
            "explicit_automation_approved",
            "activation_switch_enabled",
            "sponsor_budget_guard_available",
            "perplexity_spend_guard_available",
            "previous_publication_outcome_certain",
            "first_attempt",
        ):
            for value in (False, None, "", 1, "true"):
                with self.subTest(field=name, value=value):
                    self.assertFalse(evaluate_dispatch(accepted(**{name: value})).allowed)

    def test_no_overlap_no_queue_and_unknown_state_fail_closed(self):
        for field in ("active_publisher_runs", "queued_publisher_runs"):
            for value in (None, -1, 1, True, "0"):
                with self.subTest(field=field, value=value):
                    result = evaluate_dispatch(accepted(**{field: value}))
                    self.assertFalse(result.allowed)

    def test_status_must_be_recent_even_if_empty(self):
        self.assertFalse(evaluate_dispatch(accepted(github_status_checked_at=None)).allowed)
        self.assertFalse(evaluate_dispatch(accepted(
            github_status_checked_at=NOW-timedelta(minutes=6))).allowed)
        self.assertTrue(evaluate_dispatch(accepted(
            github_status_checked_at=NOW-timedelta(minutes=5))).allowed)
        self.assertFalse(evaluate_dispatch(accepted(
            github_status_checked_at=NOW+timedelta(seconds=1))).allowed)

    def test_dispatch_at_most_once_per_fifteen_minutes(self):
        self.assertFalse(evaluate_dispatch(accepted(
            last_dispatch_at=NOW-timedelta(minutes=14, seconds=59))).allowed)
        self.assertTrue(evaluate_dispatch(accepted(
            last_dispatch_at=NOW-timedelta(minutes=15))).allowed)
        self.assertFalse(evaluate_dispatch(accepted(
            last_dispatch_at=NOW+timedelta(seconds=1))).allowed)

    def test_timestamps_must_be_aware(self):
        with self.assertRaises(ValueError):
            evaluate_dispatch(accepted(now=NOW.replace(tzinfo=None)))
        with self.assertRaises(ValueError):
            evaluate_dispatch(accepted(github_status_checked_at=NOW.replace(tzinfo=None)))
        with self.assertRaises(ValueError):
            evaluate_dispatch(accepted(last_dispatch_at=NOW.replace(tzinfo=None)))

    def test_ninety_minute_candidate_window_versus_short_open_questions(self):
        from .test_automatic_one_selection import NOW as FIXTURE_NOW
        self.assertEqual(MINIMUM_REMAINING, timedelta(minutes=90))
        # The fixture creates a question that has 3h remaining at FIXTURE_NOW.
        q = atomic(post_id=991, qid=791, hours_left=3)
        self.assertEqual(select_one_automatic_candidate([q], [], now=FIXTURE_NOW), ([q], []))
        at_boundary = FIXTURE_NOW + timedelta(minutes=90)
        self.assertEqual(select_one_automatic_candidate([q], [], now=at_boundary), ([q], []))
        too_late = at_boundary + timedelta(seconds=1)
        self.assertEqual(select_one_automatic_candidate([q], [], now=too_late), ([], []))

    def test_burst_cannot_guarantee_full_coverage_and_never_overlaps(self):
        from .test_automatic_one_selection import NOW as FIXTURE_NOW
        # Three simultaneous questions, each open for 3 hours.
        batch = [atomic(post_id=995+i, qid=795+i, hours_left=3) for i in range(3)]
        first, _ = select_one_automatic_candidate(batch, [], now=FIXTURE_NOW)
        self.assertEqual(len(first), 1)
        # A 60m publisher is active: no second launch even if cron ticks.
        denied = evaluate_dispatch(accepted(active_publisher_runs=1))
        self.assertFalse(denied.allowed)
        # At a later check 75m in, 105m is left; one more fits.
        after_75 = [q for q in batch if q.id_of_post != first[0].id_of_post]
        second, _ = select_one_automatic_candidate(
            after_75, [], now=FIXTURE_NOW+timedelta(minutes=75))
        self.assertEqual(len(second), 1)
        # At 150m only 30m remains; the third must be skipped.
        third = [q for q in after_75 if q.id_of_post != second[0].id_of_post]
        self.assertEqual(select_one_automatic_candidate(
            third, [], now=FIXTURE_NOW+timedelta(minutes=150)), ([], []))


if __name__ == "__main__":
    import unittest
    unittest.main()

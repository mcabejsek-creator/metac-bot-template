"""Strict entirely offline tests for disabled-only UTC budget-day preview."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .support import OfflineTestCase
from .test_dispatch_intent_controller import APPROVED
from daily_rollover_review import (
    DailyRolloverEvidence, ONE_DAY_APPROVAL,
    preview_disabled_daily_rollover,
)
from dispatch_intent_controller import ControllerState, DailyQuota


NOW = datetime(2026, 11, 9, 12, 30, tzinfo=timezone.utc)
BEFORE = replace(APPROVED, enabled=False)
ROOT = Path(__file__).resolve().parents[1]


def proof(**changes):
    fields = dict(
        previous_day_utc="2026-11-08",
        target_day_utc="2026-11-09",
        checked_at=NOW,
        prior_attempts=1,
        prior_reserved_usd="0.40",
        provider_billed_usd="0.03",
        approved_prior_day_cap_usd="1.00",
        unaccounted_agent_attempts=0,
        unresolved_publication_attempts=0,
        active_publisher_runs=0,
        queued_publisher_runs=0,
        provider_statement_independently_verified=True,
        all_clients_and_charges_reconciled=True,
        publication_history_reconciled=True,
        prior_audit_archived_immutably=True,
        operator_approval=ONE_DAY_APPROVAL,
    )
    fields.update(changes)
    return DailyRolloverEvidence(**fields)


def preview(state=BEFORE, evidence=None, *, now=NOW, epoch=3):
    return preview_disabled_daily_rollover(
        state, proof() if evidence is None else evidence,
        now=now, approval_epoch=epoch,
    )


class DailyRolloverReviewTests(OfflineTestCase):
    def test_only_complete_synthetic_audit_proposes_still_disabled_state(self):
        result = preview()
        self.assertTrue(result.eligible_for_separately_authorized_cas)
        self.assertEqual(result.reason, "eligible_for_separate_operator_cas_review")
        new = result.proposed_disabled_state
        self.assertIsInstance(new, ControllerState)
        self.assertIs(new.enabled, False)
        self.assertIsNone(new.pending)
        self.assertEqual(new.approval_epoch, BEFORE.approval_epoch)
        self.assertEqual(new.daily_quota, DailyQuota(
            day_utc="2026-11-09", attempts=0, reserved_usd="0",
        ))
        self.assertEqual(BEFORE.daily_quota.day_utc, "2026-11-08")
        self.assertEqual(BEFORE.daily_quota.attempts, 1)
        self.assertEqual(ControllerState.decode(new.encode()), new)

    def test_defaults_and_missing_operator_approval_deny(self):
        self.assertFalse(preview_disabled_daily_rollover(
            ControllerState(), None, now=NOW, approval_epoch=0,
        ).eligible_for_separately_authorized_cas)
        for a in ("", "YES", None, True):
            with self.subTest(a=a):
                value = preview(evidence=proof(operator_approval=a))
                self.assertFalse(value.eligible_for_separately_authorized_cas)
                self.assertIsNone(value.proposed_disabled_state)

    def test_all_independent_attestations_are_separate_requirements(self):
        for key in (
            "provider_statement_independently_verified",
            "all_clients_and_charges_reconciled",
            "publication_history_reconciled",
            "prior_audit_archived_immutably",
        ):
            for v in (False, 1, "yes", None):
                with self.subTest(field=key, value=v):
                    d = preview(evidence=proof(**{key:v}))
                    self.assertFalse(d.eligible_for_separately_authorized_cas)
                    self.assertEqual(d.reason, "independent_billing_and_history_audit_missing")

    def test_never_approves_pending_enabled_or_wrong_epoch(self):
        enabled = replace(BEFORE, enabled=True)
        self.assertEqual(preview(state=enabled).reason, "active_or_uncertain_controller")
        self.assertEqual(preview(state=BEFORE, epoch=4).reason,
                         "controller_approval_epoch_mismatch")
        noquota = replace(BEFORE, daily_quota=None)
        self.assertEqual(preview(state=noquota).reason, "prior_durable_quota_missing")
        # Build a previously attempted pending run, and shut it OFF; still blocked.
        from .test_dispatch_intent_controller import snapshot
        from dispatch_intent_controller import propose_reservation
        pending = propose_reservation(APPROVED, snapshot(), approval_epoch=3).state
        stopped = replace(pending, enabled=False)
        self.assertEqual(preview(state=stopped).reason, "active_or_uncertain_controller")

    def test_queued_or_active_publisher_blocks_even_when_other_checks_green(self):
        for key in ("active_publisher_runs", "queued_publisher_runs"):
            for number in (1, 7, None, -1, True, "0"):
                with self.subTest(key=key, number=number):
                    d = preview(evidence=proof(**{key:number}))
                    self.assertFalse(d.eligible_for_separately_authorized_cas)
                    self.assertIsNone(d.proposed_disabled_state)

    def test_uncertain_agent_or_publication_never_releases_reserve(self):
        for key in ("unaccounted_agent_attempts",
                    "unresolved_publication_attempts"):
            for number in (1, 2, -1, None, True):
                with self.subTest(key=key, number=number):
                    d = preview(evidence=proof(**{key:number}))
                    self.assertFalse(d.eligible_for_separately_authorized_cas)

    def test_audited_counts_must_match_durable_state(self):
        for n in (0, 2, -1, None, True):
            d = preview(evidence=proof(prior_attempts=n))
            self.assertFalse(d.eligible_for_separately_authorized_cas)
        self.assertEqual(preview(evidence=proof(prior_reserved_usd="0.41")).reason,
                         "prior_budget_not_reconciled")

    def test_no_stale_or_future_evidence_and_requires_utc_aware(self):
        for when in (NOW-timedelta(minutes=5, seconds=1),
                     NOW+timedelta(seconds=1)):
            self.assertEqual(preview(evidence=proof(checked_at=when)).reason,
                             "independent_evidence_stale")
        for when in (NOW.replace(tzinfo=None), None, "2026-11-09"):
            self.assertEqual(preview(evidence=proof(checked_at=when)).reason,
                             "verified_utc_timestamps_missing")
            self.assertEqual(preview(now=when).reason,
                             "verified_utc_timestamps_missing")
        self.assertTrue(preview(evidence=proof(
            checked_at=NOW-timedelta(minutes=5),
        )).eligible_for_separately_authorized_cas)

    def test_rollover_cannot_skip_or_repeat_days_and_uses_utc_date(self):
        variants = (
            {"previous_day_utc":"2026-11-07"},
            {"target_day_utc":"2026-11-10"},
            {"target_day_utc":"2026-02-30"},
            {"previous_day_utc":"2026-11-08T00:00:00Z"},
        )
        for fields in variants:
            with self.subTest(fields=fields):
                self.assertEqual(preview(evidence=proof(**fields)).reason,
                                 "utc_day_transition_not_verified")
        tomorrow = NOW+timedelta(days=1)
        shifted = preview(now=tomorrow, evidence=proof(checked_at=tomorrow))
        self.assertEqual(shifted.reason, "utc_day_transition_not_verified")

    def test_invalid_money_or_prior_day_overbilling_blocks(self):
        for field in ("provider_billed_usd", "approved_prior_day_cap_usd",
                      "prior_reserved_usd"):
            for invalid in (None, -1, True, "NaN", "1e3", "-0.01", "",
                            "1.1234567", "0.2 "):
                with self.subTest(field=field, invalid=invalid):
                    self.assertEqual(
                        preview(evidence=proof(**{field:invalid})).reason,
                        "daily_financial_evidence_invalid")
        self.assertEqual(preview(evidence=proof(
            provider_billed_usd="1.01")).reason, "prior_budget_not_reconciled")
        self.assertEqual(preview(evidence=proof(
            approved_prior_day_cap_usd="0")).reason, "prior_budget_not_reconciled")

    def test_prior_intent_from_target_day_blocks_rollover(self):
        state = replace(
            BEFORE,
            last_intent_at="2026-11-09T10:00:00Z",
            last_slot="2026-11-09T10:00:00Z",
        )
        self.assertEqual(preview(state=state).reason,
                         "future_intent_not_reconciled")

    def test_preview_never_writes_or_activates_existing_publisher(self):
        import inspect
        import daily_rollover_review as source
        body = inspect.getsource(source)
        for danger in ("urllib.request", "requests.post",
                       "workflow_dispatch(", "compare_and_swap(",
                       "method=\"POST\"", "enabled=True"):
            self.assertNotIn(danger, body)
        pub = (ROOT / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        self.assertNotIn("\n  schedule:", pub)
        self.assertIn("default: DO_NOT_RUN", pub)
        self.assertFalse((ROOT / ".github/workflows/autonomous_dispatch.yaml").exists())


if __name__ == "__main__":
    import unittest
    unittest.main()

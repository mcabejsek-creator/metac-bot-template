"""Wholly synthetic orchestration integration: no network, keys or API usage."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .support import OfflineTestCase
from .test_dispatch_intent_controller import APPROVED, FakeStore, snapshot, NOW
from dispatch_intent_controller import ControllerState, propose_reservation
from autonomous_orchestrator import (
    AutonomousPreflightRejected, KillSwitchUncertain,
    IndependentReadings, OutcomeEvidence,
    build_dispatch_snapshot, evaluate_offline_tick,
    reserve_only_after_preflight, persist_emergency_stop, assess_settlement,
)


ROOT = Path(__file__).resolve().parents[1]


def verified_readings(**changes):
    opts = dict(
        github_checked_at=NOW,
        active_publishers=0,
        queued_publishers=0,
        funded_openrouter_checked_at=NOW,
        funded_openrouter_ok=True,
        perplexity_checked_at=NOW,
        perplexity_spend_ok=True,
        journal_checked_at=NOW,
        prior_publication_certain=True,
        operator_approval_confirmed=True,
        automation_switch_confirmed=True,
        is_first_attempt=True,
    )
    opts.update(changes)
    return IndependentReadings(**opts)


class Source:
    def __init__(self, readings):
        self.readings = readings
        self.calls = 0

    def read(self):
        self.calls += 1
        if isinstance(self.readings, Exception):
            raise self.readings
        return self.readings


def confirmed_proof(pending_state, **changes):
    intent = pending_state.pending.intent_id
    opts = dict(
        intent_id=intent,
        github_run_id=42476608583,
        run_completed=True,
        workflow_conclusion="success",
        workflow_path="autonomous_one_worker.yaml",
        run_intent_id=intent,
        run_confirmed_post_id=46347,
        journal_confirmed_post_id=46347,
        metaculus_confirmed_post_id=46347,
        independently_checked_billing=True,
        github_finished_at=NOW,
        journal_checked_at=NOW,
        metaculus_checked_at=NOW,
        billing_checked_at=NOW,
    )
    opts.update(changes)
    return OutcomeEvidence(**opts)


class AutonomousOrchestratorTests(OfflineTestCase):
    def test_zero_config_denies_without_reading_any_provider(self):
        store = FakeStore(ControllerState())
        src = Source(verified_readings())
        preview = evaluate_offline_tick(store, src, now=NOW, approval_epoch=0)
        self.assertFalse(preview.allowed)
        self.assertEqual(preview.reason, "persistent_kill_switch_disabled")
        self.assertEqual(src.calls, 0)
        self.assertEqual(store.calls, ["GET"])
        blocked = reserve_only_after_preflight(store, src, now=NOW, approval_epoch=0)
        self.assertFalse(blocked.allowed)
        self.assertEqual(src.calls, 0)

    def test_complete_trusted_synthetic_snapshot_is_only_eligible_plan(self):
        src = Source(verified_readings())
        store = FakeStore()
        outcome = evaluate_offline_tick(store, src, now=NOW, approval_epoch=3)
        self.assertTrue(outcome.allowed)
        self.assertFalse(outcome.durably_reserved)
        self.assertEqual(outcome.reason, "needs_durable_compare_and_swap")
        self.assertEqual(store.calls, ["GET"])
        self.assertIsNone(store.state.pending)
        self.assertEqual(src.calls, 1)

    def test_each_source_timestamp_must_be_fresh(self):
        for field in (
            "github_checked_at", "funded_openrouter_checked_at",
            "perplexity_checked_at", "journal_checked_at",
        ):
            for value in (None, NOW-timedelta(minutes=5, seconds=1),
                          NOW+timedelta(seconds=1)):
                with self.subTest(field=field, value=value):
                    src = Source(verified_readings(**{field: value}))
                    with self.assertRaises(AutonomousPreflightRejected):
                        evaluate_offline_tick(FakeStore(), src, now=NOW, approval_epoch=3)

    def test_missing_source_or_unsupported_attestation_fail_closed(self):
        fields = (
            "funded_openrouter_ok", "perplexity_spend_ok",
            "prior_publication_certain", "operator_approval_confirmed",
            "automation_switch_confirmed", "is_first_attempt",
        )
        for field in fields:
            for value in (False, None, 1, "true"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(AutonomousPreflightRejected):
                        build_dispatch_snapshot(
                            verified_readings(**{field: value}),
                            now=NOW, last_dispatch_at=None,
                        )
        with self.assertRaises(AutonomousPreflightRejected):
            build_dispatch_snapshot(None, now=NOW, last_dispatch_at=None)

    def test_active_queued_or_ambiguous_status_cannot_reserve(self):
        for field in ("active_publishers", "queued_publishers"):
            for value in (1, 2, -1, True, None, "0"):
                with self.subTest(field=field, value=value):
                    store = FakeStore()
                    with self.assertRaises(AutonomousPreflightRejected):
                        evaluate_offline_tick(store,
                            Source(verified_readings(**{field: value})),
                            now=NOW, approval_epoch=3)
                    self.assertEqual(store.calls, ["GET"])

    def test_missing_perplexity_provider_fails_before_any_cas(self):
        store = FakeStore()
        src = Source(OSError("Perplexity balance cannot be independently verified"))
        with self.assertRaises(OSError):
            reserve_only_after_preflight(store, src, now=NOW, approval_epoch=3,
                                         explicit_control_write_approved=True)
        self.assertEqual(store.calls, ["GET"])

    def test_optin_control_reservation_is_durable_but_never_dispatches(self):
        store = FakeStore()
        src = Source(verified_readings())
        result = reserve_only_after_preflight(
            store, src, now=NOW, approval_epoch=3,
            explicit_control_write_approved=True,
        )
        self.assertTrue(result.allowed)
        self.assertTrue(result.durably_reserved)
        self.assertEqual(result.reason, "durably_reserved_not_dispatched")
        self.assertIsNotNone(result.intent_id)
        self.assertEqual(store.calls, ["GET", "GET", "CAS"])
        self.assertEqual(src.calls, 1)
        self.assertEqual(store.state.pending.intent_id, result.intent_id)
        self.assertFalse(hasattr(result, "send_workflow"))

    def test_deduplicated_pending_does_not_revisit_any_source(self):
        store = FakeStore()
        src = Source(verified_readings())
        first = reserve_only_after_preflight(
            store, src, now=NOW, approval_epoch=3, explicit_control_write_approved=True,
        )
        self.assertTrue(first.allowed)
        before = src.calls
        denied = reserve_only_after_preflight(
            store, src, now=NOW+timedelta(minutes=16), approval_epoch=3,
            explicit_control_write_approved=True,
        )
        self.assertFalse(denied.allowed)
        self.assertIn("reconciliation", denied.reason)
        self.assertEqual(src.calls, before)
        self.assertEqual(store.calls.count("CAS"), 1)

    def test_cas_race_with_emergency_off_denies_without_write(self):
        class RacingStore(FakeStore):
            def read(self):
                self.calls.append("GET")
                if self.calls.count("GET") == 1:
                    return APPROVED, "a"*40
                self.state = replace(APPROVED, enabled=False)
                self.revision = "b"*40
                return self.state, self.revision
        store = RacingStore()
        result = reserve_only_after_preflight(
            store, Source(verified_readings()), now=NOW, approval_epoch=3,
            explicit_control_write_approved=True,
        )
        self.assertFalse(result.allowed)
        self.assertEqual(store.calls, ["GET", "GET"])

    def test_conflict_and_unknown_write_never_trigger_retry(self):
        for result in (False, None):
            store = FakeStore()
            store.compare_and_swap = lambda *args: result
            outcome = reserve_only_after_preflight(
                store, Source(verified_readings()), now=NOW, approval_epoch=3,
                explicit_control_write_approved=True,
            )
            self.assertFalse(outcome.allowed)
            self.assertFalse(outcome.durably_reserved)
        store = FakeStore()
        attempts = []
        def ambiguous(*_):
            attempts.append("attempt")
            raise TimeoutError("CAS may have committed")
        store.compare_and_swap = ambiguous
        with self.assertRaises(TimeoutError):
            reserve_only_after_preflight(
                store, Source(verified_readings()), now=NOW, approval_epoch=3,
                explicit_control_write_approved=True,
            )
        self.assertEqual(attempts, ["attempt"])

    def test_emergency_off_requires_explicit_operator_intent(self):
        store = FakeStore()
        with self.assertRaises(KillSwitchUncertain):
            persist_emergency_stop(store)
        self.assertEqual(store.calls, [])
        off = persist_emergency_stop(store, operator_stop_authorized=True)
        self.assertFalse(off.allowed)
        self.assertEqual(store.calls, ["GET", "CAS", "GET"])
        self.assertFalse(store.state.enabled)

    def test_emergency_off_keeps_uncertain_pending_blocked(self):
        store = FakeStore()
        result = reserve_only_after_preflight(
            store, Source(verified_readings()), now=NOW, approval_epoch=3,
            explicit_control_write_approved=True,
        )
        old_pending = store.state.pending
        off = persist_emergency_stop(store, operator_stop_authorized=True)
        self.assertFalse(off.allowed)
        self.assertFalse(store.state.enabled)
        self.assertEqual(store.state.pending, old_pending)
        denied = evaluate_offline_tick(store, Source(verified_readings()),
                                       now=NOW, approval_epoch=3)
        self.assertFalse(denied.allowed)

    def test_ambiguous_emergency_cas_must_not_claim_success(self):
        store = FakeStore()
        store.compare_and_swap = lambda *_: None
        with self.assertRaises(KillSwitchUncertain):
            persist_emergency_stop(store, operator_stop_authorized=True)
        store = FakeStore()
        def broken(*_):
            raise TimeoutError("write may have committed")
        store.compare_and_swap = broken
        with self.assertRaises(KillSwitchUncertain):
            persist_emergency_stop(store, operator_stop_authorized=True)

    def test_emergency_stop_detects_reenabled_state_after_cas(self):
        class Reenabled(FakeStore):
            def read(self):
                state, rev = super().read()
                if self.calls.count("GET") == 2:
                    return replace(state, enabled=True), rev
                return state, rev
        with self.assertRaises(KillSwitchUncertain):
            persist_emergency_stop(Reenabled(), operator_stop_authorized=True)

    def test_settlement_requires_independent_journal_and_metaculus_match(self):
        state = propose_reservation(APPROVED, snapshot(), approval_epoch=3).state
        verified = assess_settlement(state, confirmed_proof(state), now=NOW)
        self.assertTrue(verified.allowed)
        self.assertEqual(verified.reason,
                         "eligible_for_separately_authorized_manual_reconciliation")
        self.assertIsNotNone(state.pending)
        for changes in (
            {"run_intent_id": "wrong"},
            {"journal_confirmed_post_id": 46348},
            {"metaculus_confirmed_post_id": None},
            {"run_completed": False},
            {"workflow_conclusion": "failure"},
            {"workflow_path": "run_bot_on_tournament.yaml"},
            {"run_confirmed_post_id": 46348},
            {"independently_checked_billing": False},
            {"github_run_id": 0},
            {"metaculus_checked_at": NOW-timedelta(days=8)},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(assess_settlement(
                    state, confirmed_proof(state, **changes), now=NOW
                ).allowed)
        self.assertFalse(assess_settlement(
            ControllerState(), confirmed_proof(state), now=NOW
        ).allowed)

    def test_settlement_never_clears_existing_pending(self):
        state = propose_reservation(APPROVED, snapshot(), approval_epoch=3).state
        before = state.encode()
        result = assess_settlement(state, confirmed_proof(state), now=NOW)
        self.assertTrue(result.allowed)
        self.assertEqual(state.encode(), before)
        self.assertIsNotNone(state.pending)

    def test_production_publication_remains_manual_only(self):
        source = (ROOT / "main.py").read_text()
        publisher = (ROOT / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        self.assertNotIn('parser.add_argument("--automatic-one"', source)
        self.assertNotIn("--automatic-one", publisher)
        self.assertNotIn("\n  schedule:", publisher)
        self.assertIn("default: DO_NOT_RUN", publisher)
        self.assertFalse((ROOT / ".github/workflows/autonomous_one_worker.yaml").exists())
        self.assertFalse((ROOT / ".github/workflows/autonomous_dispatch.yaml").exists())


if __name__ == "__main__":
    import unittest
    unittest.main()

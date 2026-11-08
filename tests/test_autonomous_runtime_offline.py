"""Stdlib integration of the dormant controller and synthetic publication only.

Run without the SDK: python -B tests/test_autonomous_runtime_offline.py
The usual tests.run also discovers this suite. Direct execution adds a network
audit guard and fails even on a caught network attempt. No main.py import,
credentials, real journal, workflow dispatch, provider or Metaculus access.
"""

import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


NETWORK_ATTEMPTS = []


def reject_network(event, args):
    if event in {
        "socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
        "socket.gethostbyaddr", "socket.sendto", "socket.bind",
        "http.client.connect", "http.client.send",
    }:
        NETWORK_ATTEMPTS.append(event)
        raise RuntimeError("Network forbidden in stdlib offline integration")


if __name__ == "__main__":
    sys.addaudithook(reject_network)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autonomous_orchestrator import (
    AutonomousPreflightRejected, IndependentReadings, KillSwitchUncertain,
    OutcomeEvidence, assess_settlement, evaluate_offline_tick,
    persist_emergency_stop, reserve_only_after_preflight,
)
from dispatch_intent_controller import ControllerState, DailyQuota
from github_publisher_queue_readonly import (
    GitHubPublisherQueueReader, KNOWN_STUCK_RUN_ID, PUBLISHING_WORKFLOW_ID,
)
from github_queue_preflight import GitHubQueuePreflightSource
from perplexity_run_budget import PerplexityAutoRunBudget, PerplexityBudgetUnavailable
from publication_journal import PublicationStore, PublicationUncertain, Publisher
from scheduler_policy import DailySpendEvidence


NOW = datetime(2026, 11, 8, 12, 30, tzinfo=timezone.utc)


class MemoryControl:
    """Synthetic CAS; never the configured GitHub control or publication store."""

    def __init__(self, state=None):
        self.state = state if state is not None else ControllerState(
            enabled=True, approval_epoch=3,
            daily_quota=DailyQuota("2026-11-08", 1, "0.40"),
        )
        self.revision = 1
        self.writes = 0

    def read(self):
        return self.state, str(self.revision)

    def compare_and_swap(self, revision, state):
        self.writes += 1
        if revision != str(self.revision):
            return False
        self.state = ControllerState.decode(state.encode())
        self.revision += 1
        return True


class SyntheticSource:
    def __init__(self, **changes):
        self.calls = 0
        self.readings = replace(IndependentReadings(
            github_checked_at=NOW, active_publishers=0, queued_publishers=0,
            funded_openrouter_checked_at=NOW, funded_openrouter_ok=True,
            perplexity_checked_at=NOW, perplexity_spend_ok=True,
            journal_checked_at=NOW, prior_publication_certain=True,
            operator_approval_confirmed=True, automation_switch_confirmed=True,
            is_first_attempt=True,
            daily_spend=DailySpendEvidence(
                NOW.date(), NOW, "0.10", "1.00", "0.40", 1, 10, True,
            ),
        ), **changes)

    def read(self):
        self.calls += 1
        return self.readings


def reserve(store, source=None, now=NOW):
    return reserve_only_after_preflight(
        store, source if source is not None else SyntheticSource(),
        now=now, approval_epoch=3, explicit_control_write_approved=True,
    )


def proof(state, **changes):
    return replace(OutcomeEvidence(
        intent_id=state.pending.intent_id, github_run_id=123,
        run_completed=True, workflow_conclusion="success",
        workflow_path="autonomous_one_worker.yaml",
        run_intent_id=state.pending.intent_id,
        run_confirmed_post_id=999001, journal_confirmed_post_id=999001,
        metaculus_confirmed_post_id=999001, independently_checked_billing=True,
        github_finished_at=NOW, journal_checked_at=NOW,
        metaculus_checked_at=NOW, billing_checked_at=NOW,
    ), **changes)


def budget():
    return PerplexityAutoRunBudget(
        approved=True, balance_usd="10.00", run_cap_usd="0.40",
        verified_at="2026-11-08T12:30:00Z", now=NOW,
        auto_recharge_confirmed_off=True,
    )


class AutonomousRuntimeOfflineTests(unittest.TestCase):
    def test_disabled_controller_and_preview_never_write(self):
        source = SyntheticSource()
        disabled = MemoryControl(ControllerState())
        result = evaluate_offline_tick(disabled, source, now=NOW, approval_epoch=0)
        self.assertFalse(result.allowed)
        self.assertEqual(source.calls, 0)
        store = MemoryControl()
        result = evaluate_offline_tick(store, source, now=NOW, approval_epoch=3)
        self.assertTrue(result.allowed)
        self.assertFalse(result.durably_reserved)
        self.assertIsNone(store.state.pending)
        self.assertEqual(store.writes + disabled.writes, 0)

    def test_real_queue_reader_keeps_old_queued_run_blocking(self):
        calls = []

        class FakeGitHub:
            def open(self, request, timeout):
                calls.append(request.get_method())
                status = parse_qs(urlsplit(request.full_url).query)["status"][0]
                rows = [{
                    "id": KNOWN_STUCK_RUN_ID, "status": "queued",
                    "workflow_id": PUBLISHING_WORKFLOW_ID,
                    "path": ".github/workflows/run_bot_on_tournament.yaml",
                }] if status == "queued" else []
                reply = io.BytesIO(json.dumps({
                    "total_count": len(rows), "workflow_runs": rows,
                }).encode())
                reply.status = 200
                return reply

        reader = GitHubPublisherQueueReader(
            token="SYNTHETIC_NOT_A_CREDENTIAL", opener=FakeGitHub(), clock=lambda: NOW,
        )
        queue = GitHubQueuePreflightSource(reader).read()
        self.assertEqual(queue.queued_publishers, 1)
        # Even fabricated green funding/approval flags cannot bypass this queue.
        source = SyntheticSource(queued_publishers=queue.queued_publishers)
        store = MemoryControl()
        with self.assertRaises(AutonomousPreflightRejected):
            reserve(store, source)
        self.assertEqual(store.writes, 0)
        self.assertEqual(calls, ["GET"] * 5)  # all synthetic, no network

    def test_stale_future_and_missing_sources_fail_before_cas(self):
        for field in (
            "github_checked_at", "funded_openrouter_checked_at",
            "perplexity_checked_at", "journal_checked_at",
        ):
            for stamp in (None, NOW - timedelta(minutes=6), NOW + timedelta(seconds=1)):
                with self.subTest(field=field, stamp=stamp):
                    store = MemoryControl()
                    with self.assertRaises(AutonomousPreflightRejected):
                        reserve(store, SyntheticSource(**{field: stamp}))
                    self.assertEqual(store.writes, 0)

    def test_missing_billing_cannot_be_replaced_by_funding(self):
        for changes in ({"perplexity_spend_ok": False}, {"daily_spend": None},
                        {"prior_publication_certain": False}):
            with self.subTest(changes=changes):
                store = MemoryControl()
                with self.assertRaises(AutonomousPreflightRejected):
                    reserve(store, SyntheticSource(**changes))
                self.assertEqual(store.writes, 0)

    def test_observed_spend_floor_survives_later_lower_reading(self):
        store = MemoryControl()
        store.state = replace(store.state, daily_quota=DailyQuota("2026-11-08", 1, "0.10"))
        source = SyntheticSource()
        source.readings = replace(source.readings, daily_spend=replace(
            source.readings.daily_spend, charged_usd="0.60",
        ))
        self.assertTrue(reserve(store, source).allowed)
        self.assertEqual(store.state.daily_quota.reserved_usd, "1.00")

        # Model an independently reconciled intent in MEMORY ONLY. A subsequent
        # source correction must not erase the higher previously observed cost.
        store.state = replace(store.state, pending=None)
        later = NOW + timedelta(minutes=15)
        source = SyntheticSource(**{
            name: later for name in (
                "github_checked_at", "funded_openrouter_checked_at",
                "perplexity_checked_at", "journal_checked_at",
            )
        })
        source.readings = replace(source.readings, daily_spend=replace(
            source.readings.daily_spend, checked_at=later, attempted_dispatches=2,
        ))
        result = reserve(store, source, later)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "durable_daily_budget_exhausted")
        self.assertEqual(store.writes, 1)

    def test_existing_reserve_floor_is_never_refunded(self):
        store = MemoryControl()
        self.assertTrue(reserve(store).allowed)
        self.assertEqual(store.state.daily_quota.reserved_usd, "0.80")
        self.assertEqual(store.state.daily_quota.attempts, 2)

    def test_ambiguous_committed_cas_never_retries_or_reuses_intent(self):
        store, source = MemoryControl(), SyntheticSource()
        commit = store.compare_and_swap

        def lost_ack(revision, state):
            commit(revision, state)
            raise TimeoutError("synthetic lost CAS acknowledgment")

        store.compare_and_swap = lost_ack
        with self.assertRaises(TimeoutError):
            reserve(store, source)
        result = reserve(store, source)
        self.assertFalse(result.allowed)
        self.assertIn("reconciliation", result.reason)
        self.assertEqual(store.writes, 1)
        self.assertEqual(source.calls, 1)

    def test_emergency_stop_preserves_pending_and_checks_readback(self):
        store = MemoryControl()
        reserve(store)
        pending = store.state.pending
        stopped = persist_emergency_stop(store, operator_stop_authorized=True)
        self.assertIn("off_confirmed", stopped.reason)
        self.assertFalse(store.state.enabled)
        self.assertEqual(store.state.pending, pending)
        store = MemoryControl()
        store.compare_and_swap = lambda *args: True  # fake ACK without persistence
        with self.assertRaises(KillSwitchUncertain):
            persist_emergency_stop(store, operator_stop_authorized=True)

    def test_settlement_cannot_predate_pending_reservation(self):
        store = MemoryControl()
        reserve(store)
        result = assess_settlement(store.state, proof(
            store.state, github_finished_at=NOW - timedelta(seconds=1),
        ), now=NOW)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "workflow_completion_predates_intent")

    def test_settlement_boundary_accepts_same_or_later_completion_without_mutation(self):
        store = MemoryControl()
        reserve(store)
        before = store.state.encode()
        for finished in (NOW, NOW + timedelta(seconds=1)):
            result = assess_settlement(store.state, proof(
                store.state, github_finished_at=finished,
                journal_checked_at=finished, metaculus_checked_at=finished,
                billing_checked_at=finished,
            ), now=finished)
            self.assertTrue(result.allowed)
            self.assertEqual(store.state.encode(), before)

    def test_no_candidate_failed_or_mismatched_result_does_not_clear_pending(self):
        store = MemoryControl()
        reserve(store)
        before = store.state.encode()
        for changes in (
            {"run_confirmed_post_id": None}, {"workflow_conclusion": "failure"},
            {"journal_confirmed_post_id": 999002}, {"run_intent_id": "unrelated"},
            {"independently_checked_billing": False},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(assess_settlement(
                    store.state, proof(store.state, **changes), now=NOW,
                ).allowed)
                self.assertEqual(store.state.encode(), before)

    def test_unknown_research_cost_retains_attempt_and_stops(self):
        for response in (None, {}, {"usage": {"cost": {"total_cost": "NaN"}}}):
            with self.subTest(response=response):
                guard = budget()
                guard.begin_attempt()
                with self.assertRaises(PerplexityBudgetUnavailable):
                    guard.record_response(response)
                with self.assertRaises(PerplexityBudgetUnavailable):
                    guard.begin_attempt()
                observed = guard.cost_observation()
                self.assertEqual(observed.unpriced_attempts, 1)
                self.assertTrue(observed.outcome_uncertain)
                self.assertFalse(observed.eligible_for_automatic_daily_reset)

    def test_two_requests_cannot_silently_fund_two_research_factcheck_pairs(self):
        guard = budget()
        for _ in range(2):
            guard.begin_attempt()
            guard.record_response({"usage": {"cost": {"total_cost": "0.05"}}})
        with self.assertRaises(PerplexityBudgetUnavailable):
            guard.begin_attempt()
        self.assertEqual(guard.attempts, 2)

    def test_synthetic_publication_restart_never_repeats_confirmed_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "SYNTHETIC-ONLY.sqlite3"
            journal, writes = PublicationStore(path), []
            key, _, created = journal.prepare_for_run(
                "synthetic-resource/initial", "synthetic report", [
                    {"kind": "forecast"}, {"kind": "comment", "is_private": True},
                ], True,
            )
            self.assertTrue(created)
            Publisher(journal, writes.append).drain(key)
            restarted = PublicationStore(path)
            recovered, _, _ = restarted.select_for_run("synthetic-resource/new-run", True)
            Publisher(restarted, writes.append).drain(recovered)
            self.assertEqual([entry["kind"] for entry in writes], ["forecast", "comment"])

    def test_synthetic_lost_publication_ack_blocks_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "SYNTHETIC-ONLY.sqlite3"
            journal, writes = PublicationStore(path), []
            journal.prepare("synthetic-resource/initial", "synthetic report", [{"kind": "forecast"}])

            def lost_ack(operation):
                writes.append(operation)
                raise TimeoutError("synthetic sink accepted, acknowledgment lost")

            with self.assertRaises(PublicationUncertain):
                Publisher(journal, lost_ack).drain("synthetic-resource/initial")
            with self.assertRaises(PublicationUncertain):
                Publisher(PublicationStore(path), writes.append).drain("synthetic-resource/initial")
            self.assertEqual(len(writes), 1)

    def test_synthetic_reservation_research_publication_and_settlement(self):
        control, source = MemoryControl(), SyntheticSource()
        self.assertTrue(reserve(control, source).durably_reserved)
        guard = budget()
        for _ in range(2):
            guard.begin_attempt()
            guard.record_response({"usage": {"cost": {"total_cost": "0.05"}}})
        # Response prices alone cannot serve as independent billing proof.
        self.assertFalse(guard.cost_observation().provider_invoice_verified)
        with tempfile.TemporaryDirectory() as directory:
            journal = PublicationStore(Path(directory) / "SYNTHETIC-ONLY.sqlite3")
            key = "synthetic-resource/" + control.state.pending.intent_id
            journal.prepare(key, "synthetic report", [
                {"kind": "forecast"}, {"kind": "comment", "is_private": True},
            ])
            sink = []
            Publisher(journal, sink.append).drain(key)
            self.assertEqual(len(sink), 2)
            self.assertTrue(all(step["state"] == "confirmed" for step in journal.steps(key)))
            # Independent history/billing are explicitly SYNTHETIC attestations.
            result = assess_settlement(control.state, proof(control.state), now=NOW)
            self.assertTrue(result.allowed)
        self.assertIsNotNone(control.state.pending)
        self.assertFalse(reserve(control, source).allowed)
        self.assertEqual(control.writes, 1)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AutonomousRuntimeOfflineTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"Offline audit guard: {len(NETWORK_ATTEMPTS)} network attempts.")
    sys.exit(0 if result.wasSuccessful() and not NETWORK_ATTEMPTS else 1)

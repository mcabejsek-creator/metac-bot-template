"""Synthetic tests: no live provider, GitHub, or publication traffic."""

import base64
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import urllib.error

from .support import OfflineTestCase
from .test_scheduler_policy import accepted
from scheduler_policy import DailySpendEvidence
from dispatch_intent_controller import (
    ControllerState, DailyQuota, InvalidControlState, PendingIntent, propose_reservation,
    reserve_with_store, disable_controller,
)
from github_control_store import (
    GitHubContentsControlStore, ControlStoreUnavailable, TARGET_BRANCH,
    TARGET_PATH, API_URL,
)


NOW = datetime(2026, 11, 8, 12, 30, 0, tzinfo=timezone.utc)
SHA = "a" * 40
APPROVED = ControllerState(
    enabled=True, approval_epoch=3,
    daily_quota=DailyQuota(day_utc=NOW.date().isoformat(), attempts=1, reserved_usd="0.40"),
)
ROOT = Path(__file__).resolve().parents[1]


def daily_at(when=NOW):
    return DailySpendEvidence(
        day_utc=when.date(), checked_at=when,
        charged_usd="0.10", approved_day_cap_usd="1.00",
        approved_run_reserve_usd="0.40",
        attempted_dispatches=1, max_daily_dispatches=2,
        complete=True,
    )


def snapshot(**changes):
    values = {
        "now": NOW,
        "github_status_checked_at": NOW,
        "last_dispatch_at": NOW - timedelta(minutes=15),
        "daily_spend": daily_at(),
    }
    values.update(changes)
    return accepted(**values)


class FakeStore:
    def __init__(self, state=APPROVED):
        self.state = state
        self.revision = SHA
        self.calls = []

    def read(self):
        self.calls.append("GET")
        return self.state, self.revision

    def compare_and_swap(self, expected_revision, new_state):
        self.calls.append("CAS")
        if expected_revision != self.revision:
            return False
        self.revision = "b" * 40
        self.state = new_state
        return True


class Reply:
    def __init__(self, body, status=200):
        self.body = json.dumps(body).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return self.body


class FakeGitHub:
    def __init__(self, initial=ControllerState()):
        self.state = initial
        self.calls = []
        self.revision = SHA
        self.next_status = None

    def open(self, req, timeout):
        self.calls.append((req.get_method(), req.full_url, timeout))
        if self.next_status is not None:
            code = self.next_status
            self.next_status = None
            raise urllib.error.HTTPError(req.full_url, code, "synthetic", {}, None)
        if req.get_method() == "GET":
            return Reply({
                "type": "file",
                "path": TARGET_PATH,
                "sha": self.revision,
                "encoding": "base64",
                "content": base64.b64encode(self.state.encode().encode()).decode(),
            })
        if req.get_method() == "PUT":
            payload = json.loads(req.data)
            if payload["sha"] != self.revision or payload["branch"] != TARGET_BRANCH:
                raise urllib.error.HTTPError(req.full_url, 409, "synthetic", {}, None)
            self.state = ControllerState.decode(base64.b64decode(payload["content"]))
            self.revision = "b" * 40
            return Reply({"content": {"path": TARGET_PATH, "sha": self.revision}})
        raise AssertionError("Unexpected outbound network operation")


class ControllerIntentTests(OfflineTestCase):
    def test_default_disables_all_automatic_dispatch(self):
        state = ControllerState()
        self.assertFalse(state.enabled)
        self.assertEqual(state.approval_epoch, 0)
        self.assertIsNone(state.pending)
        self.assertFalse(propose_reservation(state, snapshot(), approval_epoch=0).allowed)

    def test_strict_json_roundtrip_and_unknown_fields_denied(self):
        self.assertEqual(ControllerState.decode(ControllerState().encode()), ControllerState())
        broken = json.loads(ControllerState().encode())
        for change in ({"unknown": True}, {"version": 1}, {"version": 3}, {"enabled": "false"},
                       {"approval_epoch": True}, {"last_slot": "2026-11-08T12:30:00Z"}):
            bad = {**broken, **change}
            with self.subTest(change=change), self.assertRaises(InvalidControlState):
                ControllerState.decode(json.dumps(bad))
        with self.assertRaises(InvalidControlState):
            ControllerState.decode(b"{" + b"x" * 4096 + b"}")
        with self.assertRaises(InvalidControlState):
            ControllerState.decode("")

    def test_wrong_approval_epoch_and_no_verified_snapshot_denied(self):
        for epoch in (0, 1, 4, True):
            with self.subTest(epoch=epoch):
                self.assertFalse(propose_reservation(APPROVED, snapshot(), approval_epoch=epoch).allowed)
        for change in (
            {"active_publisher_runs": 1},
            {"queued_publisher_runs": 1},
            {"sponsor_budget_guard_available": False},
            {"perplexity_spend_guard_available": False},
            {"previous_publication_outcome_certain": False},
            {"activation_switch_enabled": False},
        ):
            self.assertFalse(propose_reservation(APPROVED, snapshot(), approval_epoch=3).allowed
                             if not change else
                             propose_reservation(APPROVED, accepted(now=NOW,
                                github_status_checked_at=NOW,
                                last_dispatch_at=NOW-timedelta(minutes=15),
                                **change), approval_epoch=3).allowed)

    def test_daily_quota_missing_denies_before_any_control_cas(self):
        store = FakeStore()
        attempt = reserve_with_store(
            store, snapshot(daily_spend=None), approval_epoch=3,
        )
        self.assertFalse(attempt.allowed)
        self.assertEqual(attempt.reason, "verified_daily_spend_unavailable")
        self.assertEqual(store.calls, ["GET"])


    def test_success_reserves_cost_and_attempt_atomically_with_intent(self):
        before = APPROVED
        outcome = propose_reservation(before, snapshot(), approval_epoch=3)
        self.assertTrue(outcome.allowed)
        self.assertIsNone(before.pending)
        self.assertEqual(before.daily_quota.attempts, 1)
        self.assertEqual(before.daily_quota.reserved_usd, "0.40")
        self.assertEqual(outcome.state.daily_quota.attempts, 2)
        self.assertEqual(outcome.state.daily_quota.reserved_usd, "0.80")
        self.assertEqual(
            ControllerState.decode(outcome.state.encode()).daily_quota,
            outcome.state.daily_quota,
        )

    def test_missing_untrusted_or_stale_daily_quota_never_reserved(self):
        cases = (
            (replace(APPROVED, daily_quota=None), snapshot(), "durable_daily_quota_not_initialized"),
            (replace(APPROVED, daily_quota=DailyQuota(
                day_utc="2026-11-07", attempts=1, reserved_usd="0.40",
            )), snapshot(), "durable_daily_quota_day_mismatch"),
            (APPROVED, snapshot(daily_spend=replace(daily_at(), attempted_dispatches=0)),
             "audited_and_durable_daily_counts_disagree"),
        )
        for state, snap, reason in cases:
            with self.subTest(reason=reason):
                proposal = propose_reservation(state, snap, approval_epoch=3)
                self.assertFalse(proposal.allowed)
                self.assertEqual(proposal.reason, reason)
                self.assertIs(proposal.state, state)

    def test_lifetime_of_reservations_not_reset_by_lower_provider_cost(self):
        # The upstream snapshot alone would allow spending 0.30+0.40<=1.00,
        # but our durable 0.75 reserve must force a denial.
        state = replace(APPROVED, daily_quota=DailyQuota(
            day_utc=NOW.date().isoformat(), attempts=1, reserved_usd="0.75",
        ))
        proposal = propose_reservation(
            state, snapshot(daily_spend=replace(
                daily_at(), charged_usd="0.30", approved_run_reserve_usd="0.40",
            )), approval_epoch=3,
        )
        self.assertFalse(proposal.allowed)
        self.assertEqual(proposal.reason, "durable_daily_budget_exhausted")

    def test_observed_charge_higher_than_reserved_usd_also_blocks(self):
        state = replace(APPROVED, daily_quota=DailyQuota(
            day_utc=NOW.date().isoformat(), attempts=1, reserved_usd="0.20",
        ))
        proposal = propose_reservation(
            state, snapshot(daily_spend=replace(
                daily_at(), charged_usd="0.70", approved_run_reserve_usd="0.40",
            )), approval_epoch=3,
        )
        self.assertEqual(proposal.reason, "daily_spend_allowance_exhausted")

    def test_existing_day_count_and_ambiguous_cas_never_replay(self):
        state = replace(APPROVED, daily_quota=DailyQuota(
            day_utc=NOW.date().isoformat(), attempts=2, reserved_usd="0.80",
        ))
        denied = propose_reservation(
            state, snapshot(daily_spend=replace(
                daily_at(), attempted_dispatches=2,
            )), approval_epoch=3,
        )
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "daily_dispatch_quota_exhausted")
        store = FakeStore()
        original = store.state.daily_quota
        def conflict(_revision, _state):
            store.calls.append("CAS-CONFLICT")
            return False
        store.compare_and_swap = conflict
        rejected = reserve_with_store(store, snapshot(), approval_epoch=3)
        self.assertFalse(rejected.allowed)
        self.assertEqual(store.state.daily_quota, original)

    def test_invalid_daily_state_and_old_version_fail_closed(self):
        original = json.loads(APPROVED.encode())
        for altered in (
            {"version": 1},
            {"daily_quota": None},
            {"daily_quota": {"day_utc": "2026-11-08", "attempts": "1",
                             "reserved_usd": "0.40"}},
            {"daily_quota": {"day_utc": "2026-11-08", "attempts": True,
                             "reserved_usd": "0.40"}},
            {"daily_quota": {"day_utc": "2026-02-30", "attempts": 1,
                             "reserved_usd": "0.40"}},
            {"daily_quota": {"day_utc": "2026-11-08", "attempts": 1,
                             "reserved_usd": "-1"}},
            {"daily_quota": {"day_utc": "2026-11-08", "attempts": 1,
                             "reserved_usd": "0.40", "extra": True}},
        ):
            with self.subTest(altered=altered):
                payload = {**original, **altered}
                if altered == {"daily_quota": None}:
                    # Disabled state with no quota is safe, but an enabled
                    # state must not be considered an approved dispatch.
                    state = ControllerState.decode(json.dumps(payload))
                    self.assertEqual(
                        propose_reservation(state, snapshot(), approval_epoch=3).reason,
                        "durable_daily_quota_not_initialized",
                    )
                else:
                    with self.assertRaises(InvalidControlState):
                        ControllerState.decode(json.dumps(payload))

    def test_valid_intent_is_pure_and_fixed_to_quarter_hour(self):
        result = propose_reservation(APPROVED, snapshot(), approval_epoch=3)
        self.assertTrue(result.allowed)
        self.assertEqual(result.reason, "needs_durable_compare_and_swap")
        self.assertIsNone(APPROVED.pending)
        self.assertEqual(result.state.pending.slot, "2026-11-08T12:30:00Z")
        self.assertEqual(result.state.pending.reserved_at, "2026-11-08T12:30:00Z")
        self.assertEqual(result.intent_id, result.state.pending.intent_id)
        self.assertEqual(ControllerState.decode(result.state.encode()), result.state)

    def test_reserved_or_uncertain_intent_always_blocks_replay(self):
        first = propose_reservation(APPROVED, snapshot(), approval_epoch=3)
        at_next_tick = snapshot(now=NOW+timedelta(minutes=16),
                                github_status_checked_at=NOW+timedelta(minutes=16),
                                daily_spend=daily_at(NOW+timedelta(minutes=16)),
                                last_dispatch_at=NOW)
        result = propose_reservation(first.state, at_next_tick, approval_epoch=3)
        self.assertFalse(result.allowed)
        self.assertIn("reconciliation", result.reason)

    def test_same_quarter_slot_replays_denied_even_if_no_pending(self):
        first = propose_reservation(APPROVED, snapshot(), approval_epoch=3)
        cleared = replace(first.state, pending=None)
        later = snapshot(now=NOW+timedelta(minutes=1),
                         github_status_checked_at=NOW+timedelta(minutes=1),
                         daily_spend=daily_at(NOW+timedelta(minutes=1)),
                         last_dispatch_at=None)
        result = propose_reservation(cleared, later, approval_epoch=3)
        self.assertFalse(result.allowed)

    def test_persistent_spacing_uses_store_not_untrusted_caller(self):
        first = propose_reservation(APPROVED, snapshot(), approval_epoch=3)
        cleared = replace(first.state, pending=None)
        later = snapshot(now=NOW+timedelta(minutes=14),
                         github_status_checked_at=NOW+timedelta(minutes=14),
                         daily_spend=daily_at(NOW+timedelta(minutes=14)),
                         last_dispatch_at=None)
        self.assertFalse(propose_reservation(cleared, later, approval_epoch=3).allowed)

    def test_real_cas_success_then_subsequent_dispatch_denied(self):
        store = FakeStore()
        first = reserve_with_store(store, snapshot(), approval_epoch=3)
        self.assertTrue(first.allowed)
        self.assertEqual(first.reason, "durably_reserved_not_dispatched")
        self.assertEqual(store.calls, ["GET", "CAS"])
        second = reserve_with_store(store, snapshot(), approval_epoch=3)
        self.assertFalse(second.allowed)
        self.assertEqual(store.calls, ["GET", "CAS", "GET"])
        self.assertEqual(store.state.pending.intent_id, first.intent_id)

    def test_stale_revision_conflict_fails_closed_and_no_retry(self):
        store = FakeStore()
        stale_state, stale_rev = store.read()
        a = propose_reservation(stale_state, snapshot(), approval_epoch=3)
        b = propose_reservation(stale_state, snapshot(), approval_epoch=3)
        self.assertTrue(store.compare_and_swap(stale_rev, a.state))
        self.assertFalse(store.compare_and_swap(stale_rev, b.state))
        self.assertEqual(store.calls.count("CAS"), 2)

    def test_unknown_cas_result_or_io_error_cannot_authorize_dispatch(self):
        store = FakeStore()
        store.compare_and_swap = lambda *_: None
        self.assertFalse(reserve_with_store(store, snapshot(), approval_epoch=3).allowed)
        store.read = lambda: (_ for _ in ()).throw(OSError("unavailable"))
        with self.assertRaises(OSError):
            reserve_with_store(store, snapshot(), approval_epoch=3)

    def test_kill_switch_preserves_pending_and_disables_future_launch(self):
        first = propose_reservation(APPROVED, snapshot(), approval_epoch=3)
        disabled = disable_controller(first.state)
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.pending, first.state.pending)
        self.assertFalse(propose_reservation(disabled, snapshot(), approval_epoch=3).allowed)

    def test_github_adapter_read_and_write_stays_on_control_branch(self):
        fake = FakeGitHub(APPROVED)
        store = GitHubContentsControlStore(token="OFFLINE_FAKE_TOKEN", opener=fake)
        state, rev = store.read()
        self.assertEqual(state, APPROVED)
        self.assertEqual(rev, SHA)
        result = reserve_with_store(store, snapshot(), approval_epoch=3)
        self.assertTrue(result.allowed)
        self.assertEqual([v[0] for v in fake.calls], ["GET", "GET", "PUT"])
        self.assertTrue(all(v[1].startswith(API_URL) for v in fake.calls))
        self.assertTrue(all("metaculus-publication-state" not in v[1] for v in fake.calls))
        self.assertTrue(all("workflow_dispatch" not in v[1] for v in fake.calls))
        self.assertIsNotNone(fake.state.pending)

    def test_github_missing_control_branch_fails_closed(self):
        fake = FakeGitHub()
        fake.next_status = 404
        store = GitHubContentsControlStore(token="OFFLINE_FAKE_TOKEN", opener=fake)
        with self.assertRaises(ControlStoreUnavailable):
            reserve_with_store(store, snapshot(), approval_epoch=3)
        self.assertEqual(len(fake.calls), 1)

    def test_github_cas_conflict_does_not_retry(self):
        fake = FakeGitHub(APPROVED)
        store = GitHubContentsControlStore(token="OFFLINE_FAKE_TOKEN", opener=fake)
        fake.next_status = None
        state, rev = store.read()
        fake.revision = "c"*40
        self.assertFalse(store.compare_and_swap(rev, state))
        self.assertEqual([x[0] for x in fake.calls], ["GET", "PUT"])

    def test_github_unknown_write_does_not_authorize(self):
        class TimedOut(FakeGitHub):
            def open(self, req, timeout):
                if req.get_method() == "PUT":
                    raise TimeoutError("network lost after request")
                return super().open(req, timeout)
        fake = TimedOut(APPROVED)
        store = GitHubContentsControlStore(token="OFFLINE_FAKE_TOKEN", opener=fake)
        with self.assertRaises(ControlStoreUnavailable):
            reserve_with_store(store, snapshot(), approval_epoch=3)

    def test_prod_has_no_automatic_cli_or_trigger(self):
        source = (ROOT / "main.py").read_text()
        publisher = (ROOT / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        self.assertNotIn('parser.add_argument("--automatic-one"', source)
        self.assertNotIn("--automatic-one", publisher)
        self.assertNotIn("\n  schedule:", publisher)
        self.assertIn("default: DO_NOT_RUN", publisher)
        self.assertFalse((ROOT / ".github/workflows/autonomous_dispatch.yaml").exists())


if __name__ == "__main__":
    import unittest
    unittest.main()

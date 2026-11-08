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
from dispatch_intent_controller import (
    ControllerState, InvalidControlState, PendingIntent, propose_reservation,
    reserve_with_store, disable_controller,
)
from github_control_store import (
    GitHubContentsControlStore, ControlStoreUnavailable, TARGET_BRANCH,
    TARGET_PATH, API_URL,
)


NOW = datetime(2026, 11, 8, 12, 30, 0, tzinfo=timezone.utc)
SHA = "a" * 40
APPROVED = ControllerState(enabled=True, approval_epoch=3)
ROOT = Path(__file__).resolve().parents[1]


def snapshot(**changes):
    return accepted(now=NOW, github_status_checked_at=NOW,
                    last_dispatch_at=NOW - timedelta(minutes=15), **changes)


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
        for change in ({"unknown": True}, {"version": 2}, {"enabled": "false"},
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
        at_next_tick = accepted(now=NOW+timedelta(minutes=16),
                                github_status_checked_at=NOW+timedelta(minutes=16),
                                last_dispatch_at=NOW)
        result = propose_reservation(first.state, at_next_tick, approval_epoch=3)
        self.assertFalse(result.allowed)
        self.assertIn("reconciliation", result.reason)

    def test_same_quarter_slot_replays_denied_even_if_no_pending(self):
        first = propose_reservation(APPROVED, snapshot(), approval_epoch=3)
        cleared = replace(first.state, pending=None)
        later = accepted(now=NOW+timedelta(minutes=1),
                         github_status_checked_at=NOW+timedelta(minutes=1),
                         last_dispatch_at=None)
        result = propose_reservation(cleared, later, approval_epoch=3)
        self.assertFalse(result.allowed)

    def test_persistent_spacing_uses_store_not_untrusted_caller(self):
        first = propose_reservation(APPROVED, snapshot(), approval_epoch=3)
        cleared = replace(first.state, pending=None)
        later = accepted(now=NOW+timedelta(minutes=14),
                         github_status_checked_at=NOW+timedelta(minutes=14),
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

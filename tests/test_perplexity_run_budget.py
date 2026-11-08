"""Synthetic no-network cost/attempt tests for dormant automatic Perplexity research."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import json
import urllib.error

from .support import OfflineTestCase
from perplexity_run_budget import (
    PerplexityAutoRunBudget, PerplexityBudgetUnavailable,
    MAX_AUTO_AGENT_POST_ATTEMPTS, MAX_SUPERVISED_AGENT_POST_ATTEMPTS,
    MINIMUM_WALLET_RESERVE_USD,
)
from research_safety import call_agent_once


NOW = datetime(2026, 11, 8, 14, 0, tzinfo=timezone.utc)
STAMP = "2026-11-08T13:00:00Z"


def permitted(**changes):
    opts = {
        "approved": True,
        "balance_usd": "14.09",
        "verified_at": STAMP,
        "run_cap_usd": "1.00",
        "auto_recharge_confirmed_off": True,
        "now": NOW,
    }
    opts.update(changes)
    return PerplexityAutoRunBudget(**opts)


def agent_data(cost=0.07):
    return {
        "status": "completed",
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "Verified research."}
        ]}],
        "usage": {"cost": {"total_cost": cost}},
    }


class AgentReply:
    def __init__(self, data):
        self.body = json.dumps(data).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return self.body


class PerplexityRunBudgetTests(OfflineTestCase):
    def test_default_missing_or_unapproved_operator_attestation_denies(self):
        with self.assertRaises(PerplexityBudgetUnavailable):
            PerplexityAutoRunBudget()
        for field in ("approved", "auto_recharge_confirmed_off"):
            for value in (False, None, "", "YES", 1):
                with self.subTest(field=field, value=value), self.assertRaises(PerplexityBudgetUnavailable):
                    permitted(**{field: value})

    def test_balance_is_a_snapshot_not_a_live_api_proof(self):
        self.assertEqual(MAX_AUTO_AGENT_POST_ATTEMPTS, 2)
        self.assertEqual(str(MINIMUM_WALLET_RESERVE_USD), "2.00")
        for fields in (
            {"verified_at": "2026-11-07T12:00:00Z"},
            {"verified_at": "2026-11-08T15:00:00Z"},
            {"verified_at": "2026-11-08T12:00:00"},
            {"verified_at": "nonsense"},
            {"verified_at": None},
            {"now": NOW.replace(tzinfo=None)},
            {"balance_usd": None},
            {"balance_usd": "NaN"},
            {"balance_usd": "-1"},
            {"balance_usd": "2.99", "run_cap_usd": "1.00"},
            {"run_cap_usd": "0"},
            {"run_cap_usd": "100"},
            {"run_cap_usd": "Infinity"},
        ):
            with self.subTest(fields=fields), self.assertRaises(PerplexityBudgetUnavailable):
                permitted(**fields)

    def test_both_funded_attempts_allowed_then_third_denied(self):
        guard = permitted()
        for _ in range(2):
            guard.begin_attempt()
            guard.record_response(agent_data(0.11))
        self.assertEqual(guard.attempts, 2)
        self.assertEqual(guard.observed_cost_usd, "0.22")
        with self.assertRaises(PerplexityBudgetUnavailable):
            guard.begin_attempt()

    def test_cost_observation_separates_returned_prices_from_actual_billing(self):
        guard = permitted()
        initial = guard.cost_observation()
        self.assertEqual(initial.attempted_requests, 0)
        self.assertEqual(initial.priced_responses, 0)
        self.assertEqual(initial.unpriced_attempts, 0)
        self.assertFalse(initial.provider_invoice_verified)
        self.assertFalse(initial.eligible_for_automatic_daily_reset)

        # The paid attempt is counted BEFORE outbound transmission.
        guard.begin_attempt()
        inflight = guard.cost_observation()
        self.assertEqual(inflight.attempted_requests, 1)
        self.assertEqual(inflight.unpriced_attempts, 1)
        self.assertTrue(inflight.request_in_flight)
        self.assertFalse(inflight.eligible_for_automatic_daily_reset)

        guard.record_response(agent_data(0.0375))
        guard.begin_attempt()
        guard.record_response(agent_data(0.0125))
        recorded = guard.cost_observation()
        self.assertEqual(recorded.attempted_requests, 2)
        self.assertEqual(recorded.priced_responses, 2)
        self.assertEqual(recorded.unpriced_attempts, 0)
        self.assertEqual(recorded.response_costs_usd, ("0.0375", "0.0125"))
        self.assertEqual(recorded.total_response_cost_usd, "0.0500")
        self.assertFalse(recorded.request_in_flight)
        self.assertFalse(recorded.outcome_uncertain)
        self.assertFalse(recorded.provider_invoice_verified)
        self.assertFalse(recorded.eligible_for_automatic_daily_reset)

    def test_timeout_keeps_unpriced_attempt_and_cannot_claim_zero_cost(self):
        guard = permitted()
        guard.begin_attempt()
        guard.record_response(agent_data(0.02))
        guard.begin_attempt()
        guard.abandon_unknown_response()
        observation = guard.cost_observation()
        self.assertEqual(observation.attempted_requests, 2)
        self.assertEqual(observation.priced_responses, 1)
        self.assertEqual(observation.unpriced_attempts, 1)
        self.assertEqual(observation.total_response_cost_usd, "0.02")
        self.assertTrue(observation.outcome_uncertain)
        self.assertFalse(observation.provider_invoice_verified)
        with self.assertRaises(PerplexityBudgetUnavailable):
            guard.begin_attempt()

    def test_missing_cost_stays_unpriced_and_prevents_further_billing(self):
        guard = permitted()
        guard.begin_attempt()
        with self.assertRaises(PerplexityBudgetUnavailable):
            guard.record_response({"status": "completed", "usage": {"cost": {}}})
        observation = guard.cost_observation()
        self.assertEqual(observation.attempted_requests, 1)
        self.assertEqual(observation.priced_responses, 0)
        self.assertEqual(observation.unpriced_attempts, 1)
        self.assertTrue(observation.outcome_uncertain)
        self.assertEqual(observation.total_response_cost_usd, "0")
        self.assertFalse(observation.eligible_for_automatic_daily_reset)

    def test_over_run_cost_preserves_observed_charge_even_when_denied(self):
        guard = permitted(run_cap_usd="0.10")
        guard.begin_attempt()
        with self.assertRaises(PerplexityBudgetUnavailable):
            guard.record_response(agent_data(0.19))
        observation = guard.cost_observation()
        self.assertEqual(observation.response_costs_usd, ("0.19",))
        self.assertEqual(observation.total_response_cost_usd, "0.19")
        self.assertEqual(observation.unpriced_attempts, 0)
        self.assertTrue(observation.outcome_uncertain)
        self.assertFalse(observation.eligible_for_automatic_daily_reset)

    def test_cost_observation_cannot_expose_raw_research_or_credentials(self):
        guard = permitted()
        response = agent_data("0.01")
        response["output"][0]["content"][0]["text"] = "PRIVATE PROMPT / RESEARCH"
        response["api_key"] = "SYNTHETIC_SECRET"
        guard.begin_attempt()
        guard.record_response(response)
        result = repr(guard.cost_observation())
        self.assertNotIn("PRIVATE PROMPT", result)
        self.assertNotIn("SYNTHETIC_SECRET", result)
        self.assertEqual(guard.cost_observation().response_costs_usd, ("0.01",))

    def test_cost_observation_cannot_mutate_guard_state(self):
        guard = permitted()
        guard.begin_attempt()
        guard.record_response(agent_data(0.01))
        before = guard.cost_observation()
        with self.assertRaises((AttributeError, TypeError)):
            before.attempted_requests = 0
        self.assertEqual(guard.cost_observation(), before)

    def test_manual_two_report_budget_allows_exactly_four_no_retry_attempts(self):
        self.assertEqual(MAX_SUPERVISED_AGENT_POST_ATTEMPTS, 4)
        guard = permitted(max_attempts=MAX_SUPERVISED_AGENT_POST_ATTEMPTS)
        for i in range(MAX_SUPERVISED_AGENT_POST_ATTEMPTS):
            guard.begin_attempt()
            guard.record_response(agent_data(0.08))
            self.assertEqual(guard.attempts, i + 1)
        self.assertEqual(guard.observed_cost_usd, "0.32")
        with self.assertRaises(PerplexityBudgetUnavailable):
            guard.begin_attempt()

    def test_attempt_limit_cannot_be_arbitrarily_inflated(self):
        for invalid in (None, "", 1, 3, 5, 10, True, 4.0, "4"):
            with self.subTest(invalid=invalid), self.assertRaises(PerplexityBudgetUnavailable):
                permitted(max_attempts=invalid)

    def test_supervised_fourth_unknown_or_overbudget_cost_stops_before_fifth(self):
        for mode in ("timeout", "overbudget"):
            guard = permitted(
                max_attempts=MAX_SUPERVISED_AGENT_POST_ATTEMPTS,
                run_cap_usd="0.30",
            )
            for _ in range(3):
                guard.begin_attempt()
                guard.record_response(agent_data(0.05))
            guard.begin_attempt()
            if mode == "timeout":
                guard.abandon_unknown_response()
            else:
                with self.assertRaises(PerplexityBudgetUnavailable):
                    guard.record_response(agent_data(0.50))
            with self.subTest(mode=mode), self.assertRaises(PerplexityBudgetUnavailable):
                guard.begin_attempt()

    def test_abandoned_attempt_stops_all_future_research(self):
        guard = permitted()
        guard.begin_attempt()
        guard.abandon_unknown_response()
        self.assertEqual(guard.attempts, 1)
        with self.assertRaises(PerplexityBudgetUnavailable):
            guard.begin_attempt()

    def test_missing_agent_cost_or_unexpected_schema_stop_followup(self):
        variants = (
            None, {}, {"usage": {}}, {"usage": {"cost": {}}},
            {"usage": {"cost": {"total_cost": None}}},
            {"usage": {"cost": {"total_cost": True}}},
            {"usage": {"cost": {"total_cost": float("nan")}}},
            {"usage": {"cost": {"total_cost": -0.01}}},
        )
        for bad in variants:
            guard = permitted()
            guard.begin_attempt()
            with self.subTest(bad=bad), self.assertRaises(PerplexityBudgetUnavailable):
                guard.record_response(bad)
            with self.assertRaises(PerplexityBudgetUnavailable):
                guard.begin_attempt()
            self.assertEqual(guard.attempts, 1)

    def test_reported_overage_already_charged_but_prevents_new_requests(self):
        guard = permitted(run_cap_usd="0.10")
        guard.begin_attempt()
        with self.assertRaises(PerplexityBudgetUnavailable):
            guard.record_response(agent_data(0.12))
        self.assertEqual(guard.observed_cost_usd, "0.12")
        with self.assertRaises(PerplexityBudgetUnavailable):
            guard.begin_attempt()

    def test_transport_performs_two_post_attempts_only(self):
        guard = permitted()
        seen = []
        def fake_urlopen(request, timeout):
            seen.append((request.get_method(), request.full_url))
            return AgentReply(agent_data())
        with patch("research_safety.time.monotonic", return_value=1), patch(
            "research_safety.urllib.request.urlopen", fake_urlopen
        ), patch.dict("os.environ", {"PERPLEXITY_API_KEY": "FAKE_TEST_ONLY"}):
            self.assertEqual(call_agent_once("research", 100, guard), "Verified research.")
            self.assertEqual(call_agent_once("fact-check", 100, guard), "Verified research.")
            with self.assertRaises(PerplexityBudgetUnavailable):
                call_agent_once("unexpected third", 100, guard)
        self.assertEqual(seen, [
            ("POST", "https://api.perplexity.ai/v1/agent"),
            ("POST", "https://api.perplexity.ai/v1/agent"),
        ])

    def test_network_timeout_may_bill_and_must_not_retry(self):
        guard = permitted()
        calls = []
        def broken_urlopen(request, timeout):
            calls.append(request.get_method())
            raise urllib.error.URLError("timeout")
        with patch("research_safety.time.monotonic", return_value=1), patch(
            "research_safety.urllib.request.urlopen", broken_urlopen
        ), patch.dict("os.environ", {"PERPLEXITY_API_KEY": "FAKE_TEST_ONLY"}):
            with self.assertRaises(urllib.error.URLError):
                call_agent_once("research", 100, guard)
            with self.assertRaises(PerplexityBudgetUnavailable):
                call_agent_once("fact-check", 100, guard)
        self.assertEqual(calls, ["POST"])
        self.assertEqual(guard.attempts, 1)

    def test_missing_cost_field_aborts_before_second_post(self):
        guard = permitted()
        seen = []
        def fake_urlopen(request, timeout):
            seen.append(request.get_method())
            return AgentReply({k: v for k, v in agent_data().items() if k != "usage"})
        with patch("research_safety.time.monotonic", return_value=1), patch(
            "research_safety.urllib.request.urlopen", fake_urlopen
        ), patch.dict("os.environ", {"PERPLEXITY_API_KEY": "FAKE_TEST_ONLY"}):
            with self.assertRaises(PerplexityBudgetUnavailable):
                call_agent_once("research", 100, guard)
            with self.assertRaises(PerplexityBudgetUnavailable):
                call_agent_once("fact-check", 100, guard)
        self.assertEqual(seen, ["POST"])

    def test_source_contains_no_external_balance_endpoint_or_activated_cron(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        main = (root / "main.py").read_text()
        workflow = (root / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        self.assertNotIn('parser.add_argument("--automatic-one"', main)
        self.assertNotIn("--automatic-one", workflow)
        self.assertNotIn("\n  schedule:", workflow)
        self.assertIn("default: DO_NOT_RUN", workflow)


if __name__ == "__main__":
    import unittest
    unittest.main()

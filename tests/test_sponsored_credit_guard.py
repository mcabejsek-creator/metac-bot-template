"""Synthetic GET-only tests for Metaculus-donated OpenRouter credit preflight."""

import json
from unittest.mock import patch

from .support import OfflineTestCase
from sponsored_credit_guard import (
    MINIMUM_SPONSORED_RESERVE_USD,
    METACULUS_FUNDED_KEY_ENDPOINT,
    SponsoredCreditUnavailable,
    verify_sponsored_openrouter_credit,
)


FAKE_TOKEN = "sk-or-v1-" + ("a" * 64) + "9fc"


class FakeReply:
    status = 200

    def __init__(self, data):
        self.body = json.dumps({"data": data}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return self.body


class FakeOpener:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request.get_method(), request.full_url, timeout))
        return FakeReply(self.data)


def funded_data(**changes):
    values = {
        "label": "sk-or-v1-abc...9fc",
        "limit": 100.0,
        "limit_remaining": 34.29481,
        "byok_usage": 65.70519,
        "include_byok_in_limit": True,
        "is_management_key": False,
    }
    values.update(changes)
    return values


class SponsoredCreditGuardTests(OfflineTestCase):
    def test_confirmed_sponsored_usage_exactly_one_get_and_no_model(self):
        opener = FakeOpener(funded_data())
        data = verify_sponsored_openrouter_credit(
            FAKE_TOKEN, expected_suffix="9fc", opener=opener,
        )
        self.assertEqual(opener.calls, [("GET", METACULUS_FUNDED_KEY_ENDPOINT, 15)])
        self.assertEqual(data["remaining_usd"], "34.29481")
        self.assertEqual(data["sponsored_byok_usage_usd"], "65.70519")
        self.assertNotIn("9fc", repr(data))
        self.assertNotIn("sk-or-", repr(data))

    def test_missing_or_personal_key_fails_without_any_api_request(self):
        for key, suffix in (
            ("", "9fc"), ("sk-or-v1-xyz", "9fc"), (FAKE_TOKEN, ""),
            (FAKE_TOKEN, "fff"), (FAKE_TOKEN, "9fc\n"),
            ("sk-or-v1-" + ("b" * 64) + "fff", "9fc"),
        ):
            opener = FakeOpener(funded_data())
            with self.subTest(key_suffix=suffix), self.assertRaises(SponsoredCreditUnavailable):
                verify_sponsored_openrouter_credit(key, expected_suffix=suffix, opener=opener)
            self.assertEqual(opener.calls, [])

    def test_low_balance_or_bad_metadata_fails_closed_after_one_get(self):
        variants = (
            {"limit_remaining": 4.99},
            {"limit_remaining": None},
            {"limit_remaining": -2},
            {"limit_remaining": float("nan")},
            {"limit_remaining": 101},
            {"limit": 10},
            {"include_byok_in_limit": False},
            {"is_management_key": True},
            {"byok_usage": None},
            {"label": "sk-or-v1-abc...fff"},
            {"label": "plain-text-secret"},
        )
        for invalid in variants:
            opener = FakeOpener(funded_data(**invalid))
            with self.subTest(invalid=tuple(invalid)), self.assertRaises(SponsoredCreditUnavailable):
                verify_sponsored_openrouter_credit(FAKE_TOKEN, expected_suffix="9fc", opener=opener)
            self.assertEqual(len(opener.calls), 1)

    def test_funding_reserve_boundary(self):
        self.assertEqual(str(MINIMUM_SPONSORED_RESERVE_USD), "5.00")
        at_limit = FakeOpener(funded_data(limit_remaining=5))
        self.assertEqual(verify_sponsored_openrouter_credit(
            FAKE_TOKEN, expected_suffix="9fc", opener=at_limit
        )["remaining_usd"], "5")
        below = FakeOpener(funded_data(limit_remaining=4.999))
        with self.assertRaises(SponsoredCreditUnavailable):
            verify_sponsored_openrouter_credit(
                FAKE_TOKEN, expected_suffix="9fc", opener=below,
            )

    def test_network_failure_does_not_retry(self):
        class BrokenOpener:
            calls = 0
            def open(self, *_args, **_kwargs):
                self.calls += 1
                raise OSError("blocked")
        opener = BrokenOpener()
        with self.assertRaises(SponsoredCreditUnavailable):
            verify_sponsored_openrouter_credit(
                FAKE_TOKEN, expected_suffix="9fc", opener=opener,
            )
        self.assertEqual(opener.calls, 1)


if __name__ == "__main__":
    import unittest
    unittest.main()

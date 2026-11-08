"""Real read-only source composition exercised against in-memory fake APIs.

No real GitHub/Metaculus/OpenRouter/Perplexity connection, NO model or POST.
"""

from datetime import datetime, timezone
from unittest.mock import patch

from .support import OfflineTestCase
from .test_github_publisher_queue_readonly import FakeGitHub, run as github_run, NOW
from .test_sponsored_credit_guard import (
    FAKE_TOKEN, FakeOpener, funded_data,
)
from .test_github_publication import (
    MemoryGitHub, GITHUB_KEY, METACULUS_KEY,
)
from github_publication import (
    GitHubContents, GitHubPublicationStore, JournalCodec,
    initial_legacy_frozen_manifest,
)
from github_queue_preflight import GitHubQueuePreflightSource
from github_publisher_queue_readonly import (
    GitHubPublisherQueueReader, KNOWN_STUCK_RUN_ID,
)
from authenticated_readonly_preflight import (
    EXPECTED_REPOSITORY, EXPECTED_JOURNAL_BRANCH, EXPECTED_JOURNAL_PATH,
    AuthenticatedJournalReader, SponsoredOpenRouterReader,
    ReadOnlyAuthenticatedSources, ReadOnlyPreflightUnavailable,
)
from autonomous_orchestrator import (
    AutonomousPreflightRejected, build_dispatch_snapshot,
)
from sponsored_credit_guard import SponsoredCreditUnavailable


def fake_journal(*, repository=EXPECTED_REPOSITORY,
                 branch=EXPECTED_JOURNAL_BRANCH,
                 path=EXPECTED_JOURNAL_PATH, damaged=False):
    codec = JournalCodec(
        METACULUS_KEY, [repository, branch, path, "default-bot"],
    )
    initial = (b"not a valid authenticated journal" if damaged
               else codec.encode(initial_legacy_frozen_manifest()))
    server = MemoryGitHub(initial)
    contents = GitHubContents(
        repository, branch, path, GITHUB_KEY, session=server,
    )
    return GitHubPublicationStore(contents, codec), server


def sources(*, queue=None, sponsor=None, journal=None):
    github = FakeGitHub(queue or {})
    opener = FakeOpener(sponsor if sponsor is not None else funded_data())
    store, server = fake_journal() if journal is None else journal
    reader = ReadOnlyAuthenticatedSources(
        github_source=GitHubQueuePreflightSource(
            GitHubPublisherQueueReader(
                token="SYNTHETIC_GITHUB_TOKEN", opener=github,
                clock=lambda: NOW,
            ),
        ),
        funded_source=SponsoredOpenRouterReader(
            donated_key=FAKE_TOKEN, expected_suffix="9fc",
            opener=opener, clock=lambda: NOW,
        ),
        journal_source=AuthenticatedJournalReader(
            store_factory=lambda: store, clock=lambda: NOW,
        ),
    )
    return reader, github, opener, server


class AuthenticatedReadOnlyPreflightTests(OfflineTestCase):
    def test_real_three_sources_read_without_any_write(self):
        reader, github, opener, server = sources()
        result = reader.read()
        self.assertEqual(result.github_checked_at, NOW)
        self.assertEqual(result.active_publishers, 0)
        self.assertEqual(result.queued_publishers, 0)
        self.assertEqual(result.funded_openrouter_checked_at, NOW)
        self.assertTrue(result.funded_openrouter_ok)
        self.assertEqual(result.journal_checked_at, NOW)
        self.assertFalse(result.prior_publication_certain)
        self.assertFalse(result.perplexity_spend_ok)
        self.assertFalse(result.operator_approval_confirmed)
        self.assertFalse(result.automation_switch_confirmed)
        self.assertFalse(result.is_first_attempt)
        self.assertEqual(len(github.calls), 5)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(len(server.calls), 2)  # root manifest read twice
        self.assertTrue(all(m == "GET" for m, _ in server.calls))
        self.assertTrue(all(m == "GET" for m, _, _ in github.calls))
        self.assertFalse(any("SYNTHETIC_GITHUB_TOKEN" in repr(x)
                             for x in (result,)))

    def test_stuck_legacy_run_short_circuits_both_other_sources(self):
        reader, github, opener, server = sources(queue={
            "queued": [github_run(KNOWN_STUCK_RUN_ID, "queued")]
        })
        result = reader.read()
        self.assertEqual(result.queued_publishers, 1)
        self.assertIsNone(result.funded_openrouter_checked_at)
        self.assertIsNone(result.journal_checked_at)
        self.assertEqual(len(github.calls), 5)
        self.assertEqual(opener.calls, [])
        self.assertEqual(server.calls, [])

    def test_other_publishing_workflow_also_short_circuits(self):
        reader, github, opener, server = sources(queue={
            "in_progress": [github_run(
                11111, "in_progress", workflow=991000,
                path=".github/workflows/run_bot_on_metaculus_cup.yaml",
            )]
        })
        result = reader.read()
        self.assertEqual(result.active_publishers, 1)
        self.assertEqual(opener.calls, [])
        self.assertEqual(server.calls, [])

    def test_failed_donated_key_identity_stops_before_journal(self):
        reader, github, opener, server = sources()
        reader.funded_source = SponsoredOpenRouterReader(
            donated_key=FAKE_TOKEN, expected_suffix="BAD",
            opener=opener, clock=lambda: NOW,
        )
        with self.assertRaises(SponsoredCreditUnavailable):
            reader.read()
        self.assertEqual(len(github.calls), 5)
        self.assertEqual(opener.calls, [])
        self.assertEqual(server.calls, [])

    def test_funded_credit_exhausted_stops_before_journal(self):
        reader, github, opener, server = sources(
            sponsor=funded_data(limit_remaining=4.99)
        )
        with self.assertRaises(SponsoredCreditUnavailable):
            reader.read()
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(server.calls, [])

    def test_corrupt_or_unavailable_encrypted_journal_fails_closed(self):
        store, server = fake_journal(damaged=True)
        reader, github, opener, _ = sources(journal=(store, server))
        with self.assertRaises(Exception):
            reader.read()
        self.assertEqual(len(opener.calls), 1)
        self.assertTrue(all(m == "GET" for m, _ in server.calls))

    def test_wrong_journal_branch_fails_before_any_journal_get(self):
        store, server = fake_journal(branch="main")
        reader, github, opener, _ = sources(journal=(store, server))
        with self.assertRaises(ReadOnlyPreflightUnavailable):
            reader.read()
        self.assertEqual(server.calls, [])

    def test_missing_journal_store_denies_without_network(self):
        reader, github, opener, server = sources()
        reader.journal_source = AuthenticatedJournalReader(
            store_factory=lambda: None,
            clock=lambda: NOW,
        )
        with self.assertRaises(ReadOnlyPreflightUnavailable):
            reader.read()
        self.assertEqual(server.calls, [])

    def test_frozen_legacy_does_not_mean_historical_outcomes_verified(self):
        store, server = fake_journal()
        snapshot = AuthenticatedJournalReader(
            store_factory=lambda: store, clock=lambda: NOW,
        ).read()
        self.assertTrue(snapshot.current_epoch_consistent)
        self.assertTrue(snapshot.current_epoch_confirmed)
        self.assertTrue(snapshot.legacy_freeze_policy_recognized)
        self.assertFalse(snapshot.historic_outcomes_reconciled)
        self.assertFalse(snapshot.previous_publication_outcome_certain)

    def test_empty_queue_and_donated_credit_still_cannot_dispatch(self):
        reader, github, opener, server = sources()
        evidence = reader.read()
        with self.assertRaises(AutonomousPreflightRejected):
            build_dispatch_snapshot(evidence, now=NOW, last_dispatch_at=None)

    def test_all_sources_require_exact_type_not_untrusted_object(self):
        reader, *_ = sources()
        with self.assertRaises(ReadOnlyPreflightUnavailable):
            ReadOnlyAuthenticatedSources(
                github_source=object(),
                funded_source=reader.funded_source,
                journal_source=reader.journal_source,
            )
        with self.assertRaises(ReadOnlyPreflightUnavailable):
            ReadOnlyAuthenticatedSources(
                github_source=reader.github_source,
                funded_source=object(),
                journal_source=reader.journal_source,
            )
        with self.assertRaises(ReadOnlyPreflightUnavailable):
            ReadOnlyAuthenticatedSources(
                github_source=reader.github_source,
                funded_source=reader.funded_source,
                journal_source=object(),
            )

    def test_no_runner_or_scheduler_loads_live_readonly_sources(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        for file in (
            root / "main.py",
            root / ".github/workflows/run_bot_on_tournament.yaml",
            root / ".github/workflows/manual_github_queue_diagnostic.yaml",
        ):
            text = file.read_text()
            self.assertNotIn("authenticated_readonly_preflight", text)
        current = (root / "authenticated_readonly_preflight.py").read_text()
        self.assertNotIn('method="POST"', current)
        self.assertNotIn("workflow_dispatch(", current)
        self.assertNotIn("metaculus.com/api", current)


if __name__ == "__main__":
    import unittest
    unittest.main()

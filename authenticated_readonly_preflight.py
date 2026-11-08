"""Genuine read-only GitHub/OpenRouter/encrypted-journal preflight sources.

NOT a publisher, scheduler, CLI, or approval mechanism. Not imported by
production workflows. No Metaculus API, no Perplexity Agent POST, no spending,
no GitHub PUT/POST. This module never infers operational approval, paid
Perplexity authorization or historic Metaculus publication certainty.

Only explicit future operator-approved calls to .read() make GET requests.
Every unknown/error is fail-closed, with no silent fallback to personal keys.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from autonomous_orchestrator import IndependentReadings
from github_queue_preflight import GitHubQueuePreflightSource
from sponsored_credit_guard import (
    verify_sponsored_openrouter_credit, SponsoredCreditUnavailable,
)
from github_publication import (
    GitHubPublicationStore, GitHubStateError, configured_store,
    LEGACY_FREEZE_POLICY,
)
from tools.audit_publication_journal_readonly import (
    GetOnlyBoundedSession, audit_store,
)


EXPECTED_REPOSITORY = "mcabejsek-creator/metac-bot-template"
EXPECTED_JOURNAL_BRANCH = "metaculus-publication-state"
EXPECTED_JOURNAL_PATH = "publication.enc"
MIN_SPONSOR_RESERVE = Decimal("5.00")


class ReadOnlyPreflightUnavailable(RuntimeError):
    """Never treat absent/untrusted evidence as safe authorization."""


def _utc_now(clock):
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ReadOnlyPreflightUnavailable("Evidence clock must be timezone aware")
    return now.astimezone(timezone.utc)


@dataclass(frozen=True)
class FundingSnapshot:
    checked_at: datetime
    sufficient: bool


class SponsoredOpenRouterReader:
    """One existing approved-shape key-metadata GET; never any model call."""

    def __init__(self, *, donated_key, expected_suffix, opener=None, clock=None):
        self._key = donated_key
        self._suffix = expected_suffix
        self._opener = opener
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def read(self):
        info = verify_sponsored_openrouter_credit(
            self._key,
            expected_suffix=self._suffix,
            opener=self._opener,
        )
        if not isinstance(info, dict) or not isinstance(info.get("remaining_usd"), str):
            raise ReadOnlyPreflightUnavailable("Malformed verified sponsored credit result")
        try:
            balance = Decimal(info["remaining_usd"])
        except (InvalidOperation, ValueError):
            raise ReadOnlyPreflightUnavailable("Sponsored credit result invalid") from None
        if not balance.is_finite() or balance < MIN_SPONSOR_RESERVE:
            raise ReadOnlyPreflightUnavailable("Sponsored credit reserve unavailable")
        return FundingSnapshot(
            checked_at=_utc_now(self._clock),
            sufficient=True,
        )


@dataclass(frozen=True)
class JournalSnapshot:
    checked_at: datetime
    current_epoch_consistent: bool
    current_epoch_confirmed: bool
    historic_outcomes_reconciled: bool
    legacy_freeze_policy_recognized: bool

    @property
    def previous_publication_outcome_certain(self):
        # A frozen/abandoned legacy attempt is NOT proof of an accepted
        # forecast. GitHub run success is NOT independent Metaculus proof.
        return bool(
            self.current_epoch_consistent is True
            and self.current_epoch_confirmed is True
            and self.historic_outcomes_reconciled is True
            and self.legacy_freeze_policy_recognized is True
        )


class AuthenticatedJournalReader:
    """Authenticated whole-journal scan; bounded GETs; no journal mutation.

    Respects the 202 GET maximum in the pre-existing read-only audit, and
    pins the repo+branch+file to avoid accidental main/other-state reads.
    """

    def __init__(self, *, store_factory=None, clock=None):
        self._store_factory = store_factory if store_factory is not None else configured_store
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def read(self):
        store = self._store_factory()
        if not isinstance(store, GitHubPublicationStore):
            raise ReadOnlyPreflightUnavailable("Validated GitHub journal store required")
        contents = store.contents
        if (contents.repository != EXPECTED_REPOSITORY
                or contents.branch != EXPECTED_JOURNAL_BRANCH
                or contents.path != EXPECTED_JOURNAL_PATH):
            raise ReadOnlyPreflightUnavailable("Unexpected GitHub journal source")
        transport = contents.session
        gate = GetOnlyBoundedSession(transport, contents)
        contents.session = gate
        try:
            report = audit_store(store)
        finally:
            contents.session = transport
        if not isinstance(report, dict):
            raise ReadOnlyPreflightUnavailable("Journal audit did not return a report")
        for key in (
            "snapshot_consistent_on_second_read",
            "encrypted_shards_authenticated",
            "all_new_epoch_steps_confirmed",
            "expected_legacy_freeze_policy",
            "historic_outcomes_fully_reconciled",
        ):
            if type(report.get(key)) is not bool:
                raise ReadOnlyPreflightUnavailable("Journal audit evidence missing: " + key)
        if (report["snapshot_consistent_on_second_read"] is not True
                or report["encrypted_shards_authenticated"] is not True
                or report["expected_legacy_freeze_policy"] is not True):
            raise ReadOnlyPreflightUnavailable("Journal authentication or freeze policy unverified")
        return JournalSnapshot(
            checked_at=_utc_now(self._clock),
            current_epoch_consistent=True,
            current_epoch_confirmed=report["all_new_epoch_steps_confirmed"],
            historic_outcomes_reconciled=report["historic_outcomes_fully_reconciled"],
            legacy_freeze_policy_recognized=True,
        )


class ReadOnlyAuthenticatedSources:
    """Combine ONLY three independently authenticated GET sources.

    IMPORTANT: a queued publisher blocks expensive metadata/journal reads.
    All Perplexity, activation and operator approval flags remain disabled,
    even if GitHub/OpenRouter/journal evidence looks healthy. No controller
    store write, GitHub workflow dispatch, or Metaculus publication.
    """

    def __init__(self, *, github_source, funded_source, journal_source):
        if (not isinstance(github_source, GitHubQueuePreflightSource)
                or not isinstance(funded_source, SponsoredOpenRouterReader)
                or not isinstance(journal_source, AuthenticatedJournalReader)):
            raise ReadOnlyPreflightUnavailable("Three typed read-only sources required")
        self.github_source = github_source
        self.funded_source = funded_source
        self.journal_source = journal_source

    def read(self):
        github = self.github_source.read()
        if not isinstance(github, IndependentReadings):
            raise ReadOnlyPreflightUnavailable("Unverified GitHub queue response")
        if (github.active_publishers != 0 or github.queued_publishers != 0):
            # Do NOT contact a funded provider or encrypted journal while a
            # queued/active publishing operation already blocks dispatch.
            return github
        funded = self.funded_source.read()
        journal = self.journal_source.read()
        if funded.sufficient is not True:
            raise ReadOnlyPreflightUnavailable("Funded credits not independently verified")
        # One real result is only one layer of evidence. The current
        # Perplexity wallet has no reliable API-key prepaid balance reader.
        return IndependentReadings(
            github_checked_at=github.github_checked_at,
            active_publishers=github.active_publishers,
            queued_publishers=github.queued_publishers,
            funded_openrouter_checked_at=funded.checked_at,
            funded_openrouter_ok=True,
            journal_checked_at=journal.checked_at,
            prior_publication_certain=journal.previous_publication_outcome_certain,
            # All remaining fields are INTENTIONALLY at fail-closed defaults.
        )

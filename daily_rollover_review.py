"""Pure OFFLINE review of a one-day UTC quota rollover; NO live reset or API.

The return value is only a proposed DISABLED control state. It never writes
to GitHub, sends workflow_dispatch, queries providers, or settles forecasts.
A future independent adapter must authenticate every claimed evidence field,
persist an immutable audit first, and require separate operator approval for
any actual compare-and-swap. This module cannot grant such authorization.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re

from dispatch_intent_controller import ControllerState, DailyQuota

MAX_EVIDENCE_AGE = timedelta(minutes=5)
ONE_DAY_APPROVAL = "APPROVE_ONE_DISABLED_UTC_DAY_REVIEW"
_MONEY = re.compile(r"[0-9]+(?:\\.[0-9]{1,6})?\\Z")


@dataclass(frozen=True)
class DailyRolloverEvidence:
    """Operator and external provider assertions, NOT authenticated by this code.

    The independently verified provider statement MUST include every
    relevant key/client and any possible charges from lost responses.
    A dashboard balance screenshot or returned Agent usage alone is NOT
    sufficient. An external adapter must establish truth, not set flags.
    """
    previous_day_utc: str
    target_day_utc: str
    checked_at: datetime
    prior_attempts: int
    prior_reserved_usd: str
    provider_billed_usd: str
    approved_prior_day_cap_usd: str
    unaccounted_agent_attempts: int
    unresolved_publication_attempts: int
    active_publisher_runs: int
    queued_publisher_runs: int
    provider_statement_independently_verified: bool = False
    all_clients_and_charges_reconciled: bool = False
    publication_history_reconciled: bool = False
    prior_audit_archived_immutably: bool = False
    operator_approval: str = ""


@dataclass(frozen=True)
class RolloverPreview:
    eligible_for_separately_authorized_cas: bool
    reason: str
    proposed_disabled_state: ControllerState | None = None


def _date(value):
    if type(value) is not str or len(value) != 10:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    return day if day.isoformat() == value else None


def _usd(value):
    if type(value) is not str or _MONEY.fullmatch(value) is None:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and 0 <= amount <= 10000 else None


def preview_disabled_daily_rollover(state, evidence, *, now, approval_epoch):
    """Draft ONLY. No transition is committed, and no controller is enabled."""
    if type(state) is not ControllerState:
        return RolloverPreview(False, "control_state_not_verified")
    if state.enabled is not False or state.pending is not None:
        return RolloverPreview(False, "active_or_uncertain_controller")
    if (type(approval_epoch) is not int or approval_epoch <= 0
            or state.approval_epoch != approval_epoch):
        return RolloverPreview(False, "controller_approval_epoch_mismatch")
    if type(state.daily_quota) is not DailyQuota:
        return RolloverPreview(False, "prior_durable_quota_missing")
    if type(evidence) is not DailyRolloverEvidence:
        return RolloverPreview(False, "complete_review_evidence_missing")
    if evidence.operator_approval != ONE_DAY_APPROVAL:
        return RolloverPreview(False, "specific_operator_rollover_review_required")
    if not all(x is True for x in (
        evidence.provider_statement_independently_verified,
        evidence.all_clients_and_charges_reconciled,
        evidence.publication_history_reconciled,
        evidence.prior_audit_archived_immutably,
    )):
        return RolloverPreview(False, "independent_billing_and_history_audit_missing")

    if (type(now) is not datetime or now.tzinfo is None
            or now.utcoffset() is None
            or type(evidence.checked_at) is not datetime
            or evidence.checked_at.tzinfo is None
            or evidence.checked_at.utcoffset() is None):
        return RolloverPreview(False, "verified_utc_timestamps_missing")
    moment = now.astimezone(timezone.utc)
    age = moment - evidence.checked_at.astimezone(timezone.utc)
    if not timedelta(0) <= age <= MAX_EVIDENCE_AGE:
        return RolloverPreview(False, "independent_evidence_stale")

    before = _date(evidence.previous_day_utc)
    after = _date(evidence.target_day_utc)
    if (before is None or after is None
            or after != before + timedelta(days=1)
            or after != moment.date()
            or state.daily_quota.day_utc != evidence.previous_day_utc):
        return RolloverPreview(False, "utc_day_transition_not_verified")
    if state.last_intent_at is not None:
        # The existing controller validates the timestamp format and timezone.
        last_day = date.fromisoformat(state.last_intent_at[:10])
        if last_day > before:
            return RolloverPreview(False, "future_intent_not_reconciled")

    numeric_fields = (
        evidence.prior_attempts,
        evidence.unaccounted_agent_attempts,
        evidence.unresolved_publication_attempts,
        evidence.active_publisher_runs,
        evidence.queued_publisher_runs,
    )
    if any(type(x) is not int or x < 0 for x in numeric_fields):
        return RolloverPreview(False, "attempt_or_queue_counts_unverified")
    if (evidence.prior_attempts != state.daily_quota.attempts
            or any(numeric_fields[1:])):
        return RolloverPreview(False, "outstanding_or_mismatched_attempts")

    reserved = _usd(evidence.prior_reserved_usd)
    billed = _usd(evidence.provider_billed_usd)
    cap = _usd(evidence.approved_prior_day_cap_usd)
    prior = _usd(state.daily_quota.reserved_usd)
    if any(x is None for x in (reserved, billed, cap, prior)):
        return RolloverPreview(False, "daily_financial_evidence_invalid")
    if reserved != prior or cap <= 0 or billed > cap:
        return RolloverPreview(False, "prior_budget_not_reconciled")

    # Proposed result is still DISABLED. It is NOT an authorization to write,
    # reset provider budgets, auto-start models, or replay prior forecasts.
    proposal = replace(
        state,
        daily_quota=DailyQuota(day_utc=after.isoformat(),
                               attempts=0, reserved_usd="0"),
        enabled=False,
    )
    return RolloverPreview(True, "eligible_for_separate_operator_cas_review", proposal)

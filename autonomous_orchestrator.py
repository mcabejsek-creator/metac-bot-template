"""Offline, fail-closed composition of future autonomous dispatch safeguards.

This file is NOT referenced by any live CLI, GitHub workflow or cron timer.
No provider transport, workflow_dispatch POST, model, or Metaculus API here.
It composes independently obtained preflight readings with the dormant
15-minute scheduler and CAS control-journal from earlier PRs.

"Verified" flags here represent assertions from a future trusted reader,
NOT independent proof of origin; a production reader/worker still needs an
auditable provider-verified implementation and separate user authorization.
The optional offline OIDC check authenticates only signed Actions context;
production trust is absent and it never authorizes a worker launch.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import worker_configuration
import worker_identity

from dispatch_intent_controller import (
    ControllerState, ReservationResult, InvalidControlState,
    propose_reservation, reserve_with_store, disable_controller, _strict_money,
)
from scheduler_policy import DailySpendEvidence, DispatchSnapshot, STATUS_MAX_AGE
from perplexity_run_budget import MAX_AUTO_AGENT_POST_ATTEMPTS


class AutonomousPreflightRejected(RuntimeError):
    """Missing or untrusted attestation means no automatic work."""


class KillSwitchUncertain(RuntimeError):
    """An emergency-stop write could not be verified as durable."""


@dataclass(frozen=True)
class IndependentReadings:
    """Data obtained by distinct trusted adapters in a FUTURE controller.

    Until adapters are live, these are only synthetic test inputs. No
    unverified local config should be allowed to set evidence flags true.
    """
    github_checked_at: datetime | None = None
    active_publishers: int | None = None
    queued_publishers: int | None = None
    funded_openrouter_checked_at: datetime | None = None
    funded_openrouter_ok: bool = False
    perplexity_checked_at: datetime | None = None
    perplexity_spend_ok: bool = False
    daily_spend: DailySpendEvidence | None = None
    journal_checked_at: datetime | None = None
    prior_publication_certain: bool = False
    operator_approval_confirmed: bool = False
    automation_switch_confirmed: bool = False
    is_first_attempt: bool = False


def _now(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AutonomousPreflightRejected("Timezone-aware UTC status required")
    return value.astimezone(timezone.utc)


def _recent(stamp, now, label, max_age=STATUS_MAX_AGE):
    if stamp is None:
        raise AutonomousPreflightRejected("Missing " + label)
    stamp = _now(stamp)
    if not timedelta(0) <= now - stamp <= max_age:
        raise AutonomousPreflightRejected("Stale or future " + label)
    return stamp


def build_dispatch_snapshot(readings: IndependentReadings, *, now, last_dispatch_at):
    """Fail closed before CAS if any independent source is unavailable.

    All four source clocks are individually required; the fact that GitHub
    status was recently refreshed says nothing about the other providers.
    Caller MUST supply independently authenticated evidence in production.
    """
    if not isinstance(readings, IndependentReadings):
        raise AutonomousPreflightRejected("Verified reader object required")
    instant = _now(now)
    github_time = _recent(readings.github_checked_at, instant, "GitHub run status")
    _recent(readings.funded_openrouter_checked_at, instant, "OpenRouter grant")
    _recent(readings.perplexity_checked_at, instant, "Perplexity spending approval")
    if type(readings.daily_spend) is not DailySpendEvidence:
        raise AutonomousPreflightRejected("Missing independently audited daily spend")
    _recent(readings.daily_spend.checked_at, instant, "daily spend and attempt quota")
    _recent(readings.journal_checked_at, instant, "encrypted publication journal")

    if not all(type(x) is bool and x for x in (
        readings.funded_openrouter_ok,
        readings.perplexity_spend_ok,
        readings.prior_publication_certain,
        readings.operator_approval_confirmed,
        readings.automation_switch_confirmed,
        readings.is_first_attempt,
    )):
        raise AutonomousPreflightRejected("Funding, journal, activation or first attempt not verified")

    for value in (readings.active_publishers, readings.queued_publishers):
        if type(value) is not int or value < 0:
            raise AutonomousPreflightRejected("Unverifiable live or queued publisher run count")
    if readings.active_publishers or readings.queued_publishers:
        raise AutonomousPreflightRejected("Another publisher running or queued")

    return DispatchSnapshot(
        now=instant, github_status_checked_at=github_time,
        last_dispatch_at=last_dispatch_at,
        active_publisher_runs=readings.active_publishers,
        queued_publisher_runs=readings.queued_publishers,
        explicit_automation_approved=readings.operator_approval_confirmed,
        activation_switch_enabled=readings.automation_switch_confirmed,
        sponsor_budget_guard_available=readings.funded_openrouter_ok,
        perplexity_spend_guard_available=readings.perplexity_spend_ok,
        previous_publication_outcome_certain=readings.prior_publication_certain,
        first_attempt=readings.is_first_attempt,
        daily_spend=readings.daily_spend,
    )


@dataclass(frozen=True)
class ControllerTick:
    allowed: bool
    reason: str
    intent_id: str | None = None
    durably_reserved: bool = False

    # Important: a reserved intent is NOT a workflow dispatch or a forecast.


def evaluate_offline_tick(store, source, *, now, approval_epoch):
    """Pure preview. Only performs store.read/source.read, never control CAS."""
    state, revision = store.read()
    if not isinstance(state, ControllerState) or not isinstance(revision, str) or not revision:
        raise AutonomousPreflightRejected("Missing persistent controller state")
    if not state.enabled:
        return ControllerTick(False, "persistent_kill_switch_disabled")
    if type(approval_epoch) is not int or approval_epoch != state.approval_epoch:
        return ControllerTick(False, "approval_generation_mismatch")
    if state.pending is not None:
        return ControllerTick(False, "pending_or_uncertain_dispatch_requires_reconciliation")
    if state.last_intent_at is not None:
        last = datetime.fromisoformat(state.last_intent_at.replace("Z", "+00:00"))
    else:
        last = None
    readings = source.read()
    snapshot = build_dispatch_snapshot(readings, now=now, last_dispatch_at=last)
    proposal = propose_reservation(state, snapshot, approval_epoch=approval_epoch)
    return ControllerTick(proposal.allowed, proposal.reason, proposal.intent_id, False)


def reserve_only_after_preflight(store, source, *, now, approval_epoch,
                                 explicit_control_write_approved=False):
    """An opt-in control-state reservation, NEVER a workflow dispatch.

    Before a genuine publisher is possible, a future operator must separately
    approve creating the control branch and allowing repository writes.
    On a CAS conflict/timeout do not retry: caller must reconcile.
    """
    if explicit_control_write_approved is not True:
        return ControllerTick(False, "control_write_not_separately_authorized")
    state, revision = store.read()
    if not isinstance(state, ControllerState) or not isinstance(revision, str) or not revision:
        raise AutonomousPreflightRejected("Missing persistent controller state")
    if not state.enabled:
        return ControllerTick(False, "persistent_kill_switch_disabled")
    if type(approval_epoch) is not int or approval_epoch != state.approval_epoch:
        return ControllerTick(False, "approval_generation_mismatch")
    if state.pending is not None:
        return ControllerTick(False, "pending_or_uncertain_dispatch_requires_reconciliation")
    last = (datetime.fromisoformat(state.last_intent_at.replace("Z", "+00:00"))
            if state.last_intent_at is not None else None)
    snapshot = build_dispatch_snapshot(source.read(), now=now, last_dispatch_at=last)
    # The trusted control state may have changed since this read: the
    # expected-revision CAS in reserve_with_store rechecks authoritatively.
    reservation = reserve_with_store(store, snapshot, approval_epoch=approval_epoch)
    return ControllerTick(
        reservation.allowed, reservation.reason, reservation.intent_id,
        reservation.allowed and reservation.reason == "durably_reserved_not_dispatched",
    )


def evaluate_offline_worker_handoff(store, source, *, now, intent_id=None,
                                    approval_epoch=None,
                                    activation_confirmed=False,
                                    researcher=None,
                                    research_reports_per_question=None,
                                    worker_oidc_token=None):
    """Read-only admission preview for ONE atomic automatic-question worker.

    Unlike evaluate_offline_tick, this requires an ALREADY reserved intent.
    Its attempt/reserve are checked, never charged a second time. All readers
    must be trusted in production; Python flags do not authenticate evidence.

    This does NOT consume/claim the intent or invoke a worker. A durable
    single-worker claim and independent per-request stop/budget checks remain
    prerequisites to execution. Repeated previews are NOT launch permission.
    This gate is not wired into a CLI, workflow, or the live manual publisher.
    'now' may be a fixed offline instant or an injected clock. A real reader
    needs the clock so evidence is evaluated AFTER the reads, not before them.
    Research parameters are optional consistency claims, never configuration
    or identity evidence. The actual plan comes from the same immutable
    source-controlled settings main uses. This does not attest which code,
    mode or configuration a remote worker would actually execute; absent
    that independent identity binding, the final decision remains DENY.
    An optional signed OIDC token is checked only against controller-owned
    policy/keys, never worker-selected trust or an identity boolean. Production
    trust is not configured. Even a valid token cannot authorize execution.
    """
    if activation_confirmed is not True:
        return ControllerTick(False, "worker_activation_not_confirmed")
    state, revision = store.read()
    if not isinstance(state, ControllerState) or not isinstance(revision, str) or not revision:
        raise AutonomousPreflightRejected("Missing persistent controller state")
    if state.enabled is not True:
        return ControllerTick(False, "persistent_kill_switch_disabled")
    if type(approval_epoch) is not int or approval_epoch != state.approval_epoch:
        return ControllerTick(False, "approval_generation_mismatch")
    if state.pending is None:
        return ControllerTick(False, "worker_intent_not_reserved")
    if type(intent_id) is not str or intent_id != state.pending.intent_id:
        return ControllerTick(False, "worker_intent_mismatch")
    instant = _now(now() if callable(now) else now)
    reserved = datetime.fromisoformat(state.pending.reserved_at.replace("Z", "+00:00"))
    if not timedelta(0) <= instant - reserved <= STATUS_MAX_AGE:
        return ControllerTick(False, "worker_intent_expired_or_future")

    config = worker_configuration.WORKER_CONFIG
    if type(config) is not worker_configuration.WorkerConfiguration:
        return ControllerTick(False, "worker_configuration_unavailable")
    # Compatibility claims may reject a mismatch, but never override main's
    # shared configuration. No caller-supplied configuration object is used.
    if ((researcher is not None
         and (type(researcher) is not str or researcher != config.researcher))
            or (research_reports_per_question is not None
                and (type(research_reports_per_question) is not int
                     or research_reports_per_question != config.research_reports_per_question))):
        return ControllerTick(False, "worker_configuration_mismatch")
    if config.agent_requests_per_atomic_question > MAX_AUTO_AGENT_POST_ATTEMPTS:
        return ControllerTick(False, "worker_research_plan_exceeds_automatic_attempt_cap")

    readings = source.read()
    after_read = _now(now() if callable(now) else now)
    if after_read < instant:
        return ControllerTick(False, "worker_clock_moved_backwards")
    instant = after_read
    if not timedelta(0) <= instant - reserved <= STATUS_MAX_AGE:
        return ControllerTick(False, "worker_intent_expired_or_future")
    snapshot = build_dispatch_snapshot(readings, now=instant, last_dispatch_at=None)
    # Evidence from before reservation cannot corroborate the reserved attempt.
    for stamp in (
        readings.github_checked_at, readings.funded_openrouter_checked_at,
        readings.perplexity_checked_at, readings.journal_checked_at,
        readings.daily_spend.checked_at,
    ):
        if _now(stamp) < reserved:
            return ControllerTick(False, "worker_sources_predate_intent")
    budget, quota = snapshot.daily_spend, state.daily_quota
    if (quota is None or budget.complete is not True
            or budget.day_utc != instant.date()
            or quota.day_utc != instant.date().isoformat()
            or type(budget.attempted_dispatches) is not int
            or type(budget.max_daily_dispatches) is not int
            or budget.max_daily_dispatches <= 0
            or budget.attempted_dispatches != quota.attempts
            or not 1 <= quota.attempts <= budget.max_daily_dispatches):
        return ControllerTick(False, "worker_reserved_daily_quota_unverified")
    try:
        charged = _strict_money(budget.charged_usd)
        cap = _strict_money(budget.approved_day_cap_usd)
        reserve = _strict_money(budget.approved_run_reserve_usd)
        held = _strict_money(quota.reserved_usd)
    except InvalidControlState:
        return ControllerTick(False, "worker_reserved_daily_budget_unverified")
    # Do not add another allowance to 'held' or increment attempts: the CAS
    # already reserved this run. New observed charges must still leave its
    # planned allowance. Exhausting the LAST approved attempt slot is valid.
    if cap <= 0 or reserve <= 0 or held < reserve or max(held, charged + reserve) > cap:
        return ControllerTick(False, "worker_reserved_daily_budget_unverified")
    observed, observed_revision = store.read()
    if observed != state or observed_revision != revision:
        return ControllerTick(False, "worker_control_state_changed_during_preflight")
    if worker_configuration.WORKER_CONFIG != config:
        return ControllerTick(False, "worker_configuration_changed_during_preflight")
    if worker_oidc_token is not None:
        try:
            worker_identity.verify_controller_worker_token(worker_oidc_token, now=instant)
        except worker_identity.WorkerIdentityRejected:
            return ControllerTick(False, "worker_oidc_identity_unverified", intent_id)
    # Shared settings establish the source plan, not an approved artifact or
    # the identity of the actually executing worker. SHA/mode claims or local
    # checkout metadata cannot supply that missing proof. An authenticated
    # Actions context cannot attest executed code/configuration either. No
    # identity result/boolean is accepted as launch authority. Never approve.
    return ControllerTick(False, "worker_execution_identity_unverified", intent_id)


def persist_emergency_stop(store, *, operator_stop_authorized=False):
    """Persist OFF via one CAS, then READ BACK. No publishing cancellation.

    A separate future runner needs an independent kill-switch read
    immediately before every paid request/Metaculus POST; already-running
    workflows require cancellation outside this library.
    """
    if operator_stop_authorized is not True:
        raise KillSwitchUncertain("Emergency controller change not authorized")
    state, revision = store.read()
    if not isinstance(state, ControllerState) or not isinstance(revision, str) or not revision:
        raise KillSwitchUncertain("Controller state unavailable")
    if state.enabled:
        disabled = disable_controller(state)
        try:
            if store.compare_and_swap(revision, disabled) is not True:
                raise KillSwitchUncertain("Control state CAS failed; stop unconfirmed")
        except KillSwitchUncertain:
            raise
        except Exception:
            raise KillSwitchUncertain("Unknown control write outcome; stop unconfirmed") from None
    observed, check_revision = store.read()
    if (not isinstance(observed, ControllerState)
            or observed.enabled is not False
            or not isinstance(check_revision, str) or not check_revision):
        raise KillSwitchUncertain("Control OFF could not be read back")
    if state.pending is not None and observed.pending != state.pending:
        raise KillSwitchUncertain("Emergency stop unexpectedly changed pending intent")
    return ControllerTick(False, "controller_off_confirmed_pending_work_may_need_cancellation")


@dataclass(frozen=True)
class OutcomeEvidence:
    """Synthetic holder for future MULTI-SOURCE publication reconciliation.

    No single result (including a successful GitHub job) is sufficient.
    This module only reports whether a pending intent is SETTLEMENT READY;
    it NEVER clears a pending intent or writes the encrypted journal.
    """
    intent_id: str
    github_run_id: int
    run_completed: bool
    workflow_conclusion: str
    workflow_path: str
    run_intent_id: str
    run_confirmed_post_id: int | None
    journal_confirmed_post_id: int | None
    metaculus_confirmed_post_id: int | None
    independently_checked_billing: bool
    github_finished_at: datetime | None
    journal_checked_at: datetime | None
    metaculus_checked_at: datetime | None
    billing_checked_at: datetime | None


def assess_settlement(state, proof: OutcomeEvidence, *, now):
    """Only POST CONFIRMED can become eligible for future manual settlement.

    No-candidate, unknown or failed attempts remain blocked until separate
    independently verified no-POST reconciliation is implemented.
    """
    if not isinstance(state, ControllerState) or state.pending is None:
        return ControllerTick(False, "no_pending_intent_to_reconcile")
    if not isinstance(proof, OutcomeEvidence):
        return ControllerTick(False, "missing_independent_settlement_evidence")
    if proof.intent_id != state.pending.intent_id or proof.run_intent_id != proof.intent_id:
        return ControllerTick(False, "workflow_intent_correlation_mismatch")
    if (type(proof.github_run_id) is not int or proof.github_run_id < 1
            or proof.run_completed is not True or proof.independently_checked_billing is not True
            or proof.workflow_conclusion != "success"
            or proof.workflow_path != "autonomous_one_worker.yaml"):
        return ControllerTick(False, "unfinished_or_unverified_publication")
    if (type(proof.journal_confirmed_post_id) is not int
            or type(proof.metaculus_confirmed_post_id) is not int
            or type(proof.run_confirmed_post_id) is not int
            or proof.journal_confirmed_post_id < 1
            or proof.journal_confirmed_post_id != proof.metaculus_confirmed_post_id
            or proof.run_confirmed_post_id != proof.metaculus_confirmed_post_id):
        return ControllerTick(False, "journal_metaculus_disagreement")
    try:
        instant = _now(now)
        for label, stamp in (
            ("GitHub completion", proof.github_finished_at),
            ("publication journal", proof.journal_checked_at),
            ("Metaculus history", proof.metaculus_checked_at),
            ("provider billing", proof.billing_checked_at),
        ):
            _recent(stamp, instant, label, max_age=timedelta(days=7))
        completed = _now(proof.github_finished_at)
        reserved = datetime.fromisoformat(state.pending.reserved_at.replace("Z", "+00:00"))
        if completed < reserved:
            return ControllerTick(False, "workflow_completion_predates_intent")
        # Later source audits must actually follow the completed workflow.
        # Otherwise a stale snapshot from an earlier run is not evidence.
        for stamp in (proof.journal_checked_at, proof.metaculus_checked_at,
                      proof.billing_checked_at):
            if _now(stamp) < completed:
                return ControllerTick(False, "settlement_sources_predate_workflow_completion")
    except AutonomousPreflightRejected:
        return ControllerTick(False, "settlement_evidence_stale_or_missing")
    return ControllerTick(True, "eligible_for_separately_authorized_manual_reconciliation", proof.intent_id)

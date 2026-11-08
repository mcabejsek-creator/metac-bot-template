"""Pure offline dispatch policy for a FUTURE separately approved scheduler.

This is not a workflow, cron, HTTP client or publication authorization.
It can only return a decision; it cannot activate a publisher.
The external scheduler must enforce its own persistent activation/kill
switch and independently verify every claimed billing/run-state signal.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

POLL_INTERVAL = timedelta(minutes=15)
STATUS_MAX_AGE = timedelta(minutes=5)


@dataclass(frozen=True)
class DispatchSnapshot:
    now: datetime
    github_status_checked_at: datetime | None = None
    last_dispatch_at: datetime | None = None
    active_publisher_runs: int | None = None
    queued_publisher_runs: int | None = None
    # All flags default to DENY. No truthy integers or strings permitted.
    explicit_automation_approved: bool = False
    activation_switch_enabled: bool = False
    sponsor_budget_guard_available: bool = False
    perplexity_spend_guard_available: bool = False
    previous_publication_outcome_certain: bool = False
    first_attempt: bool = False


@dataclass(frozen=True)
class DispatchDecision:
    allowed: bool
    reason: str


def _utc_time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Scheduler times must be timezone aware")
    return value.astimezone(timezone.utc)


def evaluate_dispatch(snapshot: DispatchSnapshot) -> DispatchDecision:
    """Fail closed if status, budget proof, activation or dedupe is not verified.

    Even an allowed result cannot publish or send GitHub workflow_dispatch.
    It means only that a future *separate* dispatcher may consider performing
    a newly authorized launch, after additional server-side checks.
    """
    if not isinstance(snapshot, DispatchSnapshot):
        raise TypeError("Typed dispatch snapshot required")
    now = _utc_time(snapshot.now)
    if any(type(x) is not bool or x is not True for x in (
        snapshot.explicit_automation_approved,
        snapshot.activation_switch_enabled,
        snapshot.sponsor_budget_guard_available,
        snapshot.perplexity_spend_guard_available,
        snapshot.previous_publication_outcome_certain,
        snapshot.first_attempt,
    )):
        return DispatchDecision(False, "activation_or_budget_not_authorized")
    if (type(snapshot.active_publisher_runs) is not int
            or type(snapshot.queued_publisher_runs) is not int
            or snapshot.active_publisher_runs < 0
            or snapshot.queued_publisher_runs < 0):
        return DispatchDecision(False, "publisher_run_status_unknown")
    if snapshot.active_publisher_runs or snapshot.queued_publisher_runs:
        return DispatchDecision(False, "publisher_already_running_or_queued")
    if snapshot.github_status_checked_at is None:
        return DispatchDecision(False, "github_status_not_checked")
    checked = _utc_time(snapshot.github_status_checked_at)
    if not timedelta(0) <= now - checked <= STATUS_MAX_AGE:
        return DispatchDecision(False, "github_status_stale")
    if snapshot.last_dispatch_at is not None:
        last = _utc_time(snapshot.last_dispatch_at)
        if not timedelta(0) <= now - last:
            return DispatchDecision(False, "invalid_last_dispatch_time")
        if now - last < POLL_INTERVAL:
            return DispatchDecision(False, "min_dispatch_spacing_not_reached")
    return DispatchDecision(True, "eligible_for_separately_authorized_dispatch")

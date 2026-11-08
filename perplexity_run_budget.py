"""Fail-closed, dormant cost/attempt guard for Perplexity Agent automation.

Perplexity API keys do NOT expose a verified prepaid-credit balance endpoint.
An operator-entered screenshot observation is NOT a real-time provider check.
This guard enforces a strict *count* of outbound POST attempts and monitors
reported costs after successful replies. It CANNOT cap the dollar value of
a single already-sent request, guarantee wallet funds or reconcile other users.
Automatic activation needs a separately reviewed provider-side spending cap.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from threading import Lock

MAX_AUTO_AGENT_POST_ATTEMPTS = 2  # initial research + fact-check; no retries
MAX_ATTESTATION_AGE = timedelta(hours=6)
MINIMUM_WALLET_RESERVE_USD = Decimal("2.00")


class PerplexityBudgetUnavailable(RuntimeError):
    """Never send an automatic paid research request when budget is unknown."""


def _money(value, *, field):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise PerplexityBudgetUnavailable("Missing or invalid " + field)
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        raise PerplexityBudgetUnavailable("Invalid " + field) from None
    if not amount.is_finite() or amount < 0 or amount > Decimal("10000"):
        raise PerplexityBudgetUnavailable("Invalid " + field)
    return amount


def _timestamp(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PerplexityBudgetUnavailable("Only explicit UTC attestation times permitted")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PerplexityBudgetUnavailable("Malformed UTC attestation") from None
    if stamp.tzinfo is None:
        raise PerplexityBudgetUnavailable("Naive attestation forbidden")
    return stamp.astimezone(timezone.utc)


class PerplexityAutoRunBudget:
    """One-process guarded POST ledger; default construction denies all sends.

    The operator's verified balance is a manual snapshot, never a live API
    result. Authoritative wallet state, other clients and provider-side
    billing are outside this process. Attempt counter remains spent on
    ambiguous outcomes. No calls are retried automatically.
    """

    def __init__(self, *, approved=False, balance_usd=None,
                 verified_at=None, run_cap_usd=None, now=None,
                 auto_recharge_confirmed_off=False):
        if (approved is not True or auto_recharge_confirmed_off is not True):
            raise PerplexityBudgetUnavailable("Perplexity unattended spending not approved")
        self._balance = _money(balance_usd, field="verified balance")
        self._cap = _money(run_cap_usd, field="approved run ceiling")
        if self._cap <= 0:
            raise PerplexityBudgetUnavailable("Run budget must be positive")
        self._checked = _timestamp(verified_at)
        current = now if now is not None else datetime.now(timezone.utc)
        if (not isinstance(current, datetime) or current.tzinfo is None
                or current.utcoffset() is None):
            raise PerplexityBudgetUnavailable("Current time must be timezone aware")
        if not timedelta(0) <= current.astimezone(timezone.utc) - self._checked <= MAX_ATTESTATION_AGE:
            raise PerplexityBudgetUnavailable("Perplexity balance attestation expired")
        if self._balance - self._cap < MINIMUM_WALLET_RESERVE_USD:
            raise PerplexityBudgetUnavailable("Insufficient verified Perplexity reserve")
        self._used = Decimal("0")
        self._attempts = 0
        self._inflight = False
        self._outcome_uncertain = False
        self._lock = Lock()

    def begin_attempt(self):
        """Reserve a paid attempt BEFORE network transmission."""
        with self._lock:
            if self._inflight or self._outcome_uncertain:
                raise PerplexityBudgetUnavailable("Previous research outcome unverified")
            if self._attempts >= MAX_AUTO_AGENT_POST_ATTEMPTS:
                raise PerplexityBudgetUnavailable("Perplexity attempt cap reached")
            if self._used >= self._cap:
                raise PerplexityBudgetUnavailable("Perplexity observed run cap reached")
            self._attempts += 1
            self._inflight = True

    def record_response(self, response):
        """Record provider-reported cost; missing cost means unknown outcome.

        Actual dollar cap is POST-response only and CANNOT prevent a single
        call from costing more than the amount approved. Caller must refrain
        from retrying or sending further calls after any uncertainty.
        """
        with self._lock:
            if not self._inflight:
                raise PerplexityBudgetUnavailable("No Perplexity attempt in progress")
            try:
                if not isinstance(response, dict):
                    raise ValueError()
                usage = response["usage"]
                if not isinstance(usage, dict):
                    raise ValueError()
                cost = usage["cost"]
                if not isinstance(cost, dict):
                    raise ValueError()
                value = cost["total_cost"]
                if type(value) not in (str, int, float):
                    raise ValueError()
                amount = _money(str(value), field="API response cost")
            except (ValueError, KeyError, TypeError, PerplexityBudgetUnavailable):
                self._outcome_uncertain = True
                self._inflight = False
                raise PerplexityBudgetUnavailable("Agent cost absent or invalid; stop research") from None
            self._used += amount
            self._inflight = False
            if self._used > self._cap:
                self._outcome_uncertain = True
                raise PerplexityBudgetUnavailable("Actual Perplexity cost exceeded approved run cap")

    def abandon_unknown_response(self):
        """Network timeout or malformed provider response: cost may be charged."""
        with self._lock:
            if self._inflight:
                self._inflight = False
                self._outcome_uncertain = True

    @property
    def attempts(self):
        with self._lock:
            return self._attempts

    @property
    def observed_cost_usd(self):
        with self._lock:
            return str(self._used)

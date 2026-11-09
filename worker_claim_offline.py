"""Isolated V3 control-state experiment: assign an owner, NEVER run a worker.

No production binding, V2 migration, second ledger, network, leases or retries.
The controller must independently provision an authoritative, linearizable
whole-state CAS store with non-reused opaque revisions. CONTROLLER_STORE stays
None. Tests bind a synthetic SQLite store containing ONE complete control row.
OIDC trust is likewise controller-owned and absent in production.
"""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import re

from dispatch_intent_controller import (
    ControllerState, InvalidControlState, STATE_FIELDS, _instant, _iso, disable_controller,
)
from scheduler_policy import STATUS_MAX_AGE
import worker_identity


VERSION = 3  # OFFLINE ONLY; dispatch_intent_controller.VERSION remains 2
MAX_STATE_BYTES = 8192


def _hex(value, length):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value) is not None


@dataclass(frozen=True)
class ClaimOwner:
    """Recorded Actions context, not proof of the identity of a Python process."""

    repository_id: str
    repository_owner_id: str
    source_sha: str
    workflow_ref: str
    workflow_sha: str
    run_id: str
    run_attempt: str

    def __post_init__(self):
        for value in (self.repository_id, self.repository_owner_id, self.run_id):
            if type(value) is not str or re.fullmatch(r"[1-9][0-9]{0,19}", value) is None:
                raise InvalidControlState("Invalid owner identifier")
        if not _hex(self.source_sha, 40) or not _hex(self.workflow_sha, 40):
            raise InvalidControlState("Invalid owner commit")
        if (type(self.workflow_ref) is not str or not 1 <= len(self.workflow_ref) <= 1024
                or any(c.isspace() for c in self.workflow_ref)):
            raise InvalidControlState("Invalid owner workflow")
        if type(self.run_attempt) is not str or self.run_attempt != "1":
            raise InvalidControlState("Only first run attempt can own an intent")


@dataclass(frozen=True)
class WorkerClaim:
    intent_id: str
    approval_epoch: int
    request_id: str  # Correlation only; a caller-selected nonce confers no identity.
    owner: ClaimOwner
    claimed_at: str

    def __post_init__(self):
        if not _hex(self.intent_id, 64) or not _hex(self.request_id, 32):
            raise InvalidControlState("Invalid claim correlation")
        if type(self.approval_epoch) is not int or not 1 <= self.approval_epoch < 100000000:
            raise InvalidControlState("Invalid claim approval epoch")
        if type(self.owner) is not ClaimOwner:
            raise InvalidControlState("Invalid claim owner schema")
        _instant(self.claimed_at)


@dataclass(frozen=True)
class OfflineControlStateV3:
    """One full future control document, not an adjunct ledger to live V2.

    Reuses V2 validation in memory; serializes the complete document as V3.
    No implicit conversion/migration from stored V2 is accepted by decode().
    """

    control: ControllerState
    worker_claim: WorkerClaim | None = None

    def __post_init__(self):
        if type(self.control) is not ControllerState:
            raise InvalidControlState("Complete validated control state required")
        ControllerState.decode(self.control.encode())
        claim = self.worker_claim
        if claim is not None:
            pending = self.control.pending
            if (type(claim) is not WorkerClaim or pending is None
                    or claim.intent_id != pending.intent_id
                    or claim.approval_epoch != self.control.approval_epoch
                    or _instant(claim.claimed_at) < _instant(pending.reserved_at)):
                raise InvalidControlState("Claim not bound to reserved intent and approval")

    def encode(self):
        data = json.loads(self.control.encode())
        data['version'] = VERSION
        data['worker_claim'] = None if self.worker_claim is None else asdict(self.worker_claim)
        return json.dumps(data, sort_keys=True, separators=(',', ':')) + '\n'

    @classmethod
    def decode(cls, payload):
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise InvalidControlState("Duplicate offline control field")
                result[key] = value
            return result

        try:
            if type(payload) is bytes:
                if len(payload) > MAX_STATE_BYTES:
                    raise InvalidControlState("Offline control payload too large")
                payload = payload.decode('utf-8')
            if type(payload) is not str or len(payload.encode('utf-8')) > MAX_STATE_BYTES:
                raise InvalidControlState("Offline control payload unavailable or too large")
            data = json.loads(payload, object_pairs_hook=pairs)
            if (type(data) is not dict or frozenset(data) != STATE_FIELDS | {'worker_claim'}
                    or type(data['version']) is not int or data['version'] != VERSION):
                raise InvalidControlState("Explicit offline V3 schema required")
            claim = data.pop('worker_claim')
            if claim is not None:
                if type(claim) is not dict or type(claim.get('owner')) is not dict:
                    raise InvalidControlState("Invalid offline claim schema")
                claim = WorkerClaim(**{**claim, 'owner': ClaimOwner(**claim['owner'])})
            data['version'] = 2  # Reuse validation, never rewrite a live V2 file.
            control = ControllerState.decode(json.dumps(data))
            return cls(control, claim)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise InvalidControlState("Invalid offline V3 document") from None


@dataclass(frozen=True)
class ClaimResult:
    claim_confirmed: bool
    reason: str
    requires_reconciliation: bool = False

    @property
    def execution_allowed(self):
        return False  # No result, including a confirmed owner, grants execution.


@dataclass(frozen=True)
class StopResult:
    stopped: bool
    reason: str
    requires_reconciliation: bool = False


# A controller-owned trust/configuration boundary, never a worker argument or
# 'trusted=True' flag. No production adapter is instantiated or registered.
# Contract: read() -> (complete V3 JSON, opaque revision);
# compare_and_swap(expected_revision, complete_V3_JSON) -> exact bool;
# True means durably committed. Every mutation must receive a fresh revision,
# including OFF/ON or identical-content writes (no ABA/revision reuse).
CONTROLLER_STORE = None


def _store():
    store = CONTROLLER_STORE
    if (store is None or not callable(getattr(store, 'read', None))
            or not callable(getattr(store, 'compare_and_swap', None))):
        raise InvalidControlState("Trusted whole-state CAS store unavailable")
    return store


def _read(store):
    payload, revision = store.read()
    if type(revision) is not str or not 1 <= len(revision) <= 512:
        raise InvalidControlState("Opaque authoritative revision required")
    return OfflineControlStateV3.decode(payload), revision


def _clock(now):
    instant = now() if callable(now) else now
    if (not isinstance(instant, datetime) or instant.tzinfo is None
            or instant.utcoffset() is None):
        raise InvalidControlState("Independent aware clock required")
    instant = instant.astimezone(timezone.utc)
    _iso(instant)  # Canonical whole seconds, matching the controller schema.
    return instant


def claim_reserved_intent_offline(*, intent_id, approval_epoch, request_id,
                                  worker_oidc_token, now):
    """ONE CAS attempt; confirmation is accounting only, never a launch grant.

    After any ambiguous/conflicting result STOP. Re-reading is necessary but
    is not independent reconciliation; even an empty claim is no retry grant.
    A committed owner is never renewed, released, retried or taken over here.
    """
    if (not _hex(intent_id, 64) or not _hex(request_id, 32)
            or type(approval_epoch) is not int or not 1 <= approval_epoch < 100000000
            or type(worker_oidc_token) is not str or not worker_oidc_token):
        return ClaimResult(False, 'invalid_claim_request')
    try:
        store = _store()
        state, revision = _read(store)
    except Exception:
        return ClaimResult(False, 'authoritative_control_unavailable', True)
    control = state.control
    if not control.enabled:
        return ClaimResult(False, 'persistent_kill_switch_disabled')
    if control.approval_epoch != approval_epoch:
        return ClaimResult(False, 'approval_generation_mismatch')
    if control.pending is None or control.pending.intent_id != intent_id:
        return ClaimResult(False, 'reserved_intent_mismatch')
    if state.worker_claim is not None:
        # Also deny the same run/request after restart. A read cannot reissue
        # the one positive CAS confirmation or authorize another execution.
        return ClaimResult(False, 'intent_already_claimed_reconcile', True)
    try:
        instant = _clock(now)  # After the authoritative read, not before it.
        reserved = _instant(control.pending.reserved_at)
        if (not 0 <= (instant - reserved).total_seconds() <= STATUS_MAX_AGE.total_seconds()
                or control.daily_quota.day_utc != instant.date().isoformat()):
            return ClaimResult(False, 'reserved_intent_stale_or_future')
        authenticated = worker_identity.verify_controller_worker_token(worker_oidc_token, now=instant)
        if type(authenticated) is not worker_identity.ActionsOIDCIdentity:
            return ClaimResult(False, 'oidc_identity_unverified')
        owner = ClaimOwner(**{name: getattr(authenticated, name) for name in ClaimOwner.__dataclass_fields__})
        claim = WorkerClaim(intent_id, approval_epoch, request_id, owner, _iso(instant))
        proposed = replace(state, worker_claim=claim)
    except Exception:
        return ClaimResult(False, 'oidc_identity_or_clock_unverified')
    try:
        written = store.compare_and_swap(revision, proposed.encode())
    except Exception:
        return ClaimResult(False, 'claim_write_outcome_uncertain_reconcile', True)
    if written is not True:
        return ClaimResult(False, 'claim_cas_not_confirmed_reconcile', True)
    try:
        observed, observed_revision = _read(store)
    except Exception:
        return ClaimResult(False, 'claim_readback_unavailable_reconcile', True)
    if observed != proposed or observed_revision == revision:
        return ClaimResult(False, 'control_changed_or_readback_unverified_reconcile', True)
    return ClaimResult(True, 'synthetic_owner_confirmed_execution_denied')


def persist_offline_emergency_stop(*, operator_stop_authorized=False):
    """One whole-state OFF CAS and readback, preserving owner/intent/quota.

    A conflict/timeout never retries and cannot report a durable stop. Cancelling
    already-running work would require a separately authorized external action.
    """
    if operator_stop_authorized is not True:
        return StopResult(False, 'operator_stop_not_authorized')
    try:
        store = _store()
        state, revision = _read(store)
        proposed = replace(state, control=disable_controller(state.control))
        if state.control.enabled:
            if store.compare_and_swap(revision, proposed.encode()) is not True:
                return StopResult(False, 'stop_cas_not_confirmed_reconcile', True)
        observed, observed_revision = _read(store)
        if (observed != proposed or (state.control.enabled and observed_revision == revision)):
            return StopResult(False, 'stop_readback_unverified_reconcile', True)
    except Exception:
        return StopResult(False, 'stop_outcome_uncertain_reconcile', True)
    return StopResult(True, 'controller_off_owner_and_reserve_preserved')

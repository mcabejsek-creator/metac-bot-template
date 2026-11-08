"""Offline-only, CAS-ready future automatic dispatch-intent controller.

This module has NO network backend, no secrets, no GitHub dispatch calls,
no Metaculus access, and NO way to enable its default-disabled state.
A future separately authorized adapter must implement persistent compare-and-
swap (e.g. GitHub Contents expected blob SHA or non-force fast-forward ref)
on an independent control branch, never the publication journal branch.

A reservation is durable only *after* the store's CAS reports success. Even
after success this module CANNOT dispatch a workflow. Any uncertain delivery
must remain blocked until verified reconciliation in a future reviewed PR.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re

from scheduler_policy import DispatchSnapshot, DispatchDecision, evaluate_dispatch, POLL_INTERVAL

VERSION = 1
STATE_FIELDS = frozenset((
    "version", "enabled", "approval_epoch", "last_intent_at", "last_slot", "pending",
))
PENDING_FIELDS = frozenset(("intent_id", "reserved_at", "slot"))
_SLOT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:(?:00|15|30|45):00Z")
_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_HEX = re.compile(r"[0-9a-f]{64}")
STATE_MAX_BYTES = 4096


class InvalidControlState(ValueError):
    """Unknown, corrupt, stale or forged controller metadata must fail closed."""


def _instant(value):
    if not isinstance(value, str) or not _INSTANT.fullmatch(value):
        raise InvalidControlState("Controller timestamp must be exact UTC seconds")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidControlState("Impossible UTC timestamp") from None
    if moment.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise InvalidControlState("Non-canonical UTC timestamp")
    return moment


def _iso(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidControlState("Timezone-aware dispatch timestamp required")
    value = value.astimezone(timezone.utc)
    if value.microsecond:
        raise InvalidControlState("Controller uses whole seconds only")
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class PendingIntent:
    intent_id: str
    reserved_at: str
    slot: str

    def __post_init__(self):
        if not isinstance(self.intent_id, str) or not _HEX.fullmatch(self.intent_id):
            raise InvalidControlState("Invalid dispatch intent identifier")
        when = _instant(self.reserved_at)
        slot = _instant(self.slot)
        if not _SLOT.fullmatch(self.slot) or not slot <= when < slot + POLL_INTERVAL:
            raise InvalidControlState("Invalid 15-minute dispatch window")


@dataclass(frozen=True)
class ControllerState:
    version: int = VERSION
    enabled: bool = False
    approval_epoch: int = 0
    last_intent_at: str | None = None
    last_slot: str | None = None
    pending: PendingIntent | None = None

    def __post_init__(self):
        if type(self.version) is not int or self.version != VERSION:
            raise InvalidControlState("Unknown dispatch state schema version")
        if type(self.enabled) is not bool:
            raise InvalidControlState("Controller activation must be a boolean")
        if type(self.approval_epoch) is not int or not 0 <= self.approval_epoch < 100000000:
            raise InvalidControlState("Invalid activation generation")
        if self.enabled and self.approval_epoch == 0:
            raise InvalidControlState("Unapproved controller cannot be enabled")
        if (self.last_intent_at is None) != (self.last_slot is None):
            raise InvalidControlState("Incomplete last dispatch record")
        if self.last_intent_at is not None:
            when = _instant(self.last_intent_at)
            slot = _instant(self.last_slot)
            if not _SLOT.fullmatch(self.last_slot) or not slot <= when < slot + POLL_INTERVAL:
                raise InvalidControlState("Invalid previous dispatch window")
        if self.pending is not None:
            if not isinstance(self.pending, PendingIntent):
                raise InvalidControlState("Pending intent must use exact schema")
            if (self.last_intent_at != self.pending.reserved_at
                    or self.last_slot != self.pending.slot):
                raise InvalidControlState("Pending dispatch not linked to last reservation")
            expected = _intent_id(self.approval_epoch, self.pending.slot)
            if self.pending.intent_id != expected:
                raise InvalidControlState("Pending intent does not match approval epoch")

    @classmethod
    def decode(cls, payload):
        if isinstance(payload, bytes):
            if len(payload) > STATE_MAX_BYTES:
                raise InvalidControlState("Controller state too large")
            try:
                payload = payload.decode("utf-8")
            except UnicodeError:
                raise InvalidControlState("Controller state is not UTF-8") from None
        if not isinstance(payload, str) or len(payload.encode("utf-8")) > STATE_MAX_BYTES:
            raise InvalidControlState("Controller state too large or missing")
        try:
            obj = json.loads(payload)
        except (ValueError, TypeError):
            raise InvalidControlState("Invalid controller JSON") from None
        if not isinstance(obj, dict) or frozenset(obj) != STATE_FIELDS:
            raise InvalidControlState("Controller schema missing or has unknown fields")
        pending = obj["pending"]
        if pending is not None:
            if not isinstance(pending, dict) or frozenset(pending) != PENDING_FIELDS:
                raise InvalidControlState("Invalid pending intent schema")
            pending = PendingIntent(**pending)
        return cls(**{**obj, "pending": pending})

    def encode(self):
        data = {
            "version": self.version,
            "enabled": self.enabled,
            "approval_epoch": self.approval_epoch,
            "last_intent_at": self.last_intent_at,
            "last_slot": self.last_slot,
            "pending": None if self.pending is None else {
                "intent_id": self.pending.intent_id,
                "reserved_at": self.pending.reserved_at,
                "slot": self.pending.slot,
            },
        }
        return json.dumps(data, separators=(",", ":"), sort_keys=True) + "\n"


def _intent_id(epoch, slot):
    # Stable public deduplication ID, never an API credential or signature.
    return sha256(f"vrchcab-autonomous-v1|{epoch}|{slot}".encode("ascii")).hexdigest()


@dataclass(frozen=True)
class ReservationResult:
    allowed: bool
    reason: str
    state: ControllerState
    intent_id: str | None = None


def propose_reservation(state, snapshot, *, approval_epoch):
    """Pure check and proposal. NOT durable until an external atomic CAS commits."""
    if not isinstance(state, ControllerState):
        raise InvalidControlState("Validated control state required")
    if not isinstance(snapshot, DispatchSnapshot):
        raise InvalidControlState("Validated scheduling snapshot required")
    if state.enabled is not True or state.approval_epoch == 0:
        return ReservationResult(False, "persistent_kill_switch_disabled", state)
    if type(approval_epoch) is not int or approval_epoch != state.approval_epoch:
        return ReservationResult(False, "approval_generation_mismatch", state)
    if state.pending is not None:
        return ReservationResult(False, "pending_or_uncertain_dispatch_requires_reconciliation", state)
    decision = evaluate_dispatch(snapshot)
    if not decision.allowed:
        return ReservationResult(False, decision.reason, state)

    stamp = _iso(snapshot.now)
    when = _instant(stamp)
    slot = when.replace(minute=15*(when.minute//15), second=0)
    slot_text = _iso(slot)
    if state.last_slot is not None:
        if slot_text <= state.last_slot:
            return ReservationResult(False, "previous_or_replayed_poll_slot", state)
        # Cross-check the persistent last-intent clock, not only the
        # caller-supplied scheduler snapshot (which might be incomplete).
        previous = _instant(state.last_intent_at)
        if when - previous < POLL_INTERVAL:
            return ReservationResult(False, "persistent_min_dispatch_spacing", state)
    intent = PendingIntent(
        intent_id=_intent_id(state.approval_epoch, slot_text),
        reserved_at=stamp,
        slot=slot_text,
    )
    new_state = replace(
        state, last_intent_at=stamp, last_slot=slot_text, pending=intent,
    )
    return ReservationResult(True, "needs_durable_compare_and_swap", new_state, intent.intent_id)


def reserve_with_store(store, snapshot, *, approval_epoch):
    """Require a *durable*, authoritative CAS before releasing an intent.

    Store protocol: read() -> (ControllerState, opaque_revision);
    compare_and_swap(opaque_revision, new_state) -> EXACT bool.
    A missing/unreachable store MUST raise; never initialize active state.
    This method never sends a workflow_dispatch event.
    """
    state, revision = store.read()
    if not isinstance(state, ControllerState) or not isinstance(revision, str) or not revision:
        raise InvalidControlState("Authoritative control state or revision unavailable")
    proposal = propose_reservation(state, snapshot, approval_epoch=approval_epoch)
    if not proposal.allowed:
        return proposal
    success = store.compare_and_swap(revision, proposal.state)
    if success is not True:
        # Never retry an unclear or conflicting reservation.
        return ReservationResult(False, "durable_cas_failed_no_dispatch", state)
    return replace(proposal, reason="durably_reserved_not_dispatched")


def disable_controller(state):
    """Pure emergency kill-switch transition, preserving uncertain intent.

    Future production adapter must persist this via CAS and cancel any
    already queued/running GitHub workflow separately.
    """
    if not isinstance(state, ControllerState):
        raise InvalidControlState("Validated control state required")
    return replace(state, enabled=False)

"""Synthetic reserve/bind commands over ONE item; no server or production hook.

Ingress authentication, evidence and physical binding are injected by a trusted
TEST harness. Python types/names/private members are not process isolation or
artifact attestation. No network client, dispatch, paid call or send capability
exists here. The old command schema, claim engine and production anchors stay
unchanged; this explicit envelope is not a V2/V3 migration.
"""

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json

import command_gateway_offline as commands
import dynamodb_cas_offline as engine
import worker_configuration
from autonomous_orchestrator import IndependentReadings, build_dispatch_snapshot
from dispatch_intent_controller import DailyQuota, _instant, _iso, _strict_money, propose_reservation
from perplexity_run_budget import MAX_AUTO_AGENT_POST_ATTEMPTS
from worker_claim_offline import _hex
from worker_identity import ActionsOIDCIdentity, ControllerOIDCPolicy


ADMISSION_SCHEMA = 'offline-admission-v1'
MAX_ITEM_BYTES = 16384  # Synthetic envelope limit, not an AWS service guarantee.
_BASE_FIELDS = frozenset(('pk', 'incarnation', 'generation', 'document',
                          'command_schema', 'run_authorization'))


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             allow_nan=False, default=lambda v: v.isoformat()).encode()).hexdigest()


@dataclass(frozen=True)
class SyntheticAtomicityPlan:
    """ONE fixed, explicitly fake plan; never an override for WORKER_CONFIG.

    Only an injected test authority may select it. No command field selects a
    plan. This is not an alternative mode, supervised switch or worker config.
    """

    name: str = 'SYNTHETIC-ATOMICITY-ONLY'
    agent_posts: int = 2

    def __post_init__(self):
        if self.name != 'SYNTHETIC-ATOMICITY-ONLY' or type(self.agent_posts) is not int or self.agent_posts != 2:
            raise engine.StoreUnavailable('Only the fixed synthetic atomicity plan is supported')

    @property
    def configuration_digest(self):
        return _digest(asdict(self))


@dataclass(frozen=True)
class SyntheticCodeScope:
    """Independent synthetic approval scope, excluding the not-yet-bound run ID."""

    audience: str
    subject: str
    repository: str
    repository_id: str
    repository_owner_id: str
    source_sha: str
    workflow_ref: str
    workflow_sha: str
    configuration_digest: str
    artifact_digest: str
    max_token_lifetime_seconds: int = 600

    def __post_init__(self):
        ControllerOIDCPolicy(**self.policy_fields(), run_id='1')  # Shape check, not approval.
        if not _hex(self.configuration_digest, 64) or not _hex(self.artifact_digest, 64):
            raise engine.StoreUnavailable('Exact synthetic config and artifact scope required')

    def policy_fields(self):
        return {name: getattr(self, name) for name in ControllerOIDCPolicy.__dataclass_fields__ if name != 'run_id'}

    def matches(self, policy):
        return type(policy) is ControllerOIDCPolicy and all(
            getattr(policy, name) == value for name, value in self.policy_fields().items())


@dataclass(frozen=True)
class SyntheticReservationEvidence:
    """Authority OUTPUT, never command payload or a caller's verified flag.

    The authentication service must independently associate this evidence with
    its ingress context and exact whole-item revision. plan=None examines the
    real shared worker plan and is DENY today. A Python instance alone is not
    authentication; a compromised service in the TCB can fabricate evidence.
    """

    expected_revision: str
    approval_epoch: int
    evidence_id: str
    issued_at: str
    valid_until: str
    trust_generation: int
    readings: IndependentReadings
    scope: SyntheticCodeScope
    plan: SyntheticAtomicityPlan | None = None


@dataclass(frozen=True)
class ReservationRecord:
    evidence_id: str
    intent_id: str
    approval_epoch: int
    reserved_at: str
    issued_at: str
    valid_until: str
    trust_generation: int
    run_reserve_usd: str
    quota: DailyQuota
    preflight_digest: str
    scope: SyntheticCodeScope
    plan: SyntheticAtomicityPlan

    def __post_init__(self):
        if (not _hex(self.evidence_id, 32) or not _hex(self.intent_id, 64)
                or not _hex(self.preflight_digest, 64) or type(self.approval_epoch) is not int
                or not 1 <= self.approval_epoch < 100000000
                or type(self.trust_generation) is not int or self.trust_generation < 1
                or type(self.quota) is not DailyQuota or type(self.scope) is not SyntheticCodeScope
                or type(self.plan) is not SyntheticAtomicityPlan
                or self.scope.configuration_digest != self.plan.configuration_digest
                or not 0 < _strict_money(self.run_reserve_usd) <= _strict_money(self.quota.reserved_usd)
                or not 0 < (_instant(self.valid_until) - _instant(self.issued_at)).total_seconds() <= 300
                or not _instant(self.issued_at) <= _instant(self.reserved_at) < _instant(self.valid_until)):
            raise engine.StoreUnavailable('Invalid immutable reservation record')

    @classmethod
    def decode(cls, data):
        if type(data) is not dict or set(data) != set(cls.__dataclass_fields__):
            raise engine.StoreUnavailable('Exact reservation record required')
        return cls(**{**data, 'scope': SyntheticCodeScope(**data['scope']),
                      'plan': SyntheticAtomicityPlan(**data['plan']), 'quota': DailyQuota(**data['quota'])})


@dataclass(frozen=True)
class SyntheticApprovedRun:
    """Independent test-service output after checking approval AND signed JWT.

    Artifact evidence here is synthetic. A valid JWT alone never attests the
    executed code, prompts or dependencies, and cannot enable real execution.
    """

    expected_revision: str
    evidence_id: str
    authorization: commands.RunAuthorizationDraft
    identity: ActionsOIDCIdentity
    configuration_digest: str
    artifact_digest: str


@dataclass(frozen=True)
class AdmissionSnapshot:
    base: commands.CommandSnapshotDraft
    reservation: ReservationRecord | None

    def __post_init__(self):
        if type(self.base) is not commands.CommandSnapshotDraft:
            raise engine.StoreUnavailable('Existing whole command core required')
        control, owner = self.base.core.state.control, self.base.core.state.worker_claim
        record, auth = self.reservation, self.base.authorization
        if record is None:
            if control.pending is not None or owner is not None or auth is not None:
                raise engine.StoreUnavailable('No seeded reservation/binding in admission bootstrap')
        elif (type(record) is not ReservationRecord or control.pending is None
                or (record.intent_id, record.approval_epoch, record.reserved_at, record.quota)
                != (control.pending.intent_id, control.approval_epoch,
                    control.pending.reserved_at, control.daily_quota)):
            raise engine.StoreUnavailable('Reservation, epoch and exact held quota disagree')
        if auth is not None and (not record.scope.matches(auth.policy)
                or auth.trust_generation != record.trust_generation
                or not _instant(record.reserved_at) <= _instant(auth.approved_at)
                < _instant(auth.valid_until) <= _instant(record.valid_until)):
            raise engine.StoreUnavailable('Immutable run not bound to reservation approval scope')

    @property
    def revision(self):
        return 'admission-offline/v1:' + json.dumps([
            self.base.revision, _digest(None if self.reservation is None else asdict(self.reservation))],
            separators=(',', ':'))

    def item(self):
        result = {**self.base.item(), 'admission_schema': engine._av(ADMISSION_SCHEMA),
                  'reservation': engine._av(None if self.reservation is None else asdict(self.reservation))}
        if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_ITEM_BYTES:
            raise engine.StoreUnavailable('Admission envelope size exceeded')
        return result

    @classmethod
    def decode(cls, item, binding):
        if (type(item) is not dict or set(item) != _BASE_FIELDS | {'admission_schema', 'reservation'}
                or item['admission_schema'] != engine._av(ADMISSION_SCHEMA)
                or len(json.dumps(item, ensure_ascii=False).encode()) > MAX_ITEM_BYTES):
            raise engine.StoreUnavailable('Explicit complete admission envelope required; no migration')
        base = commands.CommandSnapshotDraft.decode({name: item[name] for name in _BASE_FIELDS}, binding)
        record = engine._value(item['reservation'])
        result = cls(base, None if record is None else ReservationRecord.decode(record))
        result.item()
        return result

    def view(self):
        return {**self.base.view(), 'admission_schema': ADMISSION_SCHEMA,
                'reservation': None if self.reservation is None else asdict(self.reservation)}


class _AdmissionProjection:
    """Reuse existing claim/STOP, freezing the reservation in the same CAS."""

    def __init__(self, raw, binding, record):
        self.raw, self.binding, self.record = raw, binding, record

    def _project(self, item):
        snapshot = AdmissionSnapshot.decode(item, self.binding)
        if snapshot.reservation != self.record:
            raise engine.StoreUnavailable('Reservation approval changed')
        return snapshot.base.item()

    def get_item(self, **request):
        response = self.raw.get_item(**request)
        engine._ack(response)
        return {**response, 'Item': self._project(response.get('Item'))}

    def update_item(self, **request):
        request = deepcopy(request)
        request['ExpressionAttributeNames'].update({'#ads': 'admission_schema', '#reservation': 'reservation'})
        request['ExpressionAttributeValues'].update({':ads': engine._av(ADMISSION_SCHEMA),
            ':reservation': engine._av(None if self.record is None else asdict(self.record))})
        request['ConditionExpression'] += ' AND #ads = :ads AND #reservation = :reservation'
        request = engine._pin_envelope(request, {
            'admission_schema': engine._av(ADMISSION_SCHEMA),
            'reservation': request['ExpressionAttributeValues'][':reservation']})
        response = self.raw.update_item(**request)
        engine._ack(response)
        return {**response, 'Attributes': self._project(response.get('Attributes'))}


class OfflineAdmissionGateway:
    """Trusted fixture composition only. No endpoint, factory or production anchor.

    Controller-only narrow commands; no caller-provided state, owner, plan,
    policy, expressions, next generation, approval facts or storage binding.
    """

    def __init__(self, *, transport, binding, authentication=None, clock=None):
        self._raw, self._binding, self._auth = transport, binding, authentication
        self._identity = commands.OfflineCommandGateway(
            transport=transport, binding=binding, authentication=authentication, clock=clock)

    def _read(self):
        response = self._raw.get_item(TableName=self._binding.table_name,
            Key={'pk': engine._av(self._binding.partition_key)}, ConsistentRead=True)
        engine._ack(response)
        return AdmissionSnapshot.decode(response.get('Item'), self._binding)

    def _evidence(self, method, invocation, before, actor):
        if self._identity._actor('reader', invocation, {'controller'}) != actor:
            raise engine.StoreUnavailable('Controller principal changed')
        verify = getattr(self._auth, method, None)
        if not callable(verify):
            raise engine.StoreUnavailable('Independent approval service absent')
        evidence = verify(invocation, before, now=self._identity._now())
        if evidence.expected_revision != before.revision:
            raise engine.StoreUnavailable('Approval belongs to another whole-item revision')
        return evidence

    def _reserve(self, before, invocation, actor, epoch):
        control = before.base.core.state.control
        if before.reservation is not None or control.pending is not None or before.base.authorization is not None:
            raise engine.StoreUnavailable('Reservation already exists; reconcile')
        proof = self._evidence('reservation', invocation, before, actor)
        now = self._identity._now()
        if (type(proof) is not SyntheticReservationEvidence or proof.approval_epoch != epoch
                or type(proof.readings) is not IndependentReadings
                or type(proof.scope) is not SyntheticCodeScope
                or not _instant(proof.issued_at) <= now < _instant(proof.valid_until)):
            raise engine.StoreUnavailable('Fresh independent reservation evidence required')
        if proof.plan is None:
            # No replacement of source config, env tricks, or supervised branch.
            if worker_configuration.WORKER_CONFIG.agent_requests_per_atomic_question > MAX_AUTO_AGENT_POST_ATTEMPTS:
                raise engine.StoreUnavailable('Real worker research plan exceeds automatic cap')
            raise engine.StoreUnavailable('Real runtime/artifact authority is not implemented')
        if type(proof.plan) is not SyntheticAtomicityPlan or proof.plan.agent_posts > MAX_AUTO_AGENT_POST_ATTEMPTS:
            raise engine.StoreUnavailable('Only isolated synthetic atomicity admission supported')
        last = None if control.last_intent_at is None else _instant(control.last_intent_at)
        snapshot = build_dispatch_snapshot(proof.readings, now=now, last_dispatch_at=last)
        proposal = propose_reservation(control, snapshot, approval_epoch=epoch)
        if not proposal.allowed:
            raise engine.StoreUnavailable('Existing strict V2 preflight rejected reservation')
        record = ReservationRecord(proof.evidence_id, proposal.intent_id, epoch, _iso(now),
            proof.issued_at, proof.valid_until, proof.trust_generation,
            proof.readings.daily_spend.approved_run_reserve_usd, proposal.state.daily_quota,
            _digest(asdict(proof.readings)), proof.scope, proof.plan)
        base = replace(before.base, core=replace(before.base.core,
            state=replace(before.base.core.state, control=proposal.state), generation=before.base.core.generation+1))
        return AdmissionSnapshot(base, record)

    def _bind(self, before, invocation, actor):
        record = before.reservation
        if record is None or before.base.authorization is not None or before.base.core.state.worker_claim is not None:
            raise engine.StoreUnavailable('Bind requires an unbound, unowned durable reservation')
        proof = self._evidence('approved_run', invocation, before, actor)
        now = self._identity._now()
        if (type(proof) is not SyntheticApprovedRun or proof.evidence_id != record.evidence_id
                or type(proof.authorization) is not commands.RunAuthorizationDraft
                or type(proof.identity) is not ActionsOIDCIdentity
                or proof.configuration_digest != record.scope.configuration_digest
                or proof.artifact_digest != record.scope.artifact_digest
                or not _instant(record.reserved_at) <= now < _instant(record.valid_until)
                or not _instant(proof.authorization.approved_at) <= now < _instant(proof.authorization.valid_until)
                or type(proof.identity.expires_at) is not int or proof.identity.expires_at <= now.timestamp()):
            raise engine.StoreUnavailable('Independent run/identity/artifact approval unavailable')
        if any(getattr(proof.identity, name) != value for name, value in
               asdict(commands._policy_owner(proof.authorization.policy)).items()):
            raise engine.StoreUnavailable('Verified run identity differs from independent approval')
        base = replace(before.base, authorization=proof.authorization,
                       core=replace(before.base.core, generation=before.base.core.generation+1))
        return AdmissionSnapshot(base, record)

    def _write(self, before, after, name, check):
        """Private fixed-transition writer. One send; never readback-auth or retry."""
        generation = before.base.core.generation
        if generation >= engine.MAX_GENERATION-1:
            return commands.CommandResult(False, 'generation_headroom_exhausted')
        old, new = before.item(), after.item()
        names = {f'#f{i}': key for i, key in enumerate(sorted(old))}
        aliases = {key: alias for alias, key in names.items()}
        values = {f':f{i}': old[key] for i, key in enumerate(sorted(old))}
        condition = 'attribute_exists(' + aliases['pk'] + ') AND ' + ' AND '.join(
            f'{alias} = :f{i}' for i, alias in enumerate(names))
        names.update({'#on': 'enabled', '#epoch': 'approval_epoch', '#pending': 'pending', '#owner': 'worker_claim'})
        values.update({':limit': engine._av(engine.MAX_GENERATION-1), ':one': engine._av(1),
                       ':on': engine._av(True), ':epoch': engine._av(before.base.core.state.control.approval_epoch),
                       ':null': engine._av('NULL')})
        document, auth, gen = (aliases[k] for k in ('document', 'run_authorization', 'generation'))
        condition += (f' AND {gen} < :limit AND {document}.#on = :on AND {document}.#epoch = :epoch'
                      f' AND attribute_type({document}.#owner, :null) AND attribute_type({auth}, :null)')
        if name == 'reserve_intent':
            condition += f' AND attribute_type({document}.#pending, :null)'
            values.update({':documentAfter': new['document'], ':recordAfter': new['reservation']})
            update = f'SET {document} = :documentAfter, {aliases["reservation"]} = :recordAfter, {gen} = {gen} + :one'
        else:
            names['#intent'], values[':intent'] = 'intent_id', engine._av(before.reservation.intent_id)
            condition += f' AND {document}.#pending.#intent = :intent'
            values[':authAfter'] = new['run_authorization']
            update = f'SET {auth} = :authAfter, {gen} = {gen} + :one'
        try:
            if check() != after:
                raise engine.StoreUnavailable('Approval, plan, clock or evidence changed before send')
        except Exception:
            return commands.CommandResult(False, 'admission_authority_changed_reconcile', True)
        try:
            request = dict(TableName=self._binding.table_name,
                Key={'pk': engine._av(self._binding.partition_key)}, ConditionExpression=condition,
                UpdateExpression=update, ExpressionAttributeNames=names,
                ExpressionAttributeValues=values, ReturnValues='ALL_NEW')
            response = self._raw.update_item(**engine._fence_envelope(request, old))
            engine._ack(response)
            if AdmissionSnapshot.decode(response.get('Attributes'), self._binding) != after or self._read() != after:
                return commands.CommandResult(False, 'admission_ack_or_readback_unverified', True)
        except Exception:
            return commands.CommandResult(False, 'admission_write_uncertain_or_conflicting', True)
        return commands.CommandResult(True, 'synthetic_accounting_confirmed_execution_denied', revision=after.revision)

    def handle(self, name, payload, *, invocation=None):
        if name not in ('reserve_intent', 'bind_approved_run'):
            if not commands.OfflineCommandGateway._request(name, payload):
                return commands.CommandResult(False, 'command_schema_rejected')
        else:
            fields = {'expected_revision', 'approval_epoch'} | ({'intent_id'} if name == 'bind_approved_run' else set())
            if (type(payload) is not dict or set(payload) != fields
                    or type(payload['expected_revision']) is not str or not 1 <= len(payload['expected_revision']) <= 1536
                    or type(payload['approval_epoch']) is not int or not 1 <= payload['approval_epoch'] < 100000000
                    or (name == 'bind_approved_run' and not _hex(payload['intent_id'], 64))):
                return commands.CommandResult(False, 'command_schema_rejected')
        try:
            actor = self._identity._actor('operator' if name == 'emergency_stop' else 'reader', invocation,
                {'controller'} if name in ('reserve_intent', 'bind_approved_run') else
                {'operator'} if name == 'emergency_stop' else {'worker', 'operator', 'controller'})
            before = self._read()
            if name == 'read_control':
                return commands.CommandResult(True, 'synthetic_control_observed_execution_denied',
                                               revision=before.revision, control=before.view())
            if before.revision != payload['expected_revision']:
                return commands.CommandResult(False, 'revision_conflict_reconcile', True)
            if name in ('claim_reserved_intent', 'emergency_stop'):
                projection = _AdmissionProjection(self._raw, self._binding, before.reservation)
                gateway = commands.OfflineCommandGateway(transport=projection, binding=self._binding,
                    authentication=self._auth, clock=self._identity._clock)
                return gateway.handle(name, {**payload, 'expected_revision': before.base.revision}, invocation=invocation)
            control = before.base.core.state.control
            if not control.enabled or control.approval_epoch != payload['approval_epoch']:
                return commands.CommandResult(False, 'persistent_off_or_epoch_mismatch')
            if name == 'bind_approved_run' and (control.pending is None or control.pending.intent_id != payload['intent_id']):
                return commands.CommandResult(False, 'reserved_intent_mismatch')
            prepare = (lambda: self._reserve(before, invocation, actor, payload['approval_epoch'])) if name == 'reserve_intent' else (
                lambda: self._bind(before, invocation, actor))
            after = prepare()
            after.item()
        except Exception:
            return commands.CommandResult(False, 'admission_authority_or_state_unavailable', True)
        return self._write(before, after, name, prepare)

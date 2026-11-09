"""One durable synthetic effect slot, no live transport or production authority.

Multiple DB requests are allowed across explicit invocations. Only a confirmed
NOT_STARTED -> ATTEMPT_COMMITTED CAS can create a non-serializable continuation.
Reading that state never creates another continuation. No lease/reset/takeover.
The prototype deliberately supports ONE fixed synthetic operation per intent.
"""

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import re
from threading import Lock

import command_gateway_offline as commands
import dynamodb_cas_offline as engine
from dispatch_intent_controller import _instant, _iso, _strict_money
from worker_claim_offline import _hex


RECOVERY_SCHEMA = 'offline-durable-effect-v1'
NOT_STARTED = 'NOT_STARTED'
ATTEMPT_COMMITTED = 'ATTEMPT_COMMITTED'
OUTCOME_UNKNOWN = 'OUTCOME_UNKNOWN'
OUTCOME_VERIFIED = 'OUTCOME_INDEPENDENTLY_VERIFIED'
MAX_ITEM_BYTES = 16384  # Prototype bound, not a DynamoDB service limit.
_BASE_FIELDS = frozenset(('pk', 'incarnation', 'generation', 'document',
                          'command_schema', 'run_authorization'))


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class EffectDefinition:
    """Fixed by a trusted fixture/future controller, NEVER worker request data."""

    intent_id: str
    approval_epoch: int
    authorization_id: str
    run_id: str
    configuration_digest: str
    semantic_key: str
    kind: str
    provider_id: str
    payload: str
    intent_reserved_usd: str
    request_cap_usd: str

    def __post_init__(self):
        if (not _hex(self.intent_id, 64) or not _hex(self.authorization_id, 32)
                or not _hex(self.configuration_digest, 64)
                or type(self.approval_epoch) is not int or not 1 <= self.approval_epoch < 100000000
                or type(self.run_id) is not str or re.fullmatch(r'[1-9][0-9]{0,19}', self.run_id) is None
                or type(self.semantic_key) is not str
                or re.fullmatch(r'[a-z0-9_.:-]{1,64}', self.semantic_key) is None
                or self.kind not in {'paid_model', 'github_dispatch', 'metaculus_forecast', 'metaculus_comment'}
                or type(self.provider_id) is not str or not self.provider_id.startswith('synthetic://')
                or not 13 <= len(self.provider_id) <= 128
                or type(self.payload) is not str or not self.payload.startswith('SYNTHETIC:')
                or len(self.payload.encode()) > 512):
            raise engine.StoreUnavailable('Explicit fixed synthetic operation required')
        reserve, cap = _strict_money(self.intent_reserved_usd), _strict_money(self.request_cap_usd)
        if not 0 < cap <= reserve:
            raise engine.StoreUnavailable('Invalid fixed operation budget')

    @property
    def request_digest(self):
        return _digest([self.provider_id, self.kind, self.semantic_key, self.payload])


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    committed_generation: int
    committed_at: str
    owner_digest: str

    def __post_init__(self):
        if (not _hex(self.attempt_id, 64) or not _hex(self.owner_digest, 64)
                or type(self.committed_generation) is not int
                or not 2 <= self.committed_generation <= engine.MAX_GENERATION):
            raise engine.StoreUnavailable('Invalid durable attempt')
        _instant(self.committed_at)


@dataclass(frozen=True)
class IndependentEffectEvidence:
    """Output of a separate synthetic authority, not a caller's verified flag.

    Only positive, independently confirmed effect AND known billing are modeled.
    Absence of a record is deliberately not converted into NO_EFFECT or a retry.
    """

    operation_id: str
    attempt_id: str
    request_digest: str
    source_reference: str
    result_digest: str
    charged_usd: str
    checked_at: str

    def __post_init__(self):
        if (any(not _hex(value, 64) for value in (
                self.operation_id, self.attempt_id, self.request_digest, self.result_digest))
                or type(self.source_reference) is not str
                or not self.source_reference.startswith('synthetic://')
                or not 13 <= len(self.source_reference) <= 200):
            raise engine.StoreUnavailable('Independent synthetic evidence required')
        _strict_money(self.charged_usd)
        _instant(self.checked_at)


@dataclass(frozen=True)
class EffectRecord:
    operation_id: str
    definition: EffectDefinition
    phase: str = NOT_STARTED
    attempt: AttemptRecord | None = None
    observation_digest: str | None = None
    evidence: IndependentEffectEvidence | None = None


def operation_id(base, definition):
    return _digest(['vrchcab-fixed-effect-v1', asdict(base.core.binding), asdict(definition)])


def _attempt_id(snapshot):
    return _digest(['vrchcab-effect-attempt-v1', snapshot.effect.operation_id,
                    asdict(snapshot.base.core.state.worker_claim.owner)])


@dataclass(frozen=True)
class RecoverySnapshot:
    base: commands.CommandSnapshotDraft
    effect: EffectRecord

    def __post_init__(self):
        if type(self.base) is not commands.CommandSnapshotDraft or type(self.effect) is not EffectRecord:
            raise engine.StoreUnavailable('Whole recovery item required')
        effect, auth = self.effect, self.base.authorization
        control, claim = self.base.core.state.control, self.base.core.state.worker_claim
        definition = effect.definition
        if (type(definition) is not EffectDefinition or auth is None or control.pending is None
                or (definition.intent_id, definition.approval_epoch, definition.authorization_id, definition.run_id)
                != (control.pending.intent_id, control.approval_epoch, auth.authorization_id, auth.policy.run_id)
                or _strict_money(definition.intent_reserved_usd) > _strict_money(control.daily_quota.reserved_usd)
                or effect.operation_id != operation_id(self.base, definition)
                or effect.phase not in {NOT_STARTED, ATTEMPT_COMMITTED, OUTCOME_UNKNOWN, OUTCOME_VERIFIED}):
            raise engine.StoreUnavailable('Effect not bound to intent, run, budget and authority')
        if effect.phase == NOT_STARTED:
            if any(value is not None for value in (effect.attempt, effect.observation_digest, effect.evidence)):
                raise engine.StoreUnavailable('Unstarted effect cannot contain a spent attempt')
            return
        attempt = effect.attempt
        if (type(attempt) is not AttemptRecord or claim is None
                or attempt.attempt_id != _attempt_id(self)
                or attempt.owner_digest != _digest(asdict(claim.owner))
                or attempt.committed_generation > self.base.core.generation
                or not _instant(claim.claimed_at) <= _instant(attempt.committed_at) < _instant(auth.valid_until)):
            raise engine.StoreUnavailable('Attempt not bound to immutable owner and generation')
        if effect.observation_digest is not None and not _hex(effect.observation_digest, 64):
            raise engine.StoreUnavailable('Invalid advisory observation')
        if effect.phase == ATTEMPT_COMMITTED and effect.observation_digest is not None:
            raise engine.StoreUnavailable('Committed attempt is not a provider response')
        if effect.phase != OUTCOME_VERIFIED:
            if effect.evidence is not None:
                raise engine.StoreUnavailable('Unverified effect cannot contain settlement evidence')
        else:
            proof = effect.evidence
            if (type(proof) is not IndependentEffectEvidence
                    or (proof.operation_id, proof.attempt_id, proof.request_digest)
                    != (effect.operation_id, attempt.attempt_id, definition.request_digest)
                    or _strict_money(proof.charged_usd) > _strict_money(definition.request_cap_usd)
                    or _instant(proof.checked_at) < _instant(attempt.committed_at)):
                raise engine.StoreUnavailable('Incomplete or mismatched independent outcome')

    @property
    def revision(self):
        return 'effect-offline/v1:' + json.dumps(
            [self.base.revision, _digest(asdict(self.effect))], separators=(',', ':'))

    def item(self):
        item = {**self.base.item(), 'recovery_schema': engine._av(RECOVERY_SCHEMA),
                'effect': engine._av(asdict(self.effect))}
        if len(json.dumps(item, ensure_ascii=False).encode()) > MAX_ITEM_BYTES:
            raise engine.StoreUnavailable('Recovery item bound exceeded')
        return item

    @classmethod
    def decode(cls, item, binding):
        if (type(item) is not dict or set(item) != _BASE_FIELDS | {'recovery_schema', 'effect'}
                or item['recovery_schema'] != engine._av(RECOVERY_SCHEMA)
                or len(json.dumps(item, ensure_ascii=False).encode()) > MAX_ITEM_BYTES):
            raise engine.StoreUnavailable('Explicit complete recovery draft required; no migration')
        base = commands.CommandSnapshotDraft.decode({name: item[name] for name in _BASE_FIELDS}, binding)
        data = engine._value(item['effect'])
        if type(data) is not dict or set(data) != set(EffectRecord.__dataclass_fields__):
            raise engine.StoreUnavailable('Exact effect record required')
        definition = EffectDefinition(**data['definition'])
        attempt = None if data['attempt'] is None else AttemptRecord(**data['attempt'])
        evidence = None if data['evidence'] is None else IndependentEffectEvidence(**data['evidence'])
        return cls(base, EffectRecord(**{**data, 'definition': definition, 'attempt': attempt, 'evidence': evidence}))

    def view(self):
        return {**self.base.view(), 'recovery_schema': RECOVERY_SCHEMA, 'effect': asdict(self.effect)}


class _RecoveryProjection:
    """Reuse unchanged claim/STOP engine, retaining effect in the SAME item."""

    def __init__(self, raw, binding, effect):
        self.raw, self.binding, self.effect = raw, binding, effect

    def _project(self, item):
        snapshot = RecoverySnapshot.decode(item, self.binding)
        if snapshot.effect != self.effect:
            raise engine.StoreUnavailable('Recovery state changed')
        return snapshot.base.item()

    def get_item(self, **request):
        response = self.raw.get_item(**request)
        engine._ack(response)
        return {**response, 'Item': self._project(response.get('Item'))}

    def update_item(self, **request):
        request = deepcopy(request)
        request['ExpressionAttributeNames'].update({'#rs': 'recovery_schema', '#effect': 'effect'})
        request['ExpressionAttributeValues'].update(
            {':rs': engine._av(RECOVERY_SCHEMA), ':effect': engine._av(asdict(self.effect))})
        request['ConditionExpression'] += ' AND #rs = :rs AND #effect = :effect'
        response = self.raw.update_item(**request)
        engine._ack(response)
        return {**response, 'Attributes': self._project(response.get('Attributes'))}


class _EffectStore:
    def __init__(self, raw, binding):
        self.raw, self.binding = raw, binding

    def read(self):
        response = self.raw.get_item(TableName=self.binding.table_name,
            Key={'pk': engine._av(self.binding.partition_key)}, ConsistentRead=True)
        engine._ack(response)
        return RecoverySnapshot.decode(response.get('Item'), self.binding)

    def advance(self, before, effect, *, check):
        """One conditional write, no retry. Only monotonic effect transitions."""
        try:
            transitions = {NOT_STARTED: {ATTEMPT_COMMITTED},
                           ATTEMPT_COMMITTED: {OUTCOME_UNKNOWN, OUTCOME_VERIFIED},
                           OUTCOME_UNKNOWN: {OUTCOME_VERIFIED}, OUTCOME_VERIFIED: set()}
            if (effect.phase not in transitions[before.effect.phase]
                    or effect.definition != before.effect.definition
                    or effect.operation_id != before.effect.operation_id
                    or (before.effect.attempt is not None and effect.attempt != before.effect.attempt)):
                raise engine.StoreUnavailable('Effect transition rejected')
            generation = before.base.core.generation
            limit = engine.MAX_GENERATION-1 if before.base.core.state.control.enabled else engine.MAX_GENERATION
            if generation >= limit:
                raise engine.StoreUnavailable('Preserve final increment for OFF')
            after = RecoverySnapshot(replace(before.base, core=replace(before.base.core,
                                      generation=generation+1)), effect)
            after.item()
            old = before.item()
            names = {'#pk': 'pk', '#i': 'incarnation', '#g': 'generation', '#d': 'document',
                     '#cs': 'command_schema', '#auth': 'run_authorization', '#rs': 'recovery_schema', '#f': 'effect'}
            values = {':pk': old['pk'], ':i': old['incarnation'], ':g': old['generation'],
                      ':d': old['document'], ':cs': old['command_schema'], ':auth': old['run_authorization'],
                      ':rs': old['recovery_schema'], ':f': old['effect'], ':limit': engine._av(limit),
                      ':one': engine._av(1), ':after': engine._av(asdict(effect))}
            condition = ('attribute_exists(#pk) AND #pk = :pk AND #i = :i AND #g = :g AND #g < :limit '
                         'AND #d = :d AND #cs = :cs AND #auth = :auth AND #rs = :rs AND #f = :f')
            if effect.phase == ATTEMPT_COMMITTED:
                if not before.base.core.state.control.enabled:
                    raise engine.StoreUnavailable('Persistent OFF')
                names['#on'], values[':on'] = 'enabled', engine._av(True)
                condition += ' AND #d.#on = :on'
            check()  # Independent server authorization immediately before send.
        except Exception:
            return engine.WriteObservation(False, 'effect_precondition_unavailable', True), None
        try:
            response = self.raw.update_item(TableName=self.binding.table_name,
                Key={'pk': engine._av(self.binding.partition_key)}, ConditionExpression=condition,
                UpdateExpression='SET #f = :after, #g = #g + :one', ExpressionAttributeNames=names,
                ExpressionAttributeValues=values, ReturnValues='ALL_NEW')
            engine._ack(response)
            if RecoverySnapshot.decode(response.get('Attributes'), self.binding) != after or self.read() != after:
                return engine.WriteObservation(False, 'effect_ack_or_readback_unverified', True), None
        except Exception:
            return engine.WriteObservation(False, 'effect_write_uncertain_or_conflicting', True), None
        return engine.WriteObservation(True, 'synthetic_effect_transition_confirmed'), after


class _OneShotContinuation:
    """Ephemeral continuation after a confirmed durable latch, never read-based.

    Local consumption only prevents reusing this same continuation. The durable
    CAS, not this lock, prevents another process creating another continuation.
    It is not a worker capability, serialization format or production boundary.
    """

    def __init__(self, action):
        self._action, self._spent, self._lock = action, False, Lock()

    def __copy__(self):
        raise TypeError('A live continuation cannot be copied')

    def __deepcopy__(self, memo):
        raise TypeError('A live continuation cannot be copied')

    def __reduce_ex__(self, protocol):
        raise TypeError('A live continuation cannot be serialized or restored')

    def run(self):
        with self._lock:
            if self._spent:
                return False
            self._spent = True
        self._action()
        return True


@dataclass(frozen=True)
class SyntheticProviderReply:
    status_code: int
    observation_digest: str


@dataclass(frozen=True)
class RecoveryResult:
    accepted: bool
    reason: str
    requires_reconciliation: bool = False
    claim_confirmed: bool = False
    attempt_commit_confirmed: bool = False
    simulated_dispatch_authorized: bool = False
    simulated_provider_entered: bool = False
    outcome_independently_verified: bool = False
    revision: str | None = None
    control: dict | None = None

    @property
    def execution_allowed(self):
        return False

    @property
    def external_retry_allowed(self):
        return False


class OfflineEffectRecoveryGateway:
    """No endpoint, SDK or live provider. Trusted fixture construction only.

    Hooks are privileged fault-injection instrumentation outside request data.
    Every provider and reconciliation authority is an injected synthetic double.
    The public result NEVER returns the continuation or grants real execution.
    """

    def __init__(self, *, transport, binding, authentication=None, clock=None,
                 providers=None, reconciliation=None, hooks=None):
        self._raw, self._binding = transport, binding
        self._auth, self._clock = authentication, clock
        self._providers = {} if providers is None else dict(providers)
        self._reconciliation, self._hooks = reconciliation, {} if hooks is None else dict(hooks)
        self._identity = commands.OfflineCommandGateway(
            transport=transport, binding=binding, authentication=authentication, clock=clock)
        self._store = _EffectStore(transport, binding)

    def _stage(self, name, **data):
        if name in self._hooks:
            self._hooks[name](data)

    def _worker(self, invocation, snapshot, actor):
        owner, now = self._identity._worker(invocation, snapshot.base, actor)
        claim = snapshot.base.core.state.worker_claim
        if claim is None or owner != claim.owner:
            raise engine.StoreUnavailable('Confirmed owner required')
        return owner, now

    @staticmethod
    def _request(name, payload):
        fields = {'read_control': set(), 'claim_reserved_intent': {'intent_id','approval_epoch','expected_revision'},
                  'emergency_stop': {'expected_revision'},
                  'simulate_external_attempt': {'intent_id','approval_epoch','operation_id','expected_revision'},
                  'record_independent_outcome': {'operation_id','expected_revision'}}
        if type(name) is not str or name not in fields or type(payload) is not dict or set(payload) != fields[name]:
            return False
        if name != 'read_control' and (type(payload['expected_revision']) is not str
                                      or not 1 <= len(payload['expected_revision']) <= 4096):
            return False
        if 'operation_id' in payload and not _hex(payload['operation_id'], 64):
            return False
        if 'intent_id' in payload and (not _hex(payload['intent_id'],64)
                or type(payload['approval_epoch']) is not int or not 1 <= payload['approval_epoch'] < 100000000):
            return False
        return True

    def handle(self, name, payload, *, invocation=None):
        if not self._request(name,payload):
            return RecoveryResult(False,'request_schema_rejected')
        try:
            kinds = ({'operator'} if name=='emergency_stop' else {'controller'}
                     if name=='record_independent_outcome' else {'worker','operator','controller'})
            actor = self._identity._actor('operator' if name=='emergency_stop' else 'reader', invocation, kinds)
            before = self._store.read()
            if name=='read_control':
                return RecoveryResult(True,'synthetic_control_observed',revision=before.revision,control=before.view())
            if before.revision != payload['expected_revision']:
                return RecoveryResult(False,'revision_conflict_reconcile',True)
            if 'operation_id' in payload and payload['operation_id'] != before.effect.operation_id:
                return RecoveryResult(False,'fixed_operation_mismatch')
            if name in {'claim_reserved_intent','emergency_stop'}:
                projection = _RecoveryProjection(self._raw,self._binding,before.effect)
                gateway = commands.OfflineCommandGateway(transport=projection,binding=self._binding,
                    authentication=self._auth,clock=self._clock)
                result = gateway.handle(name,{**payload,'expected_revision':before.base.revision},invocation=invocation)
                return RecoveryResult(result.accepted,result.reason,result.requires_reconciliation,
                                      claim_confirmed=name=='claim_reserved_intent' and result.accepted)
            if name=='record_independent_outcome':
                return self._reconcile(before,invocation,actor)
            if (actor.kind!='worker' or payload['intent_id']!=before.effect.definition.intent_id
                    or payload['approval_epoch']!=before.effect.definition.approval_epoch
                    or not before.base.core.state.control.enabled):
                return RecoveryResult(False,'owner_intent_epoch_or_activation_rejected')
            if before.effect.phase!=NOT_STARTED:
                return RecoveryResult(False,'durable_attempt_already_spent_reconcile',True)
            _, now = self._worker(invocation,before,actor)
            provider = self._providers.get(before.effect.definition.provider_id)
            if not callable(getattr(provider,'send_once',None)):
                return RecoveryResult(False,'synthetic_provider_unavailable')
            attempt = AttemptRecord(_attempt_id(before),before.base.core.generation+1,_iso(now),
                                    _digest(asdict(before.base.core.state.worker_claim.owner)))
            effect = replace(before.effect,phase=ATTEMPT_COMMITTED,attempt=attempt)
            result, committed = self._store.advance(before,effect,
                check=lambda:self._worker(invocation,before,actor))
            if not result.confirmed:
                return RecoveryResult(False,result.reason,True)
        except Exception:
            return RecoveryResult(False,'authority_identity_or_state_unavailable',True)

        authorized, entered, observation = False, False, None
        def dispatch():
            nonlocal authorized,entered,observation
            self._worker(invocation,committed,actor)
            self._stage('before_final_read',snapshot=committed)
            if self._store.read()!=committed or not committed.base.core.state.control.enabled:
                raise engine.StoreUnavailable('Control changed before simulated send')
            self._stage('after_final_read',snapshot=committed)
            authorized = True
            self._stage('send_authorized',snapshot=committed)
            entered = True
            reply = provider.send_once(committed.effect.definition,committed.effect.operation_id,
                                       committed.effect.attempt.attempt_id)
            self._stage('after_provider',snapshot=committed)
            observation = (reply.observation_digest if type(reply) is SyntheticProviderReply
                           and _hex(reply.observation_digest,64) else _digest('unverified_provider_response'))
        continuation = _OneShotContinuation(dispatch)  # ONLY this confirmed write creates it.
        self._stage('gate_confirmed',snapshot=committed,continuation=continuation)
        try:
            continuation.run()
        except Exception as error:
            observation = _digest(type(error).__name__)  # No exception message, payload or token.
        self._stage('before_outcome_write',snapshot=committed)
        unknown = replace(committed.effect,phase=OUTCOME_UNKNOWN,observation_digest=observation)
        self._store.advance(committed,unknown,check=lambda:None)  # One advisory write, NEVER retry.
        return RecoveryResult(authorized,'attempt_spent_outcome_requires_independent_reconciliation',True,
            attempt_commit_confirmed=True,simulated_dispatch_authorized=authorized,simulated_provider_entered=entered)

    def _reconcile(self,before,invocation,actor):
        if before.effect.phase not in {ATTEMPT_COMMITTED,OUTCOME_UNKNOWN}:
            return RecoveryResult(False,'no_unverified_spent_attempt')
        try:
            verify = getattr(self._reconciliation,'verify',None)
            if not callable(verify):
                raise engine.StoreUnavailable('Independent outcome authority unavailable')
            now = self._identity._now()
            evidence = verify(invocation,before,now=now)
            if type(evidence) is not IndependentEffectEvidence or _instant(evidence.checked_at)>now:
                raise engine.StoreUnavailable('Independent evidence not verified')
            proposed = replace(before.effect,phase=OUTCOME_VERIFIED,evidence=evidence)
            # Construction validates operation/attempt/request/budget binding.
            RecoverySnapshot(before.base,proposed)
            def check():
                if self._identity._actor('reader',invocation,{'controller'})!=actor:
                    raise engine.StoreUnavailable('Reconciliation principal changed')
            result,_ = self._store.advance(before,proposed,check=check)
        except Exception:
            return RecoveryResult(False,'independent_outcome_unavailable',True)
        return RecoveryResult(result.confirmed,result.reason,not result.confirmed,
                              outcome_independently_verified=result.confirmed)


PRODUCTION_EFFECT_RECOVERY = None

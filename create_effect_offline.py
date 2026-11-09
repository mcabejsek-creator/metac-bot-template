"""Explicit synthetic create-effect and composition; NO production authority.

One physical item, no new ledger, server, endpoint, SDK or live provider. The
exact admission/recovery schema pair plus ten-field shape is a new envelope;
neither old eight-field decoder silently upgrades it. Trusted fixture setup
and Python objects are not process isolation or real artifact attestation.
"""

from dataclasses import asdict, dataclass, replace
import json

import command_gateway_offline as commands
import durable_effect_offline as recovery
import dynamodb_cas_offline as engine
import reserve_bind_offline as admission
from dispatch_intent_controller import _instant
from worker_claim_offline import _hex
from worker_identity import ActionsOIDCIdentity


ADMISSION_FIELDS = frozenset(('pk', 'incarnation', 'generation', 'document',
    'command_schema', 'run_authorization', 'admission_schema', 'reservation'))
RECOVERY_FIELDS = (ADMISSION_FIELDS - {'admission_schema', 'reservation'}) | {'recovery_schema', 'effect'}
CREATED_FIELDS = ADMISSION_FIELDS | {'recovery_schema', 'effect'}
MAX_ITEM_BYTES = 32768  # Local prototype bound, not a DynamoDB service limit.


def fixed_definition(snapshot):
    """Controller-owned definition of ONE fake operation, not a worker catalog."""
    record, auth = snapshot.reservation, snapshot.base.authorization
    if (record is None or auth is None or type(record.plan) is not admission.SyntheticAtomicityPlan
            or record.plan != admission.SyntheticAtomicityPlan()):
        raise engine.StoreUnavailable('Bound explicit synthetic plan required')
    return recovery.EffectDefinition(record.intent_id, record.approval_epoch,
        auth.authorization_id, auth.policy.run_id, record.scope.configuration_digest,
        'atomicity.one', 'paid_model', 'synthetic://create-effect-provider',
        'SYNTHETIC:one-fixed-operation-v1', record.run_reserve_usd, '0.10')


@dataclass(frozen=True)
class SyntheticFixedEffectApproval:
    """Independent test authority OUTPUT; never accepted in a command payload.

    The injected service authenticates ingress, signature-checks the registered
    run JWT and verifies the pre-approved synthetic artifact/config facts. A
    dataclass, name or boolean alone confers no trust or execution permission.
    """

    expected_revision: str
    evidence_id: str
    authorization_id: str
    trust_generation: int
    configuration_digest: str
    artifact_digest: str
    identity: ActionsOIDCIdentity
    definition: recovery.EffectDefinition


@dataclass(frozen=True)
class CreatedEffectSnapshot:
    admission: admission.AdmissionSnapshot
    effect: recovery.EffectRecord

    def __post_init__(self):
        if type(self.admission) is not admission.AdmissionSnapshot:
            raise engine.StoreUnavailable('Whole admission envelope required')
        recovery.RecoverySnapshot(self.admission.base, self.effect)
        if self.effect.definition != fixed_definition(self.admission):
            raise engine.StoreUnavailable('Effect differs from fixed reservation/config/run scope')

    @property
    def kernel(self):
        return recovery.RecoverySnapshot(self.admission.base, self.effect)

    @property
    def revision(self):
        return 'created-effect-offline/v1:' + json.dumps([
            self.admission.revision, recovery._digest(asdict(self.effect))], separators=(',', ':'))

    def item(self):
        result = {**self.admission.item(), 'recovery_schema': engine._av(recovery.RECOVERY_SCHEMA),
                  'effect': engine._av(asdict(self.effect))}
        if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_ITEM_BYTES:
            raise engine.StoreUnavailable('Created envelope size exceeded')
        return result

    @classmethod
    def decode(cls, item, binding):
        if (type(item) is not dict or set(item) != CREATED_FIELDS
                or len(json.dumps(item, ensure_ascii=False).encode()) > MAX_ITEM_BYTES):
            raise engine.StoreUnavailable('Exact explicit ten-field envelope required')
        first = admission.AdmissionSnapshot.decode({name:item[name] for name in ADMISSION_FIELDS}, binding)
        second = recovery.RecoverySnapshot.decode({name:item[name] for name in RECOVERY_FIELDS}, binding)
        if first.base != second.base:
            raise engine.StoreUnavailable('Projections disagree on the same authority')
        result = cls(first, second.effect)
        result.item()
        return result

    def view(self):
        return {**self.admission.view(), 'recovery_schema': recovery.RECOVERY_SCHEMA,
                'effect': asdict(self.effect)}


class _CreatedRecoveryProjection:
    """Ten -> existing recovery eight; preserve admission in every atomic CAS."""

    def __init__(self, raw, binding, record):
        self.raw, self.binding, self.record = raw, binding, record

    def _project(self, item):
        snapshot = CreatedEffectSnapshot.decode(item, self.binding)
        if snapshot.admission.reservation != self.record:
            raise engine.StoreUnavailable('Immutable admission evidence changed')
        return snapshot.kernel.item()

    def get_item(self, **request):
        response = self.raw.get_item(**request)
        engine._ack(response)
        return {**response, 'Item':self._project(response.get('Item'))}

    def update_item(self, **request):
        request = engine._pin_envelope(request, {
            'admission_schema':engine._av(admission.ADMISSION_SCHEMA),
            'reservation':engine._av(asdict(self.record))})
        response = self.raw.update_item(**request)
        engine._ack(response)
        return {**response, 'Attributes':self._project(response.get('Attributes'))}


class OfflineCreatedEffectGateway:
    """Trusted synthetic harness only; reuses the old kernels and authority.

    providers/reconciliation/hooks are privileged test doubles, never request
    fields. No production factory, runtime import hook or real send exists.
    """

    def __init__(self, *, transport, binding, authentication=None, clock=None,
                 providers=None, reconciliation=None, hooks=None):
        self._raw, self._binding, self._auth, self._clock = transport, binding, authentication, clock
        self._providers, self._reconciliation, self._hooks = providers, reconciliation, hooks
        self._identity = commands.OfflineCommandGateway(
            transport=transport, binding=binding, authentication=authentication, clock=clock)

    def _read(self):
        response = self._raw.get_item(TableName=self._binding.table_name,
            Key={'pk':engine._av(self._binding.partition_key)}, ConsistentRead=True)
        engine._ack(response)
        item = response.get('Item')
        if type(item) is dict and set(item) == ADMISSION_FIELDS:
            return admission.AdmissionSnapshot.decode(item, self._binding)
        return CreatedEffectSnapshot.decode(item, self._binding)

    @staticmethod
    def _request(name, payload):
        fields = {'read_control':set(), 'reserve_intent':{'approval_epoch','expected_revision'},
            'bind_approved_run':{'intent_id','approval_epoch','expected_revision'},
            'create_fixed_effect':{'intent_id','approval_epoch','expected_revision'},
            'claim_reserved_intent':{'intent_id','approval_epoch','expected_revision'},
            'emergency_stop':{'expected_revision'},
            'simulate_external_attempt':{'intent_id','approval_epoch','operation_id','expected_revision'},
            'record_independent_outcome':{'operation_id','expected_revision'}}
        if type(name) is not str or name not in fields or type(payload) is not dict or set(payload) != fields[name]:
            return False
        if name != 'read_control' and (type(payload['expected_revision']) is not str
                or not 1 <= len(payload['expected_revision']) <= 4096):
            return False
        if 'approval_epoch' in payload and (type(payload['approval_epoch']) is not int
                or not 1 <= payload['approval_epoch'] < 100000000):
            return False
        return all(_hex(payload[field],64) for field in ('intent_id','operation_id') if field in payload)

    def _prepare(self, before, invocation, actor):
        if self._identity._actor('reader', invocation, {'controller'}) != actor:
            raise engine.StoreUnavailable('Controller principal changed')
        record, auth = before.reservation, before.base.authorization
        control = before.base.core.state.control
        now = self._identity._now()
        if (record is None or auth is None or not control.enabled
                or before.base.core.state.worker_claim is not None
                or not _instant(record.reserved_at) <= now < _instant(record.valid_until)
                or not _instant(auth.approved_at) <= now < _instant(auth.valid_until)):
            raise engine.StoreUnavailable('Fresh bound unowned reservation required')
        verify = getattr(self._auth, 'fixed_effect', None)
        if not callable(verify):
            raise engine.StoreUnavailable('Independent controller effect approval unavailable')
        proof = verify(invocation, before, now=now)
        definition = fixed_definition(before)
        if (type(proof) is not SyntheticFixedEffectApproval or proof.expected_revision != before.revision
                or type(proof.trust_generation) is not int
                or (proof.evidence_id, proof.authorization_id, proof.trust_generation,
                    proof.configuration_digest, proof.artifact_digest)
                != (record.evidence_id, auth.authorization_id, auth.trust_generation,
                    record.scope.configuration_digest, record.scope.artifact_digest)
                or proof.definition != definition or type(proof.identity) is not ActionsOIDCIdentity
                or type(proof.identity.expires_at) is not int or proof.identity.expires_at <= now.timestamp()
                or any(getattr(proof.identity,name) != value for name,value in
                       asdict(commands._policy_owner(auth.policy)).items())):
            raise engine.StoreUnavailable('Approved run/artifact/config/definition evidence mismatch')
        effect = recovery.EffectRecord(recovery.operation_id(before.base,definition), definition)
        next_base = replace(before.base,core=replace(before.base.core,generation=before.base.core.generation+1))
        return CreatedEffectSnapshot(replace(before,base=next_base),effect)

    def _create(self, before, invocation, actor):
        if before.base.core.generation >= engine.MAX_GENERATION-1:
            return recovery.RecoveryResult(False,'generation_headroom_exhausted')
        try:
            after = self._prepare(before,invocation,actor)
            old, new = before.item(), after.item()
            names = {f'#f{i}':field for i,field in enumerate(sorted(old))}
            aliases = {field:alias for alias,field in names.items()}
            values = {f':f{i}':old[field] for i,field in enumerate(sorted(old))}
            condition = 'attribute_exists('+aliases['pk']+') AND '+' AND '.join(
                f'{alias} = :f{i}' for i,alias in enumerate(names))
            names.update({'#on':'enabled','#owner':'worker_claim','#pending':'pending',
                          '#intent':'intent_id','#epoch':'approval_epoch','#rs':'recovery_schema','#effect':'effect'})
            values.update({':on':engine._av(True),':null':engine._av('NULL'),
                ':intent':engine._av(before.reservation.intent_id),':epoch':engine._av(before.reservation.approval_epoch),
                ':limit':engine._av(engine.MAX_GENERATION-1),':one':engine._av(1),
                ':rs':new['recovery_schema'],':effect':new['effect']})
            document, generation = aliases['document'], aliases['generation']
            condition += (f' AND {generation} < :limit AND {document}.#on = :on'
                f' AND attribute_type({document}.#owner, :null) AND {document}.#pending.#intent = :intent'
                f' AND {document}.#epoch = :epoch')
            request = dict(TableName=self._binding.table_name,Key={'pk':engine._av(self._binding.partition_key)},
                ConditionExpression=condition,UpdateExpression=f'SET #rs = :rs, #effect = :effect, {generation} = {generation} + :one',
                ExpressionAttributeNames=names,ExpressionAttributeValues=values,ReturnValues='ALL_NEW')
            request = engine._fence_envelope(request,old)
            if self._prepare(before,invocation,actor) != after:
                raise engine.StoreUnavailable('Authority changed before the only send')
        except Exception:
            return recovery.RecoveryResult(False,'create_effect_precondition_unavailable',True)
        try:
            response = self._raw.update_item(**request)
            engine._ack(response)
            if CreatedEffectSnapshot.decode(response.get('Attributes'),self._binding) != after or self._read() != after:
                return recovery.RecoveryResult(False,'create_effect_ack_or_readback_unverified',True)
        except Exception:
            return recovery.RecoveryResult(False,'create_effect_write_uncertain_or_conflicting',True)
        return recovery.RecoveryResult(True,'synthetic_effect_created_execution_denied',revision=after.revision)

    def handle(self, name, payload, *, invocation=None):
        if not self._request(name,payload):
            return recovery.RecoveryResult(False,'request_schema_rejected')
        try:
            kinds = ({'operator'} if name=='emergency_stop' else {'controller'}
                if name in {'reserve_intent','bind_approved_run','create_fixed_effect','record_independent_outcome'}
                else {'worker'} if name in {'claim_reserved_intent','simulate_external_attempt'}
                else {'worker','controller','operator'})
            actor = self._identity._actor('operator' if name=='emergency_stop' else 'reader',invocation,kinds)
            before = self._read()
            if name=='read_control':
                return recovery.RecoveryResult(True,'synthetic_control_observed_execution_denied',
                    revision=before.revision,control=before.view())
            if before.revision != payload['expected_revision']:
                return recovery.RecoveryResult(False,'revision_conflict_reconcile',True)
            if name=='create_fixed_effect':
                if type(before) is not admission.AdmissionSnapshot:
                    return recovery.RecoveryResult(False,'effect_slot_already_exists_reconcile',True)
                if (before.reservation is None or payload['intent_id'] != before.reservation.intent_id
                        or payload['approval_epoch'] != before.reservation.approval_epoch):
                    return recovery.RecoveryResult(False,'reserved_intent_or_epoch_mismatch')
                return self._create(before,invocation,actor)
            if type(before) is admission.AdmissionSnapshot:
                if name not in {'reserve_intent','bind_approved_run','claim_reserved_intent','emergency_stop'}:
                    return recovery.RecoveryResult(False,'effect_not_created')
                gateway = admission.OfflineAdmissionGateway(transport=self._raw,binding=self._binding,
                    authentication=self._auth,clock=self._clock)
                result = gateway.handle(name,payload,invocation=invocation)
                return recovery.RecoveryResult(result.accepted,result.reason,result.requires_reconciliation,
                    claim_confirmed=name=='claim_reserved_intent' and result.accepted,
                    revision=result.revision,control=result.control)
            if name in {'reserve_intent','bind_approved_run'}:
                return recovery.RecoveryResult(False,'immutable_reservation_and_binding_reconcile',True)
            projection = _CreatedRecoveryProjection(self._raw,self._binding,before.admission.reservation)
            gateway = recovery.OfflineEffectRecoveryGateway(transport=projection,binding=self._binding,
                authentication=self._auth,clock=self._clock,providers=self._providers,
                reconciliation=self._reconciliation,hooks=self._hooks)
            return gateway.handle(name,{**payload,'expected_revision':before.kernel.revision},invocation=invocation)
        except Exception:
            return recovery.RecoveryResult(False,'created_effect_authority_or_state_unavailable',True)

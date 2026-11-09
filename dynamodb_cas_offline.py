"""Offline, server-internal DynamoDB request prototype; NOT a live V3 store.

An injected synthetic transport is mandatory. No SDK, credentials, client
factory, endpoint, provisioning, identity grant or production binding exists.
Workers must NOT receive raw UpdateItem permission: IAM key/attribute limits
do not enforce these expressions. This engine belongs behind a future trusted
command boundary; its Python checks do not prove that boundary is deployed.
"""

from copy import deepcopy
from dataclasses import dataclass
import json
import re

from dispatch_intent_controller import disable_controller
from worker_claim_offline import OfflineControlStateV3


MAX_GENERATION = 2**63 - 1
MAX_WIRE_ITEM_BYTES = 4096  # Conservative bound; not DynamoDB's 400 KB limit.
_ENVELOPE_FIELDS = frozenset(('command_schema', 'run_authorization', 'admission_schema',
                              'reservation', 'recovery_schema', 'effect'))


class StoreUnavailable(RuntimeError):
    """No authoritative observation; never initialize or fall back to a cache."""


def _fence_envelope(request, present):
    """Fence known newer schemas at the atomic write, not only at ACK decoding.

    Server-internal request shaping. Unknown arbitrary top-level attributes
    still require trusted writers/strict schema and fail-closed ACK validation.
    """
    result = deepcopy(request)
    for field in sorted(_ENVELOPE_FIELDS - set(present)):
        alias = '#shape_' + field
        result['ExpressionAttributeNames'][alias] = field
        result['ConditionExpression'] += ' AND attribute_not_exists(' + alias + ')'
    return result


def _pin_envelope(request, fields):
    """A projection replaces absence ONLY with exact whole-value equality.

    This is not a generic client endpoint or a way to disable schema checks.
    Every consumed fence must exist and every field receives an atomic pin.
    """
    if not fields or set(fields) - _ENVELOPE_FIELDS:
        raise StoreUnavailable('Known projected envelope fields required')
    result = deepcopy(request)
    clauses = result['ConditionExpression'].split(' AND ')
    for field, value in fields.items():
        alias, parameter = '#shape_' + field, ':shape_' + field
        clause = 'attribute_not_exists(' + alias + ')'
        if clause not in clauses or result['ExpressionAttributeNames'].get(alias) != field:
            raise StoreUnavailable('Missing atomic schema fence')
        clauses.remove(clause)
        result['ExpressionAttributeValues'][parameter] = deepcopy(value)
        clauses.append(alias + ' = ' + parameter)
    result['ConditionExpression'] = ' AND '.join(clauses)
    return result


def _av(value):
    if value is None:
        return {'NULL': True}
    if type(value) is bool:
        return {'BOOL': value}
    if type(value) is int:
        return {'N': str(value)}
    if type(value) is str:
        return {'S': value}
    if type(value) is dict and all(type(key) is str for key in value):
        return {'M': {key: _av(item) for key, item in value.items()}}
    raise StoreUnavailable('Unsupported control value')


def _value(value, depth=0):
    if type(value) is not dict or len(value) != 1 or depth > 8:
        raise StoreUnavailable('Invalid DynamoDB value')
    kind, raw = next(iter(value.items()))
    if kind == 'NULL' and raw is True:
        return None
    if kind == 'BOOL' and type(raw) is bool:
        return raw
    if kind == 'S' and type(raw) is str:
        return raw
    if kind == 'N' and type(raw) is str and re.fullmatch(r'0|[1-9][0-9]{0,18}', raw):
        return int(raw)
    if kind == 'M' and type(raw) is dict and all(type(key) is str for key in raw):
        return {key: _value(item, depth + 1) for key, item in raw.items()}
    raise StoreUnavailable('Invalid DynamoDB control type')


@dataclass(frozen=True)
class AuthorityBinding:
    """Controller deployment configuration, NEVER derived from a restored item.

    Incarnation is an external deployment fence, not another claim ledger.
    A real restore/cutover requires OFF and independent reconciliation first.
    """

    table_name: str
    partition_key: str
    incarnation: str
    authority_id: str

    def __post_init__(self):
        if (type(self.table_name) is not str or not self.table_name.startswith('offline-')
                or re.fullmatch(r'[A-Za-z0-9_.-]{3,100}', self.table_name) is None
                or type(self.partition_key) is not str or not 1 <= len(self.partition_key) <= 100
                or type(self.authority_id) is not str or not self.authority_id.startswith('offline://')
                or not 10 <= len(self.authority_id) <= 200
                or type(self.incarnation) is not str
                or re.fullmatch(r'[0-9a-f]{32}', self.incarnation) is None):
            raise StoreUnavailable('Explicit synthetic authority binding required')


@dataclass(frozen=True)
class Snapshot:
    state: OfflineControlStateV3
    generation: int
    binding: AuthorityBinding

    def __post_init__(self):
        if type(self.state) is not OfflineControlStateV3 or type(self.binding) is not AuthorityBinding:
            raise StoreUnavailable('Validated whole control state required')
        if type(self.generation) is not int or not 1 <= self.generation <= MAX_GENERATION:
            raise StoreUnavailable('Invalid generation')
        if self.generation == MAX_GENERATION and self.state.control.enabled:
            raise StoreUnavailable('Exhausted active authority is invalid')
        OfflineControlStateV3.decode(self.state.encode())

    @property
    def revision(self):
        # Opaque to callers. The server always compares an authoritative read;
        # it does not interpret a caller's revision as proof of any identity.
        return 'dynamodb-offline/v1:' + json.dumps([
            self.binding.authority_id, self.binding.table_name, self.binding.partition_key,
            self.binding.incarnation, self.generation], separators=(',', ':'))

    def item(self):
        result = {
            'pk': _av(self.binding.partition_key), 'incarnation': _av(self.binding.incarnation),
            'generation': _av(self.generation), 'document': _av(json.loads(self.state.encode())),
        }
        if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > MAX_WIRE_ITEM_BYTES:
            raise StoreUnavailable('Prototype item size bound exceeded')
        return result


def _snapshot(item, binding):
    if type(item) is not dict or set(item) != {'pk', 'incarnation', 'generation', 'document'}:
        raise StoreUnavailable('Complete existing control item required')
    if len(json.dumps(item, ensure_ascii=False).encode('utf-8')) > MAX_WIRE_ITEM_BYTES:
        raise StoreUnavailable('Prototype item size bound exceeded')
    data = {key: _value(value) for key, value in item.items()}
    if data['pk'] != binding.partition_key or data['incarnation'] != binding.incarnation:
        raise StoreUnavailable('Authority mismatch; no automatic restore/cutover')
    state = OfflineControlStateV3.decode(json.dumps(data['document']))
    return Snapshot(state, data['generation'], binding)


def _ack(response):
    if type(response) is not dict:
        raise StoreUnavailable('Unverified response')
    metadata = response.get('ResponseMetadata')
    if (type(metadata) is not dict or type(metadata.get('HTTPStatusCode')) is not int
            or metadata['HTTPStatusCode'] != 200 or type(metadata.get('RetryAttempts')) is not int
            or metadata['RetryAttempts'] != 0):
        raise StoreUnavailable('Response or transport attempts unverified')


def sdk_config_requirements():
    """Future botocore.Config inputs, NOT an SDK factory or verified runtime.

    total_max_attempts=1 includes the initial request; max_attempts in a Config
    counts retries instead. The actual SDK/transport still needs offline testing
    in a separately pinned environment before any production implementation.
    """
    return dict(retries={'mode': 'standard', 'total_max_attempts': 1},
                connect_timeout=2, read_timeout=5)


@dataclass(frozen=True)
class WriteObservation:
    confirmed: bool
    reason: str
    requires_reconciliation: bool = False

    @property
    def execution_allowed(self):
        return False


class DynamoConditionalStorePrototype:
    """Storage engine inside a FUTURE trusted command service, not a worker API.

    read() returns a Snapshot, not the existing V3 store protocol tuple. This
    prototype is deliberately not connected to CONTROLLER_STORE. Identity and
    authorization are not established by a state object or this constructor.
    """

    def __init__(self, *, transport, binding):
        if (transport is None or type(binding) is not AuthorityBinding
                or not callable(getattr(transport, 'get_item', None))
                or not callable(getattr(transport, 'update_item', None))):
            raise StoreUnavailable('Injected synthetic transport and binding required')
        self.transport, self.binding = transport, binding

    def read(self):
        try:
            response = self.transport.get_item(
                TableName=self.binding.table_name, Key={'pk': _av(self.binding.partition_key)},
                ConsistentRead=True,
            )
            _ack(response)
            return _snapshot(response.get('Item'), self.binding)
        except Exception:
            raise StoreUnavailable('Authoritative strong read unavailable') from None

    def compare_and_swap(self, expected_revision, proposed):
        """One UpdateItem at most, no retry, lease, release, takeover or reset.

        Only owner assignment or OFF can be represented. Arbitrary proposed
        documents are not an acceptable external command-service interface.
        """
        if type(expected_revision) is not str or type(proposed) is not OfflineControlStateV3:
            return WriteObservation(False, 'invalid_request')
        try:
            before = self.read()
            if before.revision != expected_revision:
                return WriteObservation(False, 'revision_conflict_reconcile', True)
            OfflineControlStateV3.decode(proposed.encode())
            old = before.state
            is_claim = (old.control.enabled is True and old.worker_claim is None
                        and proposed.worker_claim is not None and proposed.control == old.control)
            is_stop = (old.control.enabled is True and proposed.worker_claim == old.worker_claim
                       and proposed.control == disable_controller(old.control))
            if not is_claim and not is_stop:
                return WriteObservation(False, 'transition_rejected')
            limit = MAX_GENERATION - 1 if is_claim else MAX_GENERATION
            if before.generation >= limit:
                return WriteObservation(False, 'generation_headroom_exhausted')
            after = Snapshot(proposed, before.generation + 1, self.binding)
            after.item()  # Validate the entire resulting item and size before writing.
            names = {'#pk': 'pk', '#i': 'incarnation', '#g': 'generation', '#d': 'document',
                     '#v': 'version', '#on': 'enabled', '#e': 'approval_epoch'}
            values = {':pk': _av(self.binding.partition_key), ':i': _av(self.binding.incarnation), ':g': _av(before.generation),
                      ':limit': _av(limit), ':one': _av(1), ':v': _av(3), ':on': _av(True),
                      ':e': _av(old.control.approval_epoch), ':before': _av(json.loads(old.encode()))}
            condition = ('attribute_exists(#pk) AND #pk = :pk AND #i = :i AND #g = :g AND #g < :limit '
                         'AND #d.#v = :v AND #d.#on = :on AND #d.#e = :e AND #d = :before')
            if is_claim:
                names.update({'#pending': 'pending', '#intent': 'intent_id', '#claim': 'worker_claim'})
                values.update({':intent': _av(old.control.pending.intent_id), ':null': _av('NULL'),
                               ':claim': _av(json.loads(proposed.encode())['worker_claim'])})
                condition += ' AND #d.#pending.#intent = :intent AND attribute_type(#d.#claim, :null)'
                update = 'SET #d.#claim = :claim, #g = #g + :one'
            else:
                values[':off'] = _av(False)
                update = 'SET #d.#on = :off, #g = #g + :one'
            request = dict(TableName=self.binding.table_name,
                           Key={'pk': _av(self.binding.partition_key)}, ConditionExpression=condition,
                           UpdateExpression=update, ExpressionAttributeNames=names,
                           ExpressionAttributeValues=values, ReturnValues='ALL_NEW')
            request = _fence_envelope(request, {'pk', 'incarnation', 'generation', 'document'})
        except Exception:
            return WriteObservation(False, 'authoritative_state_unavailable', True)
        try:
            response = self.transport.update_item(**request)  # The ONLY write call.
        except Exception as error:
            details = getattr(error, 'response', None)
            payload = details.get('Error') if type(details) is dict else None
            if type(payload) is dict and payload.get('Code') == 'ConditionalCheckFailedException':
                return WriteObservation(False, 'conditional_conflict_reconcile', True)
            return WriteObservation(False, 'write_outcome_uncertain_reconcile', True)
        try:
            _ack(response)
            if _snapshot(response.get('Attributes'), self.binding) != after:
                return WriteObservation(False, 'write_ack_unverified_reconcile', True)
            if self.read() != after:
                return WriteObservation(False, 'readback_changed_reconcile', True)
        except Exception:
            return WriteObservation(False, 'write_readback_unverified_reconcile', True)
        return WriteObservation(True, 'synthetic_transition_confirmed_execution_denied')

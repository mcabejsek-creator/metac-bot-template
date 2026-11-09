"""Isolated command-boundary model. NO server, credentials, AWS or launch grant.

Only a trusted test harness constructs the gateway and its injected authority.
Invocation contexts are authenticated outside command data. Python objects and
private attributes are not an isolation boundary against code in this process.
The existing V3 schema, CAS engine and all production NONE anchors stay intact.
"""

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json

import dynamodb_cas_offline as engine
from dispatch_intent_controller import _instant, _iso, disable_controller
from scheduler_policy import STATUS_MAX_AGE
from worker_claim_offline import ClaimOwner, WorkerClaim, _clock, _hex
from worker_identity import ActionsOIDCIdentity, ControllerOIDCPolicy


COMMAND_SCHEMA = 'offline-command-v1'  # Not a production V3 version or migration.
_CORE_FIELDS = frozenset(('pk', 'incarnation', 'generation', 'document'))
_DRAFT_FIELDS = _CORE_FIELDS | {'command_schema', 'run_authorization'}


@dataclass(frozen=True)
class RunAuthorizationDraft:
    """Seeded by an independent synthetic controller, in the SAME control item.

    This describes the future bind-run command's output; no command in this
    prototype can create, replace or revoke it. Expiry is NOT an owner lease.
    """

    authorization_id: str
    intent_id: str
    approval_epoch: int
    approved_at: str
    valid_until: str
    trust_generation: int
    policy: ControllerOIDCPolicy

    def __post_init__(self):
        if (not _hex(self.authorization_id, 32) or not _hex(self.intent_id, 64)
                or type(self.approval_epoch) is not int or not 1 <= self.approval_epoch < 100000000
                or type(self.trust_generation) is not int or self.trust_generation < 1
                or type(self.policy) is not ControllerOIDCPolicy):
            raise engine.StoreUnavailable('Invalid draft run authorization')
        if not 0 < (_instant(self.valid_until) - _instant(self.approved_at)).total_seconds() <= 600:
            raise engine.StoreUnavailable('Invalid draft authorization window')

    @classmethod
    def decode(cls, value):
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise engine.StoreUnavailable('Complete draft authorization required')
        policy = value['policy']
        if type(policy) is not dict or set(policy) != set(ControllerOIDCPolicy.__dataclass_fields__):
            raise engine.StoreUnavailable('Complete independent policy required')
        return cls(**{**value, 'policy': ControllerOIDCPolicy(**policy)})


def server_request_id(binding, authorization):
    """Deterministic correlation, never client input, credential or retry grant."""
    data = ['vrchcab-command-correlation-v1', asdict(binding), asdict(authorization)]
    return sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:32]


def _policy_owner(policy):
    return ClaimOwner(policy.repository_id, policy.repository_owner_id, policy.source_sha,
                      policy.workflow_ref, policy.workflow_sha, policy.run_id, '1')


@dataclass(frozen=True)
class CommandSnapshotDraft:
    """One authority item including run binding; no auxiliary claim ledger."""

    core: engine.Snapshot
    authorization: RunAuthorizationDraft | None

    def __post_init__(self):
        if type(self.core) is not engine.Snapshot:
            raise engine.StoreUnavailable('Complete control snapshot required')
        authorization = self.authorization
        control, claim = self.core.state.control, self.core.state.worker_claim
        if authorization is not None:
            if (type(authorization) is not RunAuthorizationDraft or control.pending is None
                    or authorization.intent_id != control.pending.intent_id
                    or authorization.approval_epoch != control.approval_epoch
                    or _instant(authorization.approved_at) < _instant(control.pending.reserved_at)):
                raise engine.StoreUnavailable('Authorization not bound to reserved intent')
        if claim is not None:
            if (authorization is None or claim.owner != _policy_owner(authorization.policy)
                    or claim.request_id != server_request_id(self.core.binding, authorization)
                    or not _instant(authorization.approved_at) <= _instant(claim.claimed_at)
                    < _instant(authorization.valid_until)):
                raise engine.StoreUnavailable('Stored owner inconsistent with run authorization')

    @property
    def revision(self):
        auth = None if self.authorization is None else asdict(self.authorization)
        digest = sha256(json.dumps(auth, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return 'command-offline/v1:' + json.dumps([self.core.revision, digest], separators=(',', ':'))

    def item(self):
        result = {**self.core.item(), 'command_schema': engine._av(COMMAND_SCHEMA),
                  'run_authorization': engine._av(
                      None if self.authorization is None else asdict(self.authorization))}
        if len(json.dumps(result, ensure_ascii=False).encode()) > engine.MAX_WIRE_ITEM_BYTES:
            raise engine.StoreUnavailable('Draft item size bound exceeded')
        return result

    @classmethod
    def decode(cls, item, binding):
        if type(item) is not dict or set(item) != _DRAFT_FIELDS:
            raise engine.StoreUnavailable('Explicit complete command draft required; no migration')
        if item['command_schema'] != engine._av(COMMAND_SCHEMA):
            raise engine.StoreUnavailable('Unknown command draft schema')
        core = engine._snapshot({name: item[name] for name in _CORE_FIELDS}, binding)
        authorization = engine._value(item['run_authorization'])
        if authorization is not None:
            authorization = RunAuthorizationDraft.decode(authorization)
        result = cls(core, authorization)
        result.item()  # Validate whole draft size, not just its V3 projection.
        return result

    def view(self):
        return dict(command_schema=COMMAND_SCHEMA, authority=asdict(self.core.binding),
                    generation=self.core.generation, document=json.loads(self.core.state.encode()),
                    run_authorization=None if self.authorization is None else asdict(self.authorization))


@dataclass(frozen=True)
class SyntheticServerActor:
    """Authority output in the test model, never accepted as invocation evidence.

    Only the injected authentication service can return one. Constructing this
    object in command data or passing it as an invocation confers no permission.
    A real SigV4/operator authority is deliberately not implemented.
    """

    principal_id: str
    kind: str
    authority_id: str
    incarnation: str
    expires_at: int


@dataclass(frozen=True)
class CommandResult:
    accepted: bool
    reason: str
    requires_reconciliation: bool = False
    revision: str | None = None
    control: dict | None = None

    @property
    def execution_allowed(self):
        return False

    @property
    def retry_allowed(self):
        return False


class _BoundTransport:
    """Per-command projection, NOT another store or authorization boundary.

    Pin the run authorization and extend every atomic condition. The unchanged
    CAS engine sees its existing schema; the real synthetic item remains whole.
    No caller-provided expressions or mutable global read cache is involved.
    """

    def __init__(self, raw, binding):
        self._raw, self._binding = raw, binding
        self._pinned = self.last = None
        self.before_write = None

    def _decode(self, item):
        snapshot = CommandSnapshotDraft.decode(item, self._binding)
        if self._pinned is not None and snapshot.authorization != self._pinned.authorization:
            raise engine.StoreUnavailable('Run authorization changed; reconcile')
        if self._pinned is None:
            self._pinned = snapshot
        return snapshot

    def get_item(self, **request):
        response = self._raw.get_item(**request)
        engine._ack(response)
        self.last = self._decode(response.get('Item'))
        return {**response, 'Item': self.last.core.item()}

    def update_item(self, **request):
        if self._pinned is None or not callable(self.before_write):
            raise engine.StoreUnavailable('Server authorization unavailable')
        self.before_write()  # Recheck independent auth/clock before the only send.
        forwarded = deepcopy(request)
        forwarded['ExpressionAttributeNames'].update(
            {'#commandSchema': 'command_schema', '#authorization': 'run_authorization'})
        forwarded['ExpressionAttributeValues'].update({
            ':commandSchema': engine._av(COMMAND_SCHEMA),
            ':authorization': engine._av(None if self._pinned.authorization is None
                                         else asdict(self._pinned.authorization))})
        forwarded['ConditionExpression'] += (
            ' AND #commandSchema = :commandSchema AND #authorization = :authorization')
        response = self._raw.update_item(**forwarded)  # Exactly one raw write, never retry.
        engine._ack(response)
        after = self._decode(response.get('Attributes'))
        return {**response, 'Attributes': after.core.item()}


class OfflineCommandGateway:
    """Trusted TEST-HARNESS composition, not a server or production factory.

    Authentication implements reader/worker/operator using trusted contexts
    created outside the request. Missing configuration fails closed. Workers
    must never be able to construct or reconfigure this object in a real server.
    """

    def __init__(self, *, transport, binding, authentication=None, clock=None):
        self._transport, self._binding = transport, binding
        self._authentication, self._clock = authentication, clock

    def _now(self):
        return _clock(self._clock)

    def _actor(self, method, invocation, kinds):
        now = self._now()
        authenticate = getattr(self._authentication, method, None)
        if not callable(authenticate):
            raise engine.StoreUnavailable('Independent authentication unavailable')
        actor = authenticate(invocation, self._binding, now=now)
        if (type(actor) is not SyntheticServerActor or actor.kind not in kinds
                or type(actor.principal_id) is not str or not actor.principal_id.startswith('synthetic://')
                or actor.authority_id != self._binding.authority_id
                or actor.incarnation != self._binding.incarnation
                or type(actor.expires_at) is not int or actor.expires_at <= now.timestamp()):
            raise engine.StoreUnavailable('Independent principal rejected')
        return actor

    def _worker(self, invocation, snapshot, actor):
        now = self._now()
        authorization = snapshot.authorization
        if (authorization is None or not _instant(authorization.approved_at) <= now
                < _instant(authorization.valid_until)):
            raise engine.StoreUnavailable('Approved run unavailable or expired')
        control = snapshot.core.state.control
        if (not 0 <= (now - _instant(control.pending.reserved_at)).total_seconds()
                <= STATUS_MAX_AGE.total_seconds()
                or control.daily_quota.day_utc != now.date().isoformat()):
            raise engine.StoreUnavailable('Reserved intent stale or future')
        if self._actor('reader', invocation, {'worker'}) != actor:
            raise engine.StoreUnavailable('Invocation principal changed')
        verify = getattr(self._authentication, 'worker', None)
        if not callable(verify):
            raise engine.StoreUnavailable('Worker identity authority unavailable')
        identity = verify(invocation, authorization, now=now)
        if type(identity) is not ActionsOIDCIdentity:
            raise engine.StoreUnavailable('Independently checked worker identity required')
        owner = ClaimOwner(**{name: getattr(identity, name) for name in ClaimOwner.__dataclass_fields__})
        if (owner != _policy_owner(authorization.policy) or type(identity.expires_at) is not int
                or identity.expires_at <= now.timestamp()):
            raise engine.StoreUnavailable('Identity does not match approved run')
        return owner, now

    @staticmethod
    def _request(command, payload):
        fields = {'read_control': set(),
                  'claim_reserved_intent': {'intent_id', 'approval_epoch', 'expected_revision'},
                  'emergency_stop': {'expected_revision'}}
        if type(command) is not str or command not in fields or type(payload) is not dict:
            return False
        if set(payload) != fields[command]:
            return False
        if command != 'read_control':
            revision = payload['expected_revision']
            if type(revision) is not str or not 1 <= len(revision) <= 1536:
                return False
        if command == 'claim_reserved_intent':
            return (_hex(payload['intent_id'], 64) and type(payload['approval_epoch']) is int
                    and 1 <= payload['approval_epoch'] < 100000000)
        return True

    def handle(self, command, payload, *, invocation=None):
        """No identity/config/store/expression/owner overrides in command data."""
        if not self._request(command, payload):
            return CommandResult(False, 'command_schema_rejected')
        try:
            kinds = {'operator'} if command == 'emergency_stop' else {'worker', 'operator', 'controller'}
            method = 'operator' if command == 'emergency_stop' else 'reader'
            actor = self._actor(method, invocation, kinds)
            if command == 'claim_reserved_intent' and actor.kind != 'worker':
                return CommandResult(False, 'worker_command_not_authorized')
            projection = _BoundTransport(self._transport, self._binding)
            store = engine.DynamoConditionalStorePrototype(transport=projection, binding=self._binding)
            core = store.read()
            snapshot = projection.last
            if command == 'read_control':
                return CommandResult(True, 'synthetic_control_observed_execution_denied',
                                     revision=snapshot.revision, control=snapshot.view())
            if snapshot.revision != payload['expected_revision']:
                return CommandResult(False, 'revision_conflict_reconcile', True)
            if command == 'emergency_stop':
                if not core.state.control.enabled:
                    return CommandResult(True, 'controller_already_off_execution_denied',
                                         revision=snapshot.revision)
                proposed = replace(core.state, control=disable_controller(core.state.control))
                def check_operator():
                    if self._actor('operator', invocation, {'operator'}) != actor:
                        raise engine.StoreUnavailable('Operator authorization changed')
                projection.before_write = check_operator
            else:
                if not core.state.control.enabled:
                    return CommandResult(False, 'persistent_kill_switch_disabled')
                if (core.state.control.pending is None
                        or core.state.control.pending.intent_id != payload['intent_id']
                        or core.state.control.approval_epoch != payload['approval_epoch']):
                    return CommandResult(False, 'reserved_intent_or_epoch_mismatch')
                if core.state.worker_claim is not None:
                    return CommandResult(False, 'intent_already_owned_reconcile', True)
                owner, now = self._worker(invocation, snapshot, actor)
                claim = WorkerClaim(snapshot.authorization.intent_id, snapshot.authorization.approval_epoch,
                                    server_request_id(self._binding, snapshot.authorization), owner, _iso(now))
                proposed = replace(core.state, worker_claim=claim)
                projection.before_write = lambda: self._worker(invocation, snapshot, actor)
        except Exception:
            return CommandResult(False, 'authority_or_authentication_unavailable', True)
        result = store.compare_and_swap(core.revision, proposed)
        return CommandResult(result.confirmed, result.reason, result.requires_reconciliation)


# No deployed trust, network route, production backend or runtime registration.
PRODUCTION_COMMAND_GATEWAY = None

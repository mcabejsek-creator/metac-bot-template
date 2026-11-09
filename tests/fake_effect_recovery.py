"""Thin fault/metric helpers over the existing one-row SQLite Dynamo model.

Counters/provider history observe effects; they are NOT an admission ledger.
The provider deliberately has NO idempotency-key support or deduplication.
"""

from dataclasses import dataclass
from threading import Lock

if __package__:
    from .fake_dynamodb_control import SyntheticDynamoTransport
else:
    from fake_dynamodb_control import SyntheticDynamoTransport

from durable_effect_offline import IndependentEffectEvidence, SyntheticProviderReply, _digest
from dispatch_intent_controller import _iso


class MeteredSyntheticDynamo(SyntheticDynamoTransport):
    def __init__(self,path,**kwargs):
        super().__init__(path,**kwargs)
        self.commits, self.after_commit, self._metric_lock = 0, None, Lock()
        self.after_update = self._committed

    def _committed(self,request):
        with self._metric_lock:
            self.commits += 1
        if self.after_commit:
            self.after_commit(request)


@dataclass(frozen=True)
class FakeEffect:
    operation_id: str
    attempt_id: str
    request_digest: str
    result_digest: str
    charged_usd: str


class NonIdempotentSyntheticProvider:
    def __init__(self):
        self.entries = self.wire_attempts = self.inflight = 0
        self.effects, self.calls = [], []
        self.status, self.cost, self.apply_effect = 200, '0.10', True
        self.error = self.before_wire = self.after_effect = None
        self.billing_available = True
        self.physical_attempts_per_call = 1
        self._lock = Lock()

    def send_once(self,definition,operation_id,attempt_id):
        with self._lock:
            self.entries += 1
            self.inflight += 1
            self.calls.append((definition,operation_id,attempt_id))
        try:
            if self.before_wire:
                self.before_wire()
            # Setting this to 2 is an explicit faulty-transport counterexample,
            # never a permitted production option or a request parameter.
            for _ in range(self.physical_attempts_per_call):
                with self._lock:
                    self.wire_attempts += 1
                    if self.apply_effect:
                        self.effects.append(FakeEffect(operation_id,attempt_id,definition.request_digest,
                            _digest([operation_id,attempt_id,'effect']),self.cost))
            if self.after_effect:
                self.after_effect()
            if self.error:
                raise self.error('Synthetic provider outcome unavailable')
            return SyntheticProviderReply(self.status,_digest([operation_id,self.status,'response']))
        finally:
            with self._lock:
                self.inflight -= 1


class SyntheticIndependentOutcomeAuthority:
    """Trusted test service, registered OUTSIDE request data; no real audit API.

    Only a positive final effect with known billing can be verified. Absence,
    incomplete history, in-flight work or duplicate effects cannot be confirmed.
    """

    def __init__(self):
        self.sources = {}
        self.available = True

    def register(self,controller_invocation,provider):
        self.sources[controller_invocation] = provider

    def verify(self,invocation,snapshot,*,now):
        provider = self.sources.get(invocation)
        if not self.available or provider is None or not provider.billing_available or provider.inflight:
            raise RuntimeError('Independent source unavailable or incomplete')
        if len(provider.effects)!=1:
            raise RuntimeError('No unique positive independently verified effect')
        event = provider.effects[0]
        if (event.operation_id,event.attempt_id,event.request_digest)!=(
                snapshot.effect.operation_id,snapshot.effect.attempt.attempt_id,
                snapshot.effect.definition.request_digest):
            raise RuntimeError('Independent provider correlation mismatch')
        return IndependentEffectEvidence(event.operation_id,event.attempt_id,event.request_digest,
            'synthetic://independent/'+event.operation_id,event.result_digest,event.charged_usd,_iso(now))

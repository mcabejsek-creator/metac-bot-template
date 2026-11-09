"""Explicit trusted TEST authority. No AWS/SigV4/operator implementation.

The fixture registers opaque invocation handles outside worker command data.
Its in-process registry models ingress authentication, NOT durable ownership or
an independent replay ledger. Claim ownership remains solely in the SQLite row.
"""

from dataclasses import dataclass

from command_gateway_offline import SyntheticServerActor
from worker_identity import GitHubActionsOIDCVerifier


@dataclass(frozen=True)
class SyntheticKeySnapshot:
    generation: int
    public_keys: tuple
    not_before: object
    not_after: object


class SyntheticCommandAuthentication:
    def __init__(self, binding, *, keys, now):
        self._binding, self._now = binding, now
        self._invocations = {}
        self.keys = {snapshot.generation: snapshot for snapshot in keys}
        self.authorization_available = True
        self.worker_keys_available = True
        self.operator_root_available = True
        self.verifications = 0

    def _register(self, kind, token=None):
        handle = object()
        actor = SyntheticServerActor('synthetic://invoke-' + kind, kind,
                                     self._binding.authority_id, self._binding.incarnation,
                                     int(self._now.timestamp()) + 600)
        self._invocations[handle] = (actor, token)
        return handle

    def worker_invocation(self, compact_token):
        return self._register('worker', compact_token)

    def operator_invocation(self):
        return self._register('operator')

    def controller_invocation(self):
        return self._register('controller')

    def reader(self, invocation, binding, *, now):
        if not self.authorization_available or binding != self._binding:
            raise RuntimeError('Synthetic authorization unavailable')
        try:
            actor, _ = self._invocations[invocation]
        except (KeyError, TypeError):
            raise RuntimeError('Unregistered synthetic ingress context') from None
        if actor.expires_at <= now.timestamp():
            raise RuntimeError('Synthetic principal expired')
        return actor

    def operator(self, invocation, binding, *, now):
        actor = self.reader(invocation, binding, now=now)
        if actor.kind != 'operator' or not self.operator_root_available:
            raise RuntimeError('Independent synthetic operator root required')
        return actor

    def worker(self, invocation, authorization, *, now):
        actor = self.reader(invocation, self._binding, now=now)
        if actor.kind != 'worker' or not self.worker_keys_available:
            raise RuntimeError('Worker trust unavailable')
        snapshot = self.keys.get(authorization.trust_generation)
        if (type(snapshot) is not SyntheticKeySnapshot
                or snapshot.generation != authorization.trust_generation or not snapshot.public_keys
                or not snapshot.not_before <= now < snapshot.not_after):
            raise RuntimeError('Independent trust snapshot unavailable')
        self.verifications += 1
        token = self._invocations[invocation][1]
        verifier = GitHubActionsOIDCVerifier(authorization.policy, snapshot.public_keys)
        return verifier.verify(token, now=now)

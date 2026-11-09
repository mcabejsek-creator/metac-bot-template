"""Adversarial command model: synthetic RSA keys/ingress/SQLite only, no service.

Standalone: python -B tests/test_command_gateway_offline.py
Crypto compatibility requires locked PyJWT 2.10.1 / cryptography 45.0.4.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __package__:
    from .fake_command_auth import SyntheticCommandAuthentication, SyntheticKeySnapshot
    from .fake_dynamodb_control import SyntheticDynamoTransport, ModelError
    from .test_worker_claim_offline import reserved, NOW
    from .test_autonomous_runtime_offline import reject_network, NETWORK_ATTEMPTS
else:
    from fake_command_auth import SyntheticCommandAuthentication, SyntheticKeySnapshot
    from fake_dynamodb_control import SyntheticDynamoTransport, ModelError
    from test_worker_claim_offline import reserved, NOW
    from test_autonomous_runtime_offline import reject_network, NETWORK_ATTEMPTS

if __name__ == '__main__':
    sys.addaudithook(reject_network)

import command_gateway_offline as command
import dynamodb_cas_offline as engine
import worker_claim_offline as v3
import worker_identity as identity
import worker_configuration
from dispatch_intent_controller import disable_controller
from perplexity_run_budget import MAX_AUTO_AGENT_POST_ATTEMPTS


class ProcessInterrupted(BaseException):
    pass


class CommandGatewayOfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        cls.jwt = jwt
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = cls.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        cls.other_pem = cls.other_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        cls.policy = identity.ControllerOIDCPolicy(
            audience='urn:synthetic:command-gateway',
            subject='repo:synthetic-owner/repo:ref:refs/heads/test',
            repository='synthetic-owner/repo', repository_id='12345', repository_owner_id='67890',
            source_sha='a' * 40, workflow_sha='b' * 40, run_id='999001',
            workflow_ref='synthetic-owner/repo/.github/workflows/worker.yaml@refs/heads/test')

    def token(self, *, key=None, kid='synthetic-command-kid', **changes):
        stamp = int(NOW.timestamp())
        claims = dict(iss=identity.GITHUB_ISSUER, aud=self.policy.audience, sub=self.policy.subject,
                      iat=stamp-30, nbf=stamp-30, exp=stamp+120, repository=self.policy.repository,
                      repository_id=self.policy.repository_id, repository_owner_id=self.policy.repository_owner_id,
                      sha=self.policy.source_sha, workflow_ref=self.policy.workflow_ref,
                      workflow_sha=self.policy.workflow_sha, run_id=self.policy.run_id, run_attempt='1')
        return self.jwt.encode({**claims, **changes}, self.key if key is None else key,
                               algorithm='RS256', headers={'kid': kid})

    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory(prefix='synthetic-command-'))
        self.serial = 0
        self.binding = engine.AuthorityBinding('offline-control', 'controller', 'a'*32,
                                               'offline://account/region/table')
        self.authorization = command.RunAuthorizationDraft(
            'c'*32, reserved().pending.intent_id, 3,
            '2026-11-09T12:30:00Z', '2026-11-09T12:35:00Z', 1, self.policy)
        keys = SyntheticKeySnapshot(1, (('synthetic-command-kid', self.pem),),
                                    NOW-timedelta(minutes=1), NOW+timedelta(minutes=10))
        self.auth = SyntheticCommandAuthentication(self.binding, keys=(keys,), now=NOW)
        self.worker = self.auth.worker_invocation(self.token())
        self.operator = self.auth.operator_invocation()
        self.controller = self.auth.controller_invocation()
        self.make()

    def make(self, *, authorization='default', generation=1, state=None, binding=None):
        self.serial += 1
        self.path = str(Path(self.directory) / f'control-{self.serial}.sqlite')
        self.seed = command.CommandSnapshotDraft(
            engine.Snapshot(v3.OfflineControlStateV3(reserved()) if state is None else state,
                            generation, self.binding if binding is None else binding),
            self.authorization if authorization == 'default' else authorization)
        self.raw = SyntheticDynamoTransport.seed(self.path, self.seed.item())
        self.gateway = command.OfflineCommandGateway(
            transport=self.raw, binding=self.binding, authentication=self.auth, clock=lambda: NOW)

    def request(self):
        return dict(intent_id=self.seed.core.state.control.pending.intent_id,
                    approval_epoch=3, expected_revision=self.seed.revision)

    def claim(self, *, invocation='default', payload=None, gateway=None):
        result = (self.gateway if gateway is None else gateway).handle(
            'claim_reserved_intent', self.request() if payload is None else payload,
            invocation=self.worker if invocation == 'default' else invocation)
        self.assertIs(result.execution_allowed, False)
        self.assertIs(result.retry_allowed, False)
        return result

    def current(self):
        return command.CommandSnapshotDraft.decode(self.raw.current(), self.binding)

    def writes(self):
        return [request for method, request in self.raw.calls if method == 'UpdateItem']

    def stop(self, *, invocation='default', revision=None):
        return self.gateway.handle('emergency_stop',
            {'expected_revision': self.current().revision if revision is None else revision},
            invocation=self.operator if invocation == 'default' else invocation)

    def test_crypto_versions_match_project_lock(self):
        self.assertEqual(importlib.metadata.version('PyJWT'), '2.10.1')
        self.assertEqual(importlib.metadata.version('cryptography'), '45.0.4')

    def test_missing_authentication_invocation_or_store_always_denies(self):
        for invocation in (None, object(), True, {'verified': True}, self.policy,
                           identity.ActionsOIDCIdentity('12345', '67890', 'a'*40,
                               self.policy.workflow_ref, 'b'*40, '999001', '1', int(NOW.timestamp())+100)):
            self.assertFalse(self.claim(invocation=invocation).accepted)
        for kwargs in ({'authentication': None}, {'transport': None}, {'clock': None}):
            options = dict(transport=self.raw, binding=self.binding, authentication=self.auth, clock=lambda: NOW)
            options.update(kwargs)
            self.assertFalse(self.claim(gateway=command.OfflineCommandGateway(**options)).accepted)
        self.assertEqual(self.writes(), [])

    def test_read_control_requires_server_context_and_returns_whole_draft_without_writes(self):
        for invocation in (self.worker, self.operator, self.controller):
            result = self.gateway.handle('read_control', {}, invocation=invocation)
            self.assertTrue(result.accepted)
            self.assertFalse(result.execution_allowed)
            self.assertFalse(result.retry_allowed)
            self.assertEqual(result.control, self.seed.view())
            self.assertEqual(result.revision, self.seed.revision)
            result.control['document']['enabled'] = False
            self.assertTrue(self.current().core.state.control.enabled)
        self.assertFalse(self.gateway.handle('read_control', {}, invocation=object()).accepted)
        self.assertEqual(self.writes(), [])

    def test_owner_and_request_id_are_derived_on_server_and_quota_is_unchanged(self):
        self.assertTrue(self.claim().accepted)
        after = self.current()
        self.assertEqual(after.core.generation, 2)
        self.assertEqual(after.core.state.control, self.seed.core.state.control)
        self.assertEqual(after.authorization, self.seed.authorization)
        self.assertEqual(after.core.state.worker_claim.owner.run_id, self.policy.run_id)
        self.assertEqual(after.core.state.worker_claim.owner.source_sha, self.policy.source_sha)
        self.assertEqual(after.core.state.worker_claim.request_id,
                         command.server_request_id(self.binding, self.authorization))
        self.assertEqual(len(self.writes()), 1)
        request = self.writes()[0]
        self.assertIn('#authorization = :authorization', request['ConditionExpression'])
        self.assertEqual(request['ExpressionAttributeValues'][':authorization'],
                         engine._av(asdict(self.authorization)))

    def test_caller_cannot_supply_owner_request_id_state_expressions_binding_or_authority(self):
        for name, value in [('owner', {'run_id': '999002'}), ('request_id', 'f'*32),
                            ('verified', True), ('operator_stop_authorized', True),
                            ('document', self.seed.view()), ('UpdateExpression', 'SET generation = 0'),
                            ('ConditionExpression', 'true'), ('table_name', 'other'), ('pk', 'other'),
                            ('incarnation', 'b'*32), ('authority_id', 'offline://other'),
                            ('role', 'operator'), ('claims', {'run_id': self.policy.run_id}),
                            ('execution_allowed', True), ('generation', 0)]:
            with self.subTest(name=name):
                result = self.claim(payload={**self.request(), name: value})
                self.assertEqual(result.reason, 'command_schema_rejected')
                self.assertFalse(result.accepted)
        self.assertEqual(self.writes(), [])

    def test_unknown_malformed_and_unimplemented_commands_are_deny(self):
        for name in ['reserve_intent', 'bind_approved_run', 'settle_intent', 'quota_day_review',
                     'release', 'takeover', 're_enable', 'restore', 'compare_and_swap', '', True]:
            self.assertFalse(self.gateway.handle(name, {}, invocation=self.controller).accepted)
        for payload in (None, [], True, {'expected_revision': True}, {}, {**self.request(), 'approval_epoch': True}):
            self.assertFalse(self.claim(payload=payload if payload is not None else []).accepted)
        for name in ('intent_id', 'approval_epoch', 'expected_revision'):
            data = self.request()
            del data[name]
            self.assertFalse(self.claim(payload=data).accepted)
        self.assertEqual(self.writes(), [])

    def test_two_workers_with_same_revision_have_one_positive_confirmation(self):
        barrier = Barrier(2)
        self.raw.before_update = lambda _request: barrier.wait(timeout=4)
        other_worker = self.auth.worker_invocation(self.token(jti='other-synthetic-token'))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda context: self.claim(invocation=context), (self.worker, other_worker)))
        self.assertEqual(sum(result.accepted for result in results), 1)
        self.assertTrue(all(not result.execution_allowed for result in results))
        self.assertEqual(self.current().core.generation, 2)
        self.assertEqual(len(self.writes()), 2)

    def test_replay_same_request_new_token_and_other_run_do_not_reconfirm(self):
        self.assertTrue(self.claim().accepted)
        owned_revision = self.current().revision
        for token in (self.token(), self.token(jti='replacement'), self.token(run_id='999002')):
            invocation = self.auth.worker_invocation(token)
            self.assertFalse(self.claim(invocation=invocation).accepted)
            self.assertFalse(self.claim(invocation=invocation,
                                       payload={**self.request(), 'expected_revision': owned_revision}).accepted)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.current().core.state.control.daily_quota, self.seed.core.state.control.daily_quota)

    def test_other_run_wrong_repo_commits_workflow_attempt_audience_or_issuer_denied(self):
        for changes in [dict(run_id='999002'), dict(repository='attacker/repo'), dict(repository_id='11111'),
                        dict(repository_owner_id='11111'), dict(sha='d'*40), dict(workflow_sha='d'*40),
                        dict(workflow_ref=self.policy.workflow_ref.replace('worker.yaml', 'other.yaml')),
                        dict(run_attempt='2'), dict(run_attempt=1), dict(aud='sts.amazonaws.com'),
                        dict(aud=[self.policy.audience]), dict(iss='https://attacker.invalid'),
                        dict(sub='repo:attacker/repo:ref:refs/heads/test')]:
            with self.subTest(changes=changes):
                context = self.auth.worker_invocation(self.token(**changes))
                self.assertFalse(self.claim(invocation=context).accepted)
        self.assertEqual(self.writes(), [])

    def test_forged_malformed_unsigned_or_missing_token_cannot_grant_identity(self):
        unsigned = self.jwt.encode({'run_id': self.policy.run_id}, key='', algorithm='none')
        for token in (None, '', 'not-a-jwt', {'verified': True}, self.token(key=self.other_key),
                      self.token(kid='unknown-kid'), unsigned):
            self.assertFalse(self.claim(invocation=self.auth.worker_invocation(token)).accepted)
        self.assertEqual(self.writes(), [])

    def test_expired_future_and_excessively_long_token_denied_without_writes(self):
        stamp = int(NOW.timestamp())
        for changes in [dict(exp=stamp), dict(iat=stamp+1, nbf=stamp+1), dict(exp=stamp+601),
                        dict(exp=True), dict(nbf=stamp+1), dict(iat='yesterday')]:
            self.assertFalse(self.claim(invocation=self.auth.worker_invocation(self.token(**changes))).accepted)
        self.assertEqual(self.writes(), [])

    def test_missing_rotated_expired_or_unavailable_keys_and_trust_deny(self):
        first = self.auth.keys[1]
        snapshots = [None, replace(first, public_keys=()), replace(first, generation=2),
                     replace(first, public_keys=(('new-kid', self.other_pem),)),
                     replace(first, not_after=NOW), replace(first, not_before=NOW+timedelta(seconds=1))]
        for snapshot in snapshots:
            self.auth.keys = {} if snapshot is None else {1: snapshot}
            self.assertFalse(self.claim().accepted)
        self.auth.keys = {1: first}
        self.auth.worker_keys_available = False
        self.assertFalse(self.claim().accepted)
        self.assertEqual(self.writes(), [])

    def test_independently_seeded_new_trust_generation_accepts_only_new_key(self):
        rotated = replace(self.auth.keys[1], generation=2, public_keys=(('new-kid', self.other_pem),))
        self.auth.keys = {2: rotated}
        self.make(authorization=replace(self.authorization, trust_generation=2))
        self.assertFalse(self.claim().accepted)
        invocation = self.auth.worker_invocation(self.token(key=self.other_key, kid='new-kid'))
        self.assertTrue(self.claim(invocation=invocation).accepted)

    def test_unauthorized_or_expired_operator_and_worker_stop_are_denied(self):
        for invocation in (None, True, {'operator_stop_authorized': True}, self.worker, self.controller, object()):
            self.assertFalse(self.stop(invocation=invocation).accepted)
        with patch.object(self.gateway, '_clock', return_value=NOW+timedelta(seconds=600)):
            self.assertFalse(self.stop().accepted)
            self.assertFalse(self.gateway.handle('read_control', {}, invocation=self.operator).accepted)
        self.auth.operator_root_available = False
        self.assertFalse(self.stop().accepted)
        self.assertEqual(self.writes(), [])

    def test_operator_stop_is_independent_of_worker_keys_and_preserves_entire_state(self):
        self.assertTrue(self.claim().accepted)
        owned = self.current()
        self.auth.worker_keys_available = False
        with patch.object(self.gateway, '_clock', return_value=NOW+timedelta(minutes=5)):
            result = self.stop()  # Worker token and run window expired; independent STOP still works.
        self.assertTrue(result.accepted)
        self.assertFalse(result.execution_allowed)
        stopped = self.current()
        self.assertFalse(stopped.core.state.control.enabled)
        self.assertEqual(stopped.core.state.worker_claim, owned.core.state.worker_claim)
        self.assertEqual(stopped.authorization, owned.authorization)
        self.assertEqual(stopped.core.state.control.daily_quota, owned.core.state.control.daily_quota)
        self.assertEqual(stopped.core.generation, 3)
        self.assertTrue(self.stop().accepted)
        self.assertEqual(len(self.writes()), 2)

    def test_stop_before_claim_and_between_read_and_put_blocks_owner(self):
        self.assertTrue(self.stop().accepted)
        self.assertFalse(self.claim().accepted)
        self.make()
        off = replace(self.seed, core=engine.Snapshot(
            replace(self.seed.core.state, control=disable_controller(self.seed.core.state.control)),
            2, self.binding))
        self.raw.before_update = lambda _request: self.raw.admin_replace(off.item())
        result = self.claim()
        self.assertFalse(result.accepted)
        self.assertTrue(result.requires_reconciliation)
        self.assertEqual(self.current(), off)

    def test_stop_after_claim_commit_makes_claim_ack_fail_closed(self):
        def stop_after(_request):
            self.raw.after_update = None
            current = self.current()
            stopped = replace(current, core=engine.Snapshot(
                replace(current.core.state, control=disable_controller(current.core.state.control)),
                3, self.binding))
            self.raw.admin_replace(stopped.item())
        self.raw.after_update = stop_after
        result = self.claim()
        self.assertFalse(result.accepted)
        self.assertTrue(result.requires_reconciliation)
        self.assertFalse(self.current().core.state.control.enabled)
        self.assertIsNotNone(self.current().core.state.worker_claim)

    def test_changed_reservation_quota_or_run_approval_is_guarded_atomically(self):
        quota_changed = replace(self.seed, core=replace(self.seed.core, state=replace(
            self.seed.core.state, control=replace(self.seed.core.state.control,
                daily_quota=replace(self.seed.core.state.control.daily_quota, reserved_usd='0.80')))))
        next_control = reserved(NOW+timedelta(minutes=15))
        next_auth = replace(self.authorization, intent_id=next_control.pending.intent_id,
                            approved_at='2026-11-09T12:45:00Z', valid_until='2026-11-09T12:50:00Z')
        next_reservation = command.CommandSnapshotDraft(
            engine.Snapshot(v3.OfflineControlStateV3(next_control), 2, self.binding), next_auth)
        other_run = replace(self.seed, authorization=replace(
            self.authorization, policy=replace(self.policy, run_id='999002')))
        for changed in (quota_changed, next_reservation, other_run):
            self.make()
            self.raw.before_update = lambda _request, value=changed: self.raw.admin_replace(value.item())
            self.assertFalse(self.claim().accepted)
            self.assertEqual(self.current(), changed)
            self.assertEqual(len(self.writes()), 1)

    def test_changed_authorization_without_generation_changes_public_revision(self):
        other = replace(self.seed, authorization=replace(self.authorization, authorization_id='d'*32))
        self.assertEqual(other.core.revision, self.seed.core.revision)
        self.assertNotEqual(other.revision, self.seed.revision)
        self.raw.admin_replace(other.item())
        self.assertFalse(self.claim().accepted)
        self.assertEqual(self.writes(), [])

    def test_no_authorization_wrong_intent_epoch_or_future_approval_never_claims(self):
        self.make(authorization=None)
        self.assertFalse(self.claim().accepted)
        self.make()
        for changes in ({'intent_id': 'd'*64}, {'approval_epoch': 4}):
            self.assertFalse(self.claim(payload={**self.request(), **changes}).accepted)
        self.make(authorization=replace(self.authorization, approved_at='2026-11-09T12:30:01Z'))
        self.assertFalse(self.claim().accepted)
        self.assertEqual(self.writes(), [])

    def test_operator_and_internal_service_cannot_claim_as_worker(self):
        for context in (self.operator, self.controller):
            self.assertEqual(self.claim(invocation=context).reason, 'worker_command_not_authorized')
        self.assertEqual(self.writes(), [])

    def test_boolean_or_claims_return_from_broken_authority_is_not_evidence(self):
        for value in (True, {'verified': True}, self.policy):
            with patch.object(self.auth, 'worker', return_value=value):
                self.assertFalse(self.claim().accepted)
        for value in (True, {'operator': True}, self.policy):
            with patch.object(self.auth, 'operator', return_value=value):
                self.assertFalse(self.stop().accepted)
        self.assertEqual(self.writes(), [])

    def test_database_authorization_and_clock_outages_fail_closed(self):
        self.auth.authorization_available = False
        self.assertFalse(self.claim().accepted)
        self.auth.authorization_available = True
        for kind in (TimeoutError, ConnectionResetError):
            with patch.object(self.raw, 'get_item', side_effect=kind()):
                self.assertFalse(self.claim().accepted)
                self.assertFalse(self.stop().accepted)
        gateway = command.OfflineCommandGateway(transport=self.raw, binding=self.binding,
            authentication=self.auth, clock=lambda: NOW.replace(tzinfo=None))
        self.assertFalse(self.claim(gateway=gateway).accepted)
        self.assertEqual(self.writes(), [])

    def test_authentication_is_rechecked_immediately_before_write(self):
        def revoke_after_second_read(response):
            if sum(method == 'GetItem' for method, _ in self.raw.calls) == 2:
                self.auth.worker_keys_available = False
            return response
        self.raw.read_filter = revoke_after_second_read
        self.assertFalse(self.claim().accepted)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.current(), self.seed)

    def test_token_expiry_between_preflight_and_send_denies_without_raw_write(self):
        gateway = command.OfflineCommandGateway(transport=self.raw, binding=self.binding,
            authentication=self.auth, clock=lambda: NOW)
        # Force expiry at the write check independently of the exact number of
        # earlier auth calls; the transport hook is controlled by the fixture.
        def expire_after_read(response):
            if sum(method == 'GetItem' for method, _ in self.raw.calls) == 2:
                gateway._clock = lambda: NOW+timedelta(seconds=120)
            return response
        self.raw.read_filter = expire_after_read
        self.assertFalse(self.claim(gateway=gateway).accepted)
        self.assertEqual(self.writes(), [])

    def test_timeout_and_connection_loss_after_commit_stop_without_retry(self):
        for kind in (TimeoutError, ConnectionResetError):
            self.make()
            def fail(_request, error=kind):
                raise error('synthetic lost ACK')
            self.raw.after_update = fail
            result = self.claim()
            self.assertFalse(result.accepted)
            self.assertTrue(result.requires_reconciliation)
            self.assertEqual(len(self.writes()), 1)
            self.assertIsNotNone(self.current().core.state.worker_claim)
            self.raw.after_update = None
            self.assertFalse(self.claim().accepted)
            self.assertEqual(len(self.writes()), 1)

    def test_process_interruption_then_restarted_handler_retains_owner(self):
        def interrupt(_request):
            raise ProcessInterrupted()
        self.raw.after_update = interrupt
        with self.assertRaises(ProcessInterrupted):
            self.claim()
        restarted = command.OfflineCommandGateway(transport=SyntheticDynamoTransport(self.path),
            binding=self.binding, authentication=self.auth, clock=lambda: NOW)
        result = self.claim(gateway=restarted,
                            payload={**self.request(), 'expected_revision': self.current().revision})
        self.assertFalse(result.accepted)
        self.assertEqual(self.current().core.generation, 2)

    def test_fresh_process_restart_cannot_confirm_another_owner(self):
        self.assertTrue(self.claim().accepted)
        script = '''
import json,sys
from test_autonomous_runtime_offline import reject_network, NETWORK_ATTEMPTS
sys.addaudithook(reject_network)
from fake_dynamodb_control import SyntheticDynamoTransport
from command_gateway_offline import CommandSnapshotDraft, OfflineCommandGateway, SyntheticServerActor
from dynamodb_cas_offline import AuthorityBinding
from datetime import datetime
d=json.load(sys.stdin)
b=AuthorityBinding(**d['binding'])
t=SyntheticDynamoTransport(d['path'])
s=CommandSnapshotDraft.decode(t.current(),b)
handle=object()
class Auth:
 def reader(self,context,binding,*,now):
  if context is not handle: raise ValueError('untrusted')
  return SyntheticServerActor('synthetic://restart-worker','worker',b.authority_id,b.incarnation,int(now.timestamp())+60)
 def worker(self,*args,**kwargs): raise AssertionError('owner already exists')
g=OfflineCommandGateway(transport=t,binding=b,authentication=Auth(),clock=lambda:datetime.fromisoformat(d['now']))
r=g.handle('claim_reserved_intent',dict(intent_id=s.core.state.control.pending.intent_id,approval_epoch=3,expected_revision=s.revision),invocation=handle)
print(json.dumps(dict(accepted=r.accepted,execution_allowed=r.execution_allowed,writes=sum(n=='UpdateItem' for n,_ in t.calls),network_attempts=NETWORK_ATTEMPTS)))
'''
        root = Path(__file__).resolve().parents[1]
        env = {n:v for n,v in os.environ.items() if not n.endswith(('_TOKEN', '_API_KEY', '_SECRET'))}
        env['PYTHONPATH'] = str(root/'tests') + os.pathsep + str(root)
        process = subprocess.run([sys.executable, '-B', '-c', script], input=json.dumps(
            dict(path=self.path, binding=asdict(self.binding), now=NOW.isoformat())),
            capture_output=True, text=True, cwd=root, env=env, timeout=5, check=True)
        self.assertEqual(json.loads(process.stdout), dict(accepted=False, execution_allowed=False,
                                                         writes=0, network_attempts=[]))

    def test_retried_incomplete_or_wrong_full_ack_cannot_confirm(self):
        for fault in ('retry', 'missing_binding', 'missing_document', 'wrong_authorization', 'missing_metadata'):
            self.make()
            def corrupt(response, mode=fault):
                response = deepcopy(response)
                if mode == 'retry':
                    response['ResponseMetadata']['RetryAttempts'] = 1
                elif mode == 'missing_binding':
                    response['Attributes'].pop('run_authorization')
                elif mode == 'missing_document':
                    response['Attributes'].pop('document')
                elif mode == 'wrong_authorization':
                    response['Attributes']['run_authorization'] = engine._av(None)
                else:
                    response.pop('ResponseMetadata')
                return response
            self.raw.response_filter = corrupt
            self.assertFalse(self.claim().accepted)
            self.assertIsNotNone(self.current().core.state.worker_claim)
            self.assertEqual(len(self.writes()), 1)
            self.assertFalse(self.claim().accepted)
            self.assertEqual(len(self.writes()), 1)

    def test_stale_readback_cannot_confirm_and_stale_initial_read_cannot_overwrite(self):
        def stale_readback(response):
            return {**response, 'Item': self.seed.item()} if self.current().core.generation > 1 else response
        self.raw.read_filter = stale_readback
        self.assertFalse(self.claim().accepted)
        self.assertIsNotNone(self.current().core.state.worker_claim)
        self.assertFalse(self.claim().accepted)
        self.assertEqual(len(self.writes()), 2)  # Second condition is rejected by the authoritative row.
        self.assertEqual(self.current().core.generation, 2)

    def test_write_errors_never_automatically_retry_or_report_success(self):
        for code in ('ConditionalCheckFailedException', 'AccessDeniedException', 'ResourceNotFoundException',
                     'InternalServerError', 'ProvisionedThroughputExceededException'):
            self.make()
            self.raw.before_update = lambda _request, error=code: (_ for _ in ()).throw(ModelError(error))
            result = self.claim()
            self.assertFalse(result.accepted)
            self.assertTrue(result.requires_reconciliation)
            self.assertEqual(len(self.writes()), 1)
            self.assertEqual(self.current(), self.seed)

    def test_no_effect_timeout_cannot_prove_a_global_cross_restart_attempt_barrier(self):
        # Deliberate limitation/counterexample. There is no second ledger or
        # local replay flag pretending to remember a write that left no effect.
        def no_commit(_request):
            raise TimeoutError('synthetic precommit timeout')
        self.raw.before_update = no_commit
        first = self.claim()
        self.assertFalse(first.accepted)
        self.assertFalse(first.retry_allowed)
        self.assertTrue(first.requires_reconciliation)
        self.assertEqual(len(self.writes()), 1)
        restarted = command.OfflineCommandGateway(transport=self.raw, binding=self.binding,
            authentication=self.auth, clock=lambda: NOW)
        second = self.claim(gateway=restarted)  # Explicit adversarial resubmission, NOT an internal retry.
        self.assertFalse(second.accepted)
        self.assertFalse(second.retry_allowed)
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual(self.current(), self.seed)
        # Dropping the fault allows a deliberately submitted third invocation
        # to commit. The server cannot infer absent history from the unchanged
        # item. This is why the full operational retry contract is NOT proven.
        self.raw.before_update = None
        third = self.claim(gateway=restarted)
        self.assertTrue(third.accepted)
        self.assertFalse(third.execution_allowed)
        self.assertEqual(self.current().core.generation, 2)

    def test_worker_raw_database_access_is_denied_by_synthetic_boundary_only(self):
        untrusted = SyntheticDynamoTransport(self.path, principal='worker')
        with self.assertRaises(ModelError):
            untrusted.update_item(UpdateExpression='SET generation = :zero',
                                  ExpressionAttributeValues={':zero': {'N': '0'}})
        self.assertEqual(self.current(), self.seed)
        self.assertTrue(self.claim().accepted)

    def test_client_or_stored_authority_changes_are_rejected(self):
        for name, value in [('incarnation', {'S': 'b'*32}), ('pk', {'S': 'other'}),
                            ('generation', {'N': '0'}), ('command_schema', {'S': 'unknown'})]:
            self.make()
            item = self.seed.item()
            item[name] = value
            self.raw.admin_replace(item)
            self.assertFalse(self.claim().accepted)
            self.assertEqual(self.writes(), [])
        self.make()
        other_binding = replace(self.binding, authority_id='offline://other-authority')
        other_gateway = command.OfflineCommandGateway(transport=self.raw, binding=other_binding,
            authentication=self.auth, clock=lambda: NOW)
        self.assertFalse(self.claim(gateway=other_gateway).accepted)

    def test_restore_under_new_incarnation_is_denied_and_no_automatic_cutover_exists(self):
        self.assertTrue(self.claim().accepted)
        old_backup = self.seed.item()
        self.raw.admin_replace(old_backup)
        new_binding = replace(self.binding, incarnation='b'*32)
        new_auth = SyntheticCommandAuthentication(new_binding, keys=(), now=NOW)
        new_context = new_auth.operator_invocation()
        new_gateway = command.OfflineCommandGateway(transport=self.raw, binding=new_binding,
            authentication=new_auth, clock=lambda: NOW)
        self.assertFalse(new_gateway.handle('read_control', {}, invocation=new_context).accepted)
        self.assertFalse(new_gateway.handle('restore', {}, invocation=new_context).accepted)

    def test_privileged_restore_of_binding_and_item_is_an_explicit_counterexample(self):
        self.assertTrue(self.claim().accepted)
        self.raw.admin_replace(self.seed.item())  # Outside the trusted-writer model.
        self.assertTrue(self.claim().accepted)  # Local Python cannot detect a full privileged rollback.
        self.assertFalse(self.claim().execution_allowed)
        self.assertEqual(self.current().core.state.control.daily_quota, self.seed.core.state.control.daily_quota)

    def test_existing_v3_and_engine_cannot_silently_accept_or_migrate_draft(self):
        with self.assertRaises(engine.StoreUnavailable):
            engine._snapshot(self.seed.item(), self.binding)
        with self.assertRaises(engine.StoreUnavailable):
            command.CommandSnapshotDraft.decode(self.seed.core.item(), self.binding)
        with self.assertRaises(ValueError):
            v3.OfflineControlStateV3.decode(json.dumps(self.seed.view()))

    def test_expiry_and_clock_rollover_never_release_owner_or_budget(self):
        self.assertTrue(self.claim().accepted)
        owned = self.current()
        gateway = command.OfflineCommandGateway(transport=self.raw, binding=self.binding,
            authentication=self.auth, clock=lambda: NOW+timedelta(days=1))
        self.assertFalse(self.claim(gateway=gateway).accepted)
        self.assertEqual(self.current(), owned)

    def test_no_command_can_enable_execution_or_change_plan_and_production_anchors(self):
        self.assertIs(command.PRODUCTION_COMMAND_GATEWAY, None)
        self.assertIs(v3.CONTROLLER_STORE, None)
        self.assertIs(identity.CONTROLLER_VERIFIER, None)
        self.assertTrue(self.claim().accepted)
        self.assertFalse(self.claim().execution_allowed)
        config = worker_configuration.WORKER_CONFIG
        self.assertEqual(config.research_reports_per_question, 2)
        self.assertEqual(config.predictions_per_research_report, 3)
        self.assertEqual(config.agent_requests_per_atomic_question, 4)
        self.assertEqual(MAX_AUTO_AGENT_POST_ATTEMPTS, 2)
        self.assertFalse(command.CommandResult(True, 'synthetic', control={'execution_allowed': True}).execution_allowed)
        with self.assertRaises(AttributeError):
            command.CommandResult(True, 'synthetic').execution_allowed = True


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CommandGatewayOfflineTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print('Unexpected network attempts:', NETWORK_ATTEMPTS)
    raise SystemExit(0 if result.wasSuccessful() and not NETWORK_ATTEMPTS else 1)

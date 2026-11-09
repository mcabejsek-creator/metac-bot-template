"""Synthetic SQLite whole-state CAS and ephemeral signed OIDC tokens only.

Standalone: python -B tests/test_worker_claim_offline.py
Also discoverable by the existing tests.run when its SDK dependencies exist.
No GitHub store, live journal, worker, network request or paid operation.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Barrier, Lock
import unittest
from unittest.mock import patch


NETWORK_ATTEMPTS = []


def reject_network(event, args):
    if event in {
        'socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname',
        'socket.gethostbyaddr', 'socket.sendto', 'socket.bind',
        'http.client.connect', 'http.client.send',
    }:
        NETWORK_ATTEMPTS.append(event)
        raise RuntimeError('Network forbidden in offline claim tests')


if __name__ == '__main__':
    sys.addaudithook(reject_network)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch_intent_controller as v2
import worker_claim_offline as claim
import worker_identity as identity
import worker_configuration
from autonomous_orchestrator import IndependentReadings, evaluate_offline_worker_handoff
from perplexity_run_budget import MAX_AUTO_AGENT_POST_ATTEMPTS
from scheduler_policy import DailySpendEvidence


NOW = datetime(2026, 11, 9, 12, 30, tzinfo=timezone.utc)
REQUEST_A, REQUEST_B = '1' * 32, '2' * 32


def reserved(when=NOW):
    stamp = v2._iso(when)
    return v2.ControllerState(
        enabled=True, approval_epoch=3, last_intent_at=stamp, last_slot=stamp,
        pending=v2.PendingIntent(v2._intent_id(3, stamp), stamp, stamp),
        daily_quota=v2.DailyQuota(when.date().isoformat(), 1, '0.40'),
    )


class SQLiteControlStore:
    """TEST-ONLY authority: ONE full V3 row, fresh revisions, independent connections.

    SQL UPDATE ... WHERE revision is the CAS linearization point. Commit finishes
    before True returns. The Python lock protects diagnostics, not admission.
    This is not a production backend or a ledger alongside the real V2 store.
    """

    def __init__(self, path):
        self.path = path
        self.reads = self.cas_calls = 0
        self.counter_lock = Lock()

    @classmethod
    def seed(cls, path, state):
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute("CREATE TABLE control (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL, generation INTEGER NOT NULL CHECK(typeof(generation)='integer' AND generation>=1))")
            connection.execute('INSERT INTO control VALUES (1, ?, 1)', (state.encode(),))
        return cls(path)

    def read(self):
        with self.counter_lock:
            self.reads += 1
        with closing(sqlite3.connect(self.path, timeout=2)) as connection, connection:
            row = connection.execute('SELECT payload, generation FROM control WHERE id=1').fetchone()
        if row is None:
            raise RuntimeError('Synthetic control document missing')
        return row[0], 'opaque:' + str(row[1])  # Client treats this as opaque.

    def compare_and_swap(self, revision, payload):
        with self.counter_lock:
            self.cas_calls += 1
        with closing(sqlite3.connect(self.path, timeout=2)) as connection, connection:
            updated = connection.execute(
                "UPDATE control SET payload=?, generation=generation+1 WHERE id=1 AND ('opaque:' || generation)=?",
                (payload, revision),
            ).rowcount
        return updated == 1  # context manager has committed before acknowledgment


class SimulatedProcessCrash(BaseException):
    pass


class WorkerClaimOfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        cls.jwt = jwt
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.bad_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = cls.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        cls.policy = identity.ControllerOIDCPolicy(
            audience='urn:synthetic:claim', subject='repo:synthetic-owner/repo:ref:refs/heads/test',
            repository='synthetic-owner/repo', repository_id='12345', repository_owner_id='67890',
            source_sha='a' * 40, workflow_sha='b' * 40, run_id='999001',
            workflow_ref='synthetic-owner/repo/.github/workflows/worker.yaml@refs/heads/test',
        )
        cls.verifier = identity.GitHubActionsOIDCVerifier(cls.policy, (('test-kid', pem),))

    def token(self, *, key=None, **changes):
        stamp = int(NOW.timestamp())
        claims = dict(
            iss=identity.GITHUB_ISSUER, aud=self.policy.audience, sub=self.policy.subject,
            iat=stamp - 30, nbf=stamp - 30, exp=stamp + 120,
            repository=self.policy.repository, repository_id=self.policy.repository_id,
            repository_owner_id=self.policy.repository_owner_id, sha=self.policy.source_sha,
            workflow_ref=self.policy.workflow_ref, workflow_sha=self.policy.workflow_sha,
            run_id=self.policy.run_id, run_attempt='1',
        )
        return self.jwt.encode({**claims, **changes}, self.key if key is None else key,
                               algorithm='RS256', headers={'kid': 'test-kid'})

    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix='vrchcab-synthetic-claim-'))
        self.path = str(Path(directory) / 'control.sqlite')
        self.initial = claim.OfflineControlStateV3(reserved())
        self.store = SQLiteControlStore.seed(self.path, self.initial)
        self.enterContext(patch.object(claim, 'CONTROLLER_STORE', self.store))
        self.enterContext(patch.object(identity, 'CONTROLLER_VERIFIER', self.verifier))
        self.arguments = dict(intent_id=self.initial.control.pending.intent_id,
                              approval_epoch=3, request_id=REQUEST_A, worker_oidc_token=self.token(), now=NOW)

    def attempt(self, **changes):
        result = claim.claim_reserved_intent_offline(**{**self.arguments, **changes})
        self.assertIs(result.execution_allowed, False)
        return result

    def state(self):
        return claim.OfflineControlStateV3.decode(SQLiteControlStore(self.path).read()[0])

    def admin_write(self, state):
        # Simulate a distinct controller using the SAME authoritative CAS row.
        peer = SQLiteControlStore(self.path)
        _, revision = peer.read()
        self.assertTrue(peer.compare_and_swap(revision, state.encode()))

    def test_v3_roundtrip_is_complete_and_v2_migration_is_not_automatic(self):
        self.assertEqual(claim.OfflineControlStateV3.decode(self.initial.encode()), self.initial)
        document = json.loads(self.initial.encode())
        self.assertEqual(set(document), v2.STATE_FIELDS | {'worker_claim'})
        self.assertEqual(document['version'], 3)
        self.assertEqual(v2.VERSION, 2)
        with self.assertRaises(v2.InvalidControlState):
            claim.OfflineControlStateV3.decode(self.initial.control.encode())
        with self.assertRaises(v2.InvalidControlState):
            v2.ControllerState.decode(self.initial.encode())
        self.assertTrue(self.attempt().claim_confirmed)
        self.assertEqual(claim.OfflineControlStateV3.decode(self.state().encode()), self.state())

    def test_invalid_state_schema_claim_and_owner_binding_fail_closed(self):
        self.assertTrue(self.attempt().claim_confirmed)
        good = json.loads(self.state().encode())
        mutations = [dict(good, version=2), dict(good, enabled=1), dict(good, unknown=True),
                     dict(good, worker_claim=True), dict(good, pending=None)]
        for field, value in (('intent_id', 'f' * 64), ('approval_epoch', True),
                             ('request_id', 'bad'), ('claimed_at', '2026-11-09T12:29:59Z'),
                             ('owner', {'verified': True})):
            mutations.append({**good, 'worker_claim': {**good['worker_claim'], field: value}})
        for data in mutations:
            with self.subTest(fields=tuple(data)), self.assertRaises(v2.InvalidControlState):
                claim.OfflineControlStateV3.decode(json.dumps(data))
        for raw in ('[]', '{', 'x' * 8193, b'\xff', self.initial.encode().replace('"version":3', '"version":3,"version":3')):
            with self.assertRaises(v2.InvalidControlState):
                claim.OfflineControlStateV3.decode(raw)

    def test_missing_store_and_local_trusted_flags_cannot_confirm(self):
        for untrusted in (None, object(), {'trusted': True}):
            with self.subTest(kind=type(untrusted).__name__), patch.object(claim, 'CONTROLLER_STORE', untrusted):
                self.assertFalse(self.attempt().claim_confirmed)
        self.assertEqual(self.store.cas_calls, 0)
        with self.assertRaises(TypeError):
            claim.claim_reserved_intent_offline(**self.arguments, store=self.store)

    def test_actual_v2_payload_is_rejected_without_rewrite(self):
        original_read = self.store.read
        with patch.object(self.store, 'read', side_effect=lambda: (self.initial.control.encode(), original_read()[1])):
            result = self.attempt()
        self.assertFalse(result.claim_confirmed)
        self.assertTrue(result.requires_reconciliation)
        self.assertEqual(self.store.cas_calls, 0)
        self.assertEqual(self.state(), self.initial)

    def test_signed_token_cannot_replace_missing_production_oidc_trust(self):
        with patch.object(identity, 'CONTROLLER_VERIFIER', None):
            self.assertFalse(self.attempt().claim_confirmed)
        self.assertEqual(self.store.cas_calls, 0)

    def test_forged_claims_flags_tokens_and_environment_cannot_authorize(self):
        with patch.dict(os.environ, {'GITHUB_SHA': self.policy.source_sha, 'GITHUB_RUN_ID': self.policy.run_id,
                                    'GITHUB_RUN_ATTEMPT': '1', 'WORKER_IDENTITY_VERIFIED': 'true'}):
            for token in ({'verified': True}, {'run_id': self.policy.run_id}, True, None,
                          'bad', self.token(key=self.bad_key), self.token(run_attempt='2'),
                          self.token(sha='c' * 40), self.token(aud='other'),
                          self.verifier.verify(self.token(), now=NOW)):
                with self.subTest(kind=type(token).__name__):
                    self.assertFalse(self.attempt(worker_oidc_token=token).claim_confirmed)
        self.assertEqual(self.store.cas_calls, 0)
        for extra in ({'verified': True}, {'owner': {'run_id': '999001'}}, {'source_sha': 'a' * 40}):
            with self.assertRaises(TypeError):
                claim.claim_reserved_intent_offline(**self.arguments, **extra)

    def test_wrong_intent_epoch_or_disabled_activation_never_writes(self):
        for changes in ({'intent_id': 'f' * 64}, {'approval_epoch': 4}, {'approval_epoch': True}):
            with self.subTest(changes=changes):
                self.assertFalse(self.attempt(**changes).claim_confirmed)
        self.admin_write(replace(self.initial, control=replace(self.initial.control, enabled=False)))
        self.assertEqual(self.attempt().reason, 'persistent_kill_switch_disabled')
        self.assertEqual(self.store.cas_calls, 0)

    def test_missing_intent_invalid_request_clock_or_revision_never_writes(self):
        for changes in ({'intent_id': None}, {'request_id': ''}, {'request_id': True},
                        {'now': NOW.replace(tzinfo=None)}, {'now': NOW + timedelta(microseconds=1)},
                        {'now': NOW - timedelta(seconds=1)}, {'now': NOW + timedelta(minutes=6)}):
            with self.subTest(changes=changes):
                self.assertFalse(self.attempt(**changes).claim_confirmed)
        for revision in (None, '', 1, True):
            with patch.object(self.store, 'read', return_value=(self.initial.encode(), revision)):
                self.assertFalse(self.attempt().claim_confirmed)
        no_pending = replace(self.initial, control=replace(self.initial.control, pending=None))
        self.admin_write(no_pending)
        self.assertFalse(self.attempt().claim_confirmed)
        self.assertEqual(self.store.cas_calls, 0)

    def test_local_success_flag_without_persisted_owner_cannot_confirm(self):
        with patch.object(self.store, 'compare_and_swap', return_value=True) as write:
            result = self.attempt()
        self.assertFalse(result.claim_confirmed)
        self.assertTrue(result.requires_reconciliation)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(self.state(), self.initial)

    def test_success_records_only_owner_and_keeps_existing_reserve(self):
        result = self.attempt()
        self.assertTrue(result.claim_confirmed)
        self.assertFalse(result.requires_reconciliation)
        self.assertEqual(self.store.cas_calls, 1)
        self.assertEqual(self.store.reads, 2)
        stored = self.state()
        self.assertEqual(stored.control, self.initial.control)
        self.assertEqual(stored.worker_claim.request_id, REQUEST_A)
        self.assertEqual(stored.worker_claim.owner.run_id, self.policy.run_id)
        self.assertEqual(stored.worker_claim.owner.source_sha, self.policy.source_sha)
        self.assertNotIn(self.arguments['worker_oidc_token'], stored.encode())

    def test_two_contending_workers_with_same_revision_have_one_confirmed_owner(self):
        barrier = Barrier(2)
        real_cas = self.store.compare_and_swap

        def competing_cas(revision, payload):
            barrier.wait(timeout=3)
            return real_cas(revision, payload)

        with patch.object(self.store, 'compare_and_swap', side_effect=competing_cas):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda request: self.attempt(request_id=request), (REQUEST_A, REQUEST_B)))
        self.assertEqual(sum(r.claim_confirmed for r in results), 1)
        self.assertEqual(sum(r.requires_reconciliation for r in results), 1)
        winner = (REQUEST_A, REQUEST_B)[next(i for i, r in enumerate(results) if r.claim_confirmed)]
        self.assertEqual(self.state().worker_claim.request_id, winner)
        self.assertEqual(self.state().control, self.initial.control)
        self.assertEqual(self.store.cas_calls, 2)

    def test_same_request_or_other_request_cannot_reconfirm_or_charge_twice(self):
        self.assertTrue(self.attempt().claim_confirmed)
        for request_id in (REQUEST_A, REQUEST_B):
            result = self.attempt(request_id=request_id)
            self.assertFalse(result.claim_confirmed)
            self.assertTrue(result.requires_reconciliation)
        self.assertEqual(self.store.cas_calls, 1)
        self.assertEqual(self.state().control.daily_quota, self.initial.control.daily_quota)

    def test_restart_uses_durable_owner_and_cannot_issue_another_confirmation(self):
        self.assertTrue(self.attempt().claim_confirmed)
        reopened = SQLiteControlStore(self.path)
        with patch.object(claim, 'CONTROLLER_STORE', reopened):
            self.assertFalse(self.attempt().claim_confirmed)
            self.assertFalse(self.attempt(request_id=REQUEST_B).claim_confirmed)
        self.assertEqual(reopened.cas_calls, 0)

    def test_fresh_python_process_observes_persistent_owner_and_does_not_retry(self):
        self.assertTrue(self.attempt().claim_confirmed)
        root = Path(__file__).resolve().parents[1]
        script = '''
import json, sys
from dataclasses import asdict
from datetime import datetime
from test_worker_claim_offline import SQLiteControlStore, reject_network, NETWORK_ATTEMPTS
sys.addaudithook(reject_network)
import worker_identity as identity
import worker_claim_offline as claim
data = json.load(sys.stdin)
identity.CONTROLLER_VERIFIER = identity.GitHubActionsOIDCVerifier(
    identity.ControllerOIDCPolicy(**data['policy']), (('test-kid', data['pem'].encode('ascii')),))
claim.CONTROLLER_STORE = SQLiteControlStore(data['path'])
args = data['arguments']
args['now'] = datetime.fromisoformat(args['now'])
result = claim.claim_reserved_intent_offline(**args)
print(json.dumps(dict(result=asdict(result), execution_allowed=result.execution_allowed,
                     cas_calls=claim.CONTROLLER_STORE.cas_calls, network_attempts=NETWORK_ATTEMPTS)))
'''
        data = dict(policy=asdict(self.policy), pem=self.verifier.trusted_public_keys[0][1].decode('ascii'),
                    path=self.path, arguments={**self.arguments, 'now': NOW.isoformat()})
        env = {name: value for name, value in os.environ.items()
               if not name.endswith(('_TOKEN', '_API_KEY', '_SECRET'))}
        env['PYTHONPATH'] = str(root / 'tests') + os.pathsep + str(root)
        restarted = subprocess.run([sys.executable, '-B', '-c', script], input=json.dumps(data),
                                   text=True, capture_output=True, check=True, timeout=5, cwd=root, env=env)
        observed = json.loads(restarted.stdout)
        self.assertFalse(observed['result']['claim_confirmed'])
        self.assertTrue(observed['result']['requires_reconciliation'])
        self.assertFalse(observed['execution_allowed'])
        self.assertEqual(observed['cas_calls'], 0)
        self.assertEqual(observed['network_attempts'], [])
        self.assertEqual(self.state().worker_claim.request_id, REQUEST_A)

    def test_revision_conflict_performs_one_attempt_and_no_reconciliation_write(self):
        real_cas = self.store.compare_and_swap

        def conflict(revision, payload):
            self.admin_write(self.initial)  # Same bytes, NEW opaque generation.
            return real_cas(revision, payload)

        with patch.object(self.store, 'compare_and_swap', side_effect=conflict):
            result = self.attempt()
        self.assertFalse(result.claim_confirmed)
        self.assertTrue(result.requires_reconciliation)
        self.assertEqual(self.store.cas_calls, 1)
        self.assertEqual(self.state(), self.initial)

    def test_changed_intent_cannot_be_overwritten_by_stale_claim(self):
        real_cas = self.store.compare_and_swap
        other = claim.OfflineControlStateV3(reserved(NOW + timedelta(minutes=15)))

        def change_intent(revision, payload):
            self.admin_write(other)
            return real_cas(revision, payload)

        with patch.object(self.store, 'compare_and_swap', side_effect=change_intent):
            result = self.attempt()
        self.assertFalse(result.claim_confirmed)
        self.assertTrue(result.requires_reconciliation)
        self.assertEqual(self.state(), other)
        self.assertEqual(self.store.cas_calls, 1)

    def test_timeout_after_possible_commit_stops_and_restart_keeps_owner(self):
        real_cas = self.store.compare_and_swap

        def timeout(revision, payload):
            self.assertTrue(real_cas(revision, payload))
            raise TimeoutError('synthetic lost response')

        with patch.object(self.store, 'compare_and_swap', side_effect=timeout):
            result = self.attempt()
        self.assertFalse(result.claim_confirmed)
        self.assertTrue(result.requires_reconciliation)
        self.assertIsNotNone(self.state().worker_claim)
        self.assertFalse(self.attempt(request_id=REQUEST_B).claim_confirmed)
        self.assertEqual(self.store.cas_calls, 1)

    def test_lost_acknowledgment_or_non_boolean_result_never_confirms(self):
        real_cas = self.store.compare_and_swap

        def lost_ack(revision, payload):
            self.assertTrue(real_cas(revision, payload))
            return None

        with patch.object(self.store, 'compare_and_swap', side_effect=lost_ack):
            result = self.attempt()
        self.assertFalse(result.claim_confirmed)
        self.assertTrue(result.requires_reconciliation)
        self.assertFalse(self.attempt().claim_confirmed)
        self.assertEqual(self.store.cas_calls, 1)

    def test_timeout_before_commit_is_uncertain_not_a_retry_loop(self):
        with patch.object(self.store, 'compare_and_swap', side_effect=TimeoutError('synthetic')) as write:
            result = self.attempt()
        self.assertFalse(result.claim_confirmed)
        self.assertTrue(result.requires_reconciliation)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(self.state(), self.initial)  # Observation is not retry authority.

    def test_process_interruption_after_commit_cannot_release_or_replay_owner(self):
        real_cas = self.store.compare_and_swap

        def crash(revision, payload):
            self.assertTrue(real_cas(revision, payload))
            raise SimulatedProcessCrash()

        with patch.object(self.store, 'compare_and_swap', side_effect=crash):
            with self.assertRaises(SimulatedProcessCrash):
                self.attempt()
        with patch.object(claim, 'CONTROLLER_STORE', SQLiteControlStore(self.path)):
            self.assertFalse(self.attempt().claim_confirmed)
        self.assertEqual(self.store.cas_calls, 1)

    def test_readback_failure_cannot_confirm_despite_stored_owner(self):
        real_read = self.store.read
        reads = 0

        def read():
            nonlocal reads
            reads += 1
            if reads > 1:
                raise OSError('synthetic readback unavailable')
            return real_read()

        with patch.object(self.store, 'read', side_effect=read):
            result = self.attempt()
        self.assertFalse(result.claim_confirmed)
        self.assertTrue(result.requires_reconciliation)
        self.assertIsNotNone(self.state().worker_claim)
        self.assertEqual(self.store.cas_calls, 1)

    def test_unknown_owner_in_readback_requires_reconciliation_without_overwrite(self):
        real_cas = self.store.compare_and_swap

        def unknown_owner(revision, payload):
            self.assertTrue(real_cas(revision, payload))
            stored = self.state()
            different = replace(stored.worker_claim, owner=replace(stored.worker_claim.owner, run_id='999002'))
            self.admin_write(replace(stored, worker_claim=different))
            return True

        with patch.object(self.store, 'compare_and_swap', side_effect=unknown_owner):
            result = self.attempt()
        self.assertFalse(result.claim_confirmed)
        self.assertTrue(result.requires_reconciliation)
        self.assertEqual(self.state().worker_claim.owner.run_id, '999002')
        self.assertFalse(self.attempt().claim_confirmed)
        self.assertEqual(self.store.cas_calls, 1)

    def test_emergency_off_racing_before_claim_cas_wins_without_owner(self):
        real_cas = self.store.compare_and_swap

        def stop_before(revision, payload):
            self.admin_write(replace(self.initial, control=replace(self.initial.control, enabled=False)))
            return real_cas(revision, payload)

        with patch.object(self.store, 'compare_and_swap', side_effect=stop_before):
            result = self.attempt()
        self.assertFalse(result.claim_confirmed)
        self.assertFalse(self.state().control.enabled)
        self.assertIsNone(self.state().worker_claim)

    def test_emergency_off_after_commit_before_readback_denies_confirmation(self):
        real_cas = self.store.compare_and_swap

        def stop_after(revision, payload):
            self.assertTrue(real_cas(revision, payload))
            stored = self.state()
            self.admin_write(replace(stored, control=replace(stored.control, enabled=False)))
            return True

        with patch.object(self.store, 'compare_and_swap', side_effect=stop_after):
            result = self.attempt()
        self.assertFalse(result.claim_confirmed)
        self.assertTrue(result.requires_reconciliation)
        self.assertFalse(self.state().control.enabled)
        self.assertIsNotNone(self.state().worker_claim)
        self.assertEqual(self.state().control.daily_quota, self.initial.control.daily_quota)

    def test_stop_after_confirmed_claim_is_durable_and_preserves_owner_and_quota(self):
        self.assertTrue(self.attempt().claim_confirmed)
        owned = self.state()
        stopped = claim.persist_offline_emergency_stop(operator_stop_authorized=True)
        self.assertTrue(stopped.stopped)
        observed = self.state()
        self.assertFalse(observed.control.enabled)
        self.assertEqual(observed.worker_claim, owned.worker_claim)
        self.assertEqual(observed.control.daily_quota, owned.control.daily_quota)
        self.assertFalse(self.attempt(request_id=REQUEST_B).claim_confirmed)
        self.admin_write(replace(observed, control=replace(observed.control, enabled=True)))
        self.assertFalse(self.attempt(request_id=REQUEST_B).claim_confirmed)
        self.assertEqual(self.state().worker_claim, owned.worker_claim)

    def test_stop_requires_authorization_and_uncertain_stop_never_reports_success(self):
        self.assertFalse(claim.persist_offline_emergency_stop().stopped)
        self.assertEqual(self.store.reads, 0)
        real_cas = self.store.compare_and_swap

        def timeout(revision, payload):
            self.assertTrue(real_cas(revision, payload))
            raise TimeoutError('synthetic stop acknowledgment lost')

        with patch.object(self.store, 'compare_and_swap', side_effect=timeout):
            stopped = claim.persist_offline_emergency_stop(operator_stop_authorized=True)
        self.assertFalse(stopped.stopped)
        self.assertTrue(stopped.requires_reconciliation)
        self.assertEqual(self.store.cas_calls, 1)
        self.assertFalse(self.state().control.enabled)

    def test_off_on_aba_does_not_restore_the_old_revision(self):
        real_cas = self.store.compare_and_swap

        def aba(revision, payload):
            self.admin_write(replace(self.initial, control=replace(self.initial.control, enabled=False)))
            self.admin_write(self.initial)
            return real_cas(revision, payload)

        with patch.object(self.store, 'compare_and_swap', side_effect=aba):
            result = self.attempt()
        self.assertFalse(result.claim_confirmed)
        self.assertTrue(result.requires_reconciliation)
        self.assertEqual(self.state(), self.initial)
        self.assertEqual(self.store.cas_calls, 1)

    def test_owner_never_expires_or_is_taken_over_when_token_or_clock_expires(self):
        self.assertTrue(self.attempt().claim_confirmed)
        owned = self.state()
        self.assertFalse(self.attempt(request_id=REQUEST_B, now=NOW + timedelta(days=3650)).claim_confirmed)
        self.assertEqual(self.state(), owned)
        self.assertEqual(self.store.cas_calls, 1)

    def test_confirmed_claim_never_runs_worker_and_real_research_limits_still_block(self):
        result = self.attempt()
        self.assertTrue(result.claim_confirmed)
        self.assertIs(result.execution_allowed, False)
        config = worker_configuration.WORKER_CONFIG
        self.assertEqual((config.research_reports_per_question, config.predictions_per_research_report), (2, 3))
        self.assertEqual(config.agent_requests_per_atomic_question, 4)
        self.assertEqual(MAX_AUTO_AGENT_POST_ATTEMPTS, 2)
        stored = self.state()

        class V2ReadOnlyView:
            def read(self):
                return stored.control, 'synthetic-view'

        denied = evaluate_offline_worker_handoff(
            V2ReadOnlyView(), object(), now=NOW, intent_id=self.arguments['intent_id'],
            approval_epoch=3, activation_confirmed=True, worker_oidc_token=self.token())
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, 'worker_research_plan_exceeds_automatic_attempt_cap')
        readings = IndependentReadings(
            github_checked_at=NOW, active_publishers=0, queued_publishers=0,
            funded_openrouter_checked_at=NOW, funded_openrouter_ok=True,
            perplexity_checked_at=NOW, perplexity_spend_ok=True,
            daily_spend=DailySpendEvidence(NOW.date(), NOW, '0.10', '1.00', '0.40', 1, 1, True),
            journal_checked_at=NOW, prior_publication_certain=True,
            operator_approval_confirmed=True, automation_switch_confirmed=True, is_first_attempt=True)

        class Source:
            def read(self):
                return readings

        with patch.object(worker_configuration, 'WORKER_CONFIG', replace(config, research_reports_per_question=1)):
            denied = evaluate_offline_worker_handoff(
                V2ReadOnlyView(), Source(), now=NOW, intent_id=self.arguments['intent_id'],
                approval_epoch=3, activation_confirmed=True, worker_oidc_token=self.token())
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, 'worker_execution_identity_unverified')
        self.assertEqual(self.state(), stored)


if __name__ == '__main__':
    import importlib.metadata
    versions = {name: importlib.metadata.version(name) for name in ('PyJWT', 'cryptography')}
    print('Crypto versions:', versions, flush=True)
    if versions != {'PyJWT': '2.10.1', 'cryptography': '45.0.4'}:
        print('Locked crypto compatibility NOT VERIFIED; exploratory crypto run only.', flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(WorkerClaimOfflineTests))
    print(f'Offline audit guard: {len(NETWORK_ATTEMPTS)} network attempts.')
    sys.exit(0 if result.wasSuccessful() and not NETWORK_ATTEMPTS else 1)

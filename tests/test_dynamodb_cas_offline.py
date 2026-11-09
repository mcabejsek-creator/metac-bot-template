"""Stdlib-only model tests; no AWS SDK, credentials, network or live authority."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __package__:
    from .fake_dynamodb_control import SyntheticDynamoTransport, ModelError
    from .test_github_contents_cas_offline import candidate
    from .test_worker_claim_offline import reserved, NOW
    from .test_autonomous_runtime_offline import reject_network, NETWORK_ATTEMPTS
else:
    from fake_dynamodb_control import SyntheticDynamoTransport, ModelError
    from test_github_contents_cas_offline import candidate
    from test_worker_claim_offline import reserved, NOW
    from test_autonomous_runtime_offline import reject_network, NETWORK_ATTEMPTS

if __name__ == '__main__':
    sys.addaudithook(reject_network)

import dynamodb_cas_offline as engine
from dispatch_intent_controller import disable_controller
import worker_claim_offline as v3
import worker_identity
import worker_configuration
from perplexity_run_budget import MAX_AUTO_AGENT_POST_ATTEMPTS


class ProcessInterrupted(BaseException):
    pass


class DynamoConditionalPrototypeTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory(prefix='synthetic-dynamo-'))
        self.serial = 0
        self.binding = engine.AuthorityBinding('offline-control', 'controller', 'a' * 32,
                                               'offline://account/region/table')
        self.initial = v3.OfflineControlStateV3(reserved())
        self.make()

    def make(self, *, state=None, generation=1):
        self.serial += 1
        self.path = str(Path(self.directory) / f'control-{self.serial}.sqlite')
        self.seed = engine.Snapshot(self.initial if state is None else state, generation, self.binding)
        self.transport = SyntheticDynamoTransport.seed(self.path, self.seed.item())
        self.store = engine.DynamoConditionalStorePrototype(transport=self.transport, binding=self.binding)

    def claim(self, request='1'):
        result = self.store.compare_and_swap(self.seed.revision, candidate(self.seed.state, request))
        self.assertFalse(result.execution_allowed)
        return result

    def calls(self, method):
        return [request for name, request in self.transport.calls if name == method]

    def test_transport_and_explicit_binding_required_without_any_default_client(self):
        for transport in (None, object(), {'verified': True}):
            with self.assertRaises(engine.StoreUnavailable):
                engine.DynamoConditionalStorePrototype(transport=transport, binding=self.binding)
        with self.assertRaises(engine.StoreUnavailable):
            replace(self.binding, table_name='production-table')
        with self.assertRaises(TypeError):
            engine.DynamoConditionalStorePrototype(transport=self.transport, binding=self.binding, verified=True)

    def test_read_is_strong_and_returns_entire_authoritative_snapshot(self):
        snapshot = self.store.read()
        self.assertEqual(snapshot, self.seed)
        self.assertEqual(self.calls('GetItem'), [dict(TableName='offline-control',
                                                    Key={'pk': {'S': 'controller'}}, ConsistentRead=True)])
        self.assertEqual(self.calls('UpdateItem'), [])

    def test_missing_item_unavailable_reads_and_bad_metadata_never_initialize(self):
        for response in ({}, {'ResponseMetadata': {'HTTPStatusCode': 200, 'RetryAttempts': 0}},
                         {'ResponseMetadata': {'HTTPStatusCode': 200, 'RetryAttempts': 1}, 'Item': self.seed.item()},
                         {'ResponseMetadata': {'HTTPStatusCode': 500, 'RetryAttempts': 0}}):
            self.transport.read_filter = lambda _response, value=response: value
            with self.assertRaises(engine.StoreUnavailable):
                self.store.read()
        self.assertEqual(self.calls('UpdateItem'), [])

    def test_invalid_types_sizes_or_incarnations_are_not_authority(self):
        for field, value in (('generation', {'N': '0'}), ('generation', {'N': '1.0'}),
                             ('generation', {'BOOL': True}), ('incarnation', {'S': 'b' * 32}),
                             ('pk', {'S': 'other'}), ('document', {'S': 'not-a-map'})):
            with self.subTest(field=field, value=value):
                item = self.seed.item()
                item[field] = value
                self.transport.admin_replace(item)
                with self.assertRaises(engine.StoreUnavailable):
                    self.store.read()
        oversized = self.seed.item()
        oversized['extra'] = {'S': 'x' * 5000}
        self.transport.admin_replace(oversized)
        with self.assertRaises(engine.StoreUnavailable):
            self.store.read()

    def test_claim_emits_one_conditional_atomic_increment_and_preserves_quota(self):
        result = self.claim()
        self.assertTrue(result.confirmed)
        self.assertEqual(len(self.calls('UpdateItem')), 1)
        self.assertEqual(len(self.calls('GetItem')), 2)
        request = self.calls('UpdateItem')[0]
        self.assertEqual(request['ReturnValues'], 'ALL_NEW')
        self.assertIn('attribute_exists(#pk)', request['ConditionExpression'])
        self.assertIn('#g = :g', request['ConditionExpression'])
        self.assertIn('#d = :before', request['ConditionExpression'])
        self.assertIn('attribute_type(#d.#claim, :null)', request['ConditionExpression'])
        self.assertEqual(request['UpdateExpression'], 'SET #d.#claim = :claim, #g = #g + :one')
        after = self.store.read()
        self.assertEqual(after.generation, 2)
        self.assertEqual(after.state.control, self.initial.control)
        self.assertEqual(after.state.worker_claim.request_id, '1' * 32)

    def test_two_workers_using_same_revision_have_at_most_one_confirmed_write(self):
        barrier = Barrier(2)
        self.transport.before_update = lambda _request: barrier.wait(timeout=3)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.claim, ('1', '2')))
        self.assertEqual(sum(result.confirmed for result in results), 1)
        self.assertEqual(sum(result.requires_reconciliation for result in results), 1)
        self.assertEqual(len(self.calls('UpdateItem')), 2)
        self.assertEqual(self.store.read().generation, 2)

    def test_duplicate_request_and_restart_cannot_reconfirm_or_reserve_again(self):
        self.assertTrue(self.claim().confirmed)
        self.assertFalse(self.claim().confirmed)
        restarted = engine.DynamoConditionalStorePrototype(
            transport=SyntheticDynamoTransport(self.path), binding=self.binding)
        owned = restarted.read()
        self.assertFalse(restarted.compare_and_swap(owned.revision, owned.state).confirmed)
        self.assertFalse(restarted.compare_and_swap(owned.revision, candidate(self.initial, '2')).confirmed)
        self.assertEqual(restarted.read().state.control.daily_quota, self.initial.control.daily_quota)

    def test_restart_in_fresh_python_process_reads_persisted_owner(self):
        self.assertTrue(self.claim().confirmed)
        root = Path(__file__).resolve().parents[1]
        script = '''
import json, sys
from fake_dynamodb_control import SyntheticDynamoTransport
from test_github_contents_cas_offline import candidate
from test_autonomous_runtime_offline import reject_network, NETWORK_ATTEMPTS
sys.addaudithook(reject_network)
import dynamodb_cas_offline as engine
data = json.load(sys.stdin)
store = engine.DynamoConditionalStorePrototype(transport=SyntheticDynamoTransport(data['path']),
                                              binding=engine.AuthorityBinding(**data['binding']))
snapshot = store.read()
result = store.compare_and_swap(snapshot.revision, candidate(snapshot.state, '2'))
print(json.dumps(dict(confirmed=result.confirmed, execution_allowed=result.execution_allowed,
                     network_attempts=NETWORK_ATTEMPTS, generation=store.read().generation)))
'''
        env = {name: value for name, value in os.environ.items() if not name.endswith(('_TOKEN', '_API_KEY', '_SECRET'))}
        env['PYTHONPATH'] = str(root / 'tests') + os.pathsep + str(root)
        process = subprocess.run([sys.executable, '-B', '-c', script], input=json.dumps(
            dict(path=self.path, binding=asdict(self.binding))), text=True, capture_output=True,
            timeout=5, check=True, env=env, cwd=root)
        result = json.loads(process.stdout)
        self.assertEqual(result, dict(confirmed=False, execution_allowed=False, network_attempts=[], generation=2))

    def test_changed_intent_or_emergency_off_between_read_and_update_conflicts(self):
        from datetime import timedelta
        states = [replace(self.initial, control=disable_controller(self.initial.control)),
                  v3.OfflineControlStateV3(reserved(NOW + timedelta(minutes=15)))]
        for changed in states:
            self.make()
            self.transport.before_update = lambda _request, value=changed: self.transport.admin_replace(
                engine.Snapshot(value, 2, self.binding).item())
            result = self.claim()
            self.assertFalse(result.confirmed)
            self.assertTrue(result.requires_reconciliation)
            self.assertEqual(len(self.calls('UpdateItem')), 1)
            self.assertEqual(self.store.read().state, changed)

    def test_full_document_condition_detects_writer_change_even_without_generation_increment(self):
        changed = replace(self.initial, control=replace(self.initial.control,
                          daily_quota=replace(self.initial.control.daily_quota, reserved_usd='0.80')))
        self.transport.before_update = lambda _request: self.transport.admin_replace(
            engine.Snapshot(changed, 1, self.binding).item())
        self.assertFalse(self.claim().confirmed)
        self.assertEqual(self.store.read().state, changed)

    def test_conditional_failure_and_other_errors_never_retry_the_write(self):
        for code in ('ConditionalCheckFailedException', 'AccessDeniedException',
                     'ProvisionedThroughputExceededException', 'ResourceNotFoundException', 'InternalServerError'):
            self.make()
            def fail(_request, value=code):
                raise ModelError(value)
            self.transport.before_update = fail
            result = self.claim()
            self.assertFalse(result.confirmed)
            self.assertTrue(result.requires_reconciliation)
            self.assertEqual(len(self.calls('UpdateItem')), 1)
            self.assertEqual(len(self.calls('GetItem')), 1)
            self.assertEqual(self.transport.current(), self.seed.item())

    def test_timeout_or_connection_loss_after_commit_never_confirms_or_retries(self):
        for error in (TimeoutError, ConnectionResetError):
            self.make()
            def fail(_request, kind=error):
                raise kind('Synthetic lost write response')
            self.transport.after_update = fail
            result = self.claim()
            self.assertFalse(result.confirmed)
            self.assertTrue(result.requires_reconciliation)
            self.assertEqual(len(self.calls('UpdateItem')), 1)
            self.transport.after_update = None
            self.assertIsNotNone(self.store.read().state.worker_claim)
            self.assertFalse(self.claim('2').confirmed)

    def test_process_interruption_after_commit_retains_the_owner(self):
        def interrupt(_request):
            raise ProcessInterrupted()
        self.transport.after_update = interrupt
        with self.assertRaises(ProcessInterrupted):
            self.claim()
        self.transport.after_update = None
        self.assertIsNotNone(self.store.read().state.worker_claim)
        self.assertFalse(self.claim('2').confirmed)
        self.assertEqual(len(self.calls('UpdateItem')), 1)

    def test_incomplete_wrong_or_retried_ack_is_uncertain_even_after_commit(self):
        for fault in ('missing_attributes', 'wrong_generation', 'missing_metadata', 'retried'):
            self.make()
            def response(value, mode=fault):
                value = deepcopy(value)
                if mode == 'missing_attributes':
                    value.pop('Attributes')
                elif mode == 'wrong_generation':
                    value['Attributes']['generation'] = {'N': '100'}
                elif mode == 'missing_metadata':
                    value.pop('ResponseMetadata')
                else:
                    value['ResponseMetadata']['RetryAttempts'] = 1
                return value
            self.transport.response_filter = response
            result = self.claim()
            self.assertFalse(result.confirmed)
            self.assertTrue(result.requires_reconciliation)
            self.assertEqual(len(self.calls('UpdateItem')), 1)
            self.assertIsNotNone(self.store.read().state.worker_claim)

    def test_stale_read_cannot_overwrite_a_later_generation(self):
        old = {**self.transport.metadata(), 'Item': self.seed.item()}
        self.assertTrue(self.claim().confirmed)
        self.transport.read_filter = lambda _response: deepcopy(old)
        result = self.claim('2')
        self.assertFalse(result.confirmed)
        self.assertTrue(result.requires_reconciliation)
        self.transport.read_filter = None
        self.assertEqual(self.store.read().state.worker_claim.request_id, '1' * 32)

    def test_off_after_commit_before_readback_prevents_confirmation_and_preserves_owner(self):
        def stop(_request):
            current = self.transport.current()
            current['document']['M']['enabled'] = {'BOOL': False}
            current['generation'] = {'N': '3'}
            self.transport.admin_replace(current)
        self.transport.after_update = stop
        result = self.claim()
        self.assertFalse(result.confirmed)
        self.assertTrue(result.requires_reconciliation)
        after = self.store.read()
        self.assertFalse(after.state.control.enabled)
        self.assertIsNotNone(after.state.worker_claim)

    def test_emergency_stop_is_monotonic_and_preserves_pending_owner_and_reserve(self):
        self.assertTrue(self.claim().confirmed)
        owned = self.store.read()
        off = replace(owned.state, control=disable_controller(owned.state.control))
        result = self.store.compare_and_swap(owned.revision, off)
        self.assertTrue(result.confirmed)
        stopped = self.store.read()
        self.assertEqual(stopped.generation, 3)
        self.assertEqual(stopped.state.worker_claim, owned.state.worker_claim)
        self.assertEqual(stopped.state.control.daily_quota, owned.state.control.daily_quota)
        self.assertFalse(self.store.compare_and_swap(stopped.revision, owned.state).confirmed)

    def test_aba_with_a_fresh_generation_rejects_old_revision(self):
        off = replace(self.initial, control=disable_controller(self.initial.control))
        self.transport.admin_replace(engine.Snapshot(off, 2, self.binding).item())
        self.transport.admin_replace(engine.Snapshot(self.initial, 3, self.binding).item())
        self.assertFalse(self.claim().confirmed)
        self.assertEqual(len(self.calls('UpdateItem')), 0)

    def test_raw_update_permission_can_bypass_client_cas_and_restore_aba(self):
        self.assertTrue(self.claim().confirmed)
        # Test-only counterexample: a broadly authorized raw writer is NOT
        # constrained by the Python engine. No production IAM claim is made.
        broad = SyntheticDynamoTransport(self.path, principal='broad_update_grant')
        broad.update_item(TableName='offline-control', Key={'pk': {'S': 'controller'}},
                          UpdateExpression='SET #g = :old, #d = :document',
                          ExpressionAttributeNames={'#g': 'generation', '#d': 'document'},
                          ExpressionAttributeValues={':old': {'N': '1'},
                                                     ':document': self.seed.item()['document']},
                          ReturnValues='ALL_NEW')
        self.assertTrue(self.claim('2').confirmed)
        self.assertEqual(self.store.read().state.worker_claim.request_id, '2' * 32)
        self.assertFalse(engine.WriteObservation(True, 'synthetic').execution_allowed)

    def test_worker_principal_without_raw_database_permissions_cannot_bypass_broker(self):
        worker = SyntheticDynamoTransport(self.path, principal='worker')
        with self.assertRaises(ModelError):
            worker.update_item(UpdateExpression='SET generation = :old')
        with self.assertRaises(ModelError):
            worker.get_item(ConsistentRead=True)
        self.assertEqual(self.transport.current(), self.seed.item())

    def test_backup_restore_fence_and_different_authority_never_auto_cut_over(self):
        restored = self.seed.item()
        restored['incarnation'] = {'S': 'b' * 32}
        self.transport.admin_replace(restored)
        with self.assertRaises(engine.StoreUnavailable):
            self.store.read()
        self.transport.admin_replace(self.seed.item())
        other = engine.DynamoConditionalStorePrototype(transport=self.transport,
                    binding=replace(self.binding, authority_id='offline://different-account/table'))
        self.assertNotEqual(other.read().revision, self.seed.revision)
        self.assertFalse(other.compare_and_swap(self.seed.revision, candidate(self.initial, '1')).confirmed)

    def test_deletion_before_update_never_upserts_a_new_authority(self):
        self.transport.before_update = lambda _request: self.transport.admin_replace(None)
        result = self.claim()
        self.assertFalse(result.confirmed)
        self.assertTrue(result.requires_reconciliation)
        self.assertIsNone(self.transport.current())

    def test_generation_limit_reserves_last_increment_for_emergency_off(self):
        self.make(generation=engine.MAX_GENERATION - 1)
        self.assertFalse(self.claim().confirmed)
        self.assertEqual(len(self.calls('UpdateItem')), 0)
        stopped = replace(self.initial, control=disable_controller(self.initial.control))
        self.assertTrue(self.store.compare_and_swap(self.seed.revision, stopped).confirmed)
        self.assertEqual(self.store.read().generation, engine.MAX_GENERATION)
        with self.assertRaises(engine.StoreUnavailable):
            engine.Snapshot(self.initial, engine.MAX_GENERATION, self.binding)

    def test_arbitrary_owner_replacement_quota_change_and_noop_are_rejected(self):
        changed_quota = replace(self.initial, control=replace(self.initial.control,
            daily_quota=replace(self.initial.control.daily_quota, reserved_usd='0.80', attempts=2)))
        for proposal in (self.initial, changed_quota, {'verified': True}):
            self.assertFalse(self.store.compare_and_swap(self.seed.revision, proposal).confirmed)
        self.assertEqual(len(self.calls('UpdateItem')), 0)
        self.assertTrue(self.claim().confirmed)
        owned = self.store.read()
        self.assertFalse(self.store.compare_and_swap(owned.revision, candidate(self.initial, '2')).confirmed)

    def test_no_lease_or_takeover_even_for_old_reserved_owner(self):
        self.assertTrue(self.claim().confirmed)
        owned = self.store.read()
        # There is deliberately no expiry/renewal/clock input to the store.
        self.assertFalse(self.store.compare_and_swap(owned.revision, self.initial).confirmed)
        self.assertEqual(self.store.read(), owned)

    def test_sdk_requirements_and_existing_production_blocks_stay_separate(self):
        options = engine.sdk_config_requirements()
        self.assertEqual(options['retries'], {'mode': 'standard', 'total_max_attempts': 1})
        options['retries']['total_max_attempts'] = 100
        self.assertEqual(engine.sdk_config_requirements()['retries']['total_max_attempts'], 1)
        self.assertIsNone(v3.CONTROLLER_STORE)
        self.assertIsNone(worker_identity.CONTROLLER_VERIFIER)
        result = v3.claim_reserved_intent_offline(
            intent_id=self.initial.control.pending.intent_id, approval_epoch=3, request_id='1' * 32,
            worker_oidc_token={'verified': True}, now=NOW)
        self.assertFalse(result.claim_confirmed)
        self.assertFalse(result.execution_allowed)
        config = worker_configuration.WORKER_CONFIG
        self.assertEqual((config.research_reports_per_question, config.predictions_per_research_report), (2, 3))
        self.assertEqual(config.agent_requests_per_atomic_question, 4)
        self.assertEqual(MAX_AUTO_AGENT_POST_ATTEMPTS, 2)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(DynamoConditionalPrototypeTests))
    print(f'Offline audit guard: {len(NETWORK_ATTEMPTS)} network attempts.')
    sys.exit(0 if result.wasSuccessful() and not NETWORK_ATTEMPTS else 1)

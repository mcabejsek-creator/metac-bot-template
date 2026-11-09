"""Durable at-most-once admission, not exactly-once effects. No live services."""

from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import json
import os
import pickle
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier, Event, Lock
import unittest
from unittest.mock import patch

if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

if __package__:
    from . import test_command_gateway_offline as gateway_tests
    from .fake_command_auth import SyntheticCommandAuthentication, SyntheticKeySnapshot
    from .fake_effect_recovery import (
        MeteredSyntheticDynamo, NonIdempotentSyntheticProvider, SyntheticIndependentOutcomeAuthority)
    from .fake_dynamodb_control import ModelError
    from .test_worker_claim_offline import reserved, NOW
    from .test_autonomous_runtime_offline import reject_network, NETWORK_ATTEMPTS
else:
    import test_command_gateway_offline as gateway_tests
    from fake_command_auth import SyntheticCommandAuthentication, SyntheticKeySnapshot
    from fake_effect_recovery import (
        MeteredSyntheticDynamo, NonIdempotentSyntheticProvider, SyntheticIndependentOutcomeAuthority)
    from fake_dynamodb_control import ModelError
    from test_worker_claim_offline import reserved, NOW
    from test_autonomous_runtime_offline import reject_network, NETWORK_ATTEMPTS

if __name__=='__main__':
    sys.addaudithook(reject_network)

import durable_effect_offline as recovery
import command_gateway_offline as command
import dynamodb_cas_offline as engine
import worker_claim_offline as v3
import worker_identity
import worker_configuration
from dispatch_intent_controller import disable_controller
from perplexity_run_budget import MAX_AUTO_AGENT_POST_ATTEMPTS


class ProcessInterrupted(BaseException):
    pass


class DurableEffectRecoveryTests(unittest.TestCase):
    token = gateway_tests.CommandGatewayOfflineTests.token

    @classmethod
    def setUpClass(cls):
        fixture = gateway_tests.CommandGatewayOfflineTests
        fixture.setUpClass()
        for name in ('jwt','key','pem','other_key','other_pem','policy'):
            setattr(cls,name,getattr(fixture,name))

    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory(prefix='synthetic-durable-effect-'))
        self.serial = 0
        self.binding = engine.AuthorityBinding('offline-control','controller','a'*32,'offline://account/region/table')
        self.authorization = command.RunAuthorizationDraft('c'*32,reserved().pending.intent_id,3,
            '2026-11-09T12:30:00Z','2026-11-09T12:35:00Z',1,self.policy)
        keys = SyntheticKeySnapshot(1,(('synthetic-command-kid',self.pem),),
                                    NOW-timedelta(minutes=1),NOW+timedelta(minutes=10))
        self.auth = SyntheticCommandAuthentication(self.binding,keys=(keys,),now=NOW)
        self.worker = self.auth.worker_invocation(self.token())
        self.operator = self.auth.operator_invocation()
        self.controller = self.auth.controller_invocation()
        self.make()

    def make(self,*,claimed=True,kind='paid_model',generation=None):
        self.serial += 1
        self.path = str(Path(self.directory)/f'control-{self.serial}.sqlite')
        state = v3.OfflineControlStateV3(reserved())
        base = command.CommandSnapshotDraft(engine.Snapshot(state,1,self.binding),self.authorization)
        definition = recovery.EffectDefinition(self.authorization.intent_id,3,self.authorization.authorization_id,
            self.policy.run_id,'d'*64,'fixed.synthetic.request',kind,'synthetic://provider',
            'SYNTHETIC:fixed request body','0.40','0.20')
        self.unclaimed = recovery.RecoverySnapshot(base,recovery.EffectRecord(
            recovery.operation_id(base,definition),definition))
        self.raw = MeteredSyntheticDynamo.seed(self.path,self.unclaimed.item())
        self.provider = NonIdempotentSyntheticProvider()
        self.source = SyntheticIndependentOutcomeAuthority()
        self.source.register(self.controller,self.provider)
        self.hooks = {}
        self.gateway = self.restart()
        if claimed:
            self.assertTrue(self.claim().claim_confirmed)
        if generation is not None:
            before = self.current()
            changed = replace(before,base=replace(before.base,core=replace(before.base.core,generation=generation)))
            self.raw.admin_replace(changed.item())
        self.start = self.current()
        self.raw.calls.clear()
        self.raw.commits = 0

    def restart(self,**overrides):
        options = dict(transport=self.raw,binding=self.binding,authentication=self.auth,clock=lambda:NOW,
                       providers={'synthetic://provider':self.provider},reconciliation=self.source,hooks=self.hooks)
        options.update(overrides)
        return recovery.OfflineEffectRecoveryGateway(**options)

    def current(self):
        return recovery.RecoverySnapshot.decode(self.raw.current(),self.binding)

    def claim(self,*,invocation=None,revision=None):
        state = self.current()
        return self.gateway.handle('claim_reserved_intent',dict(intent_id=self.authorization.intent_id,
            approval_epoch=3,expected_revision=state.revision if revision is None else revision),
            invocation=self.worker if invocation is None else invocation)

    def payload(self,**changes):
        return {**dict(intent_id=self.authorization.intent_id,approval_epoch=3,
                       operation_id=self.start.effect.operation_id,expected_revision=self.start.revision),**changes}

    def attempt(self,*,payload=None,invocation=None,gateway=None):
        result = (self.gateway if gateway is None else gateway).handle('simulate_external_attempt',
            self.payload() if payload is None else payload,invocation=self.worker if invocation is None else invocation)
        self.assertFalse(result.execution_allowed)
        self.assertFalse(result.external_retry_allowed)
        return result

    def stop(self,*,invocation=None):
        return self.restart().handle('emergency_stop',{'expected_revision':self.current().revision},
                                     invocation=self.operator if invocation is None else invocation)

    def reconcile(self,*,invocation=None):
        state = self.current()
        return self.restart().handle('record_independent_outcome',dict(operation_id=state.effect.operation_id,
            expected_revision=state.revision),invocation=self.controller if invocation is None else invocation)

    def writes(self):
        return [request for method,request in self.raw.calls if method=='UpdateItem']

    def assert_spent_no_reissue(self):
        before = self.current()
        calls, effects = self.provider.entries,len(self.provider.effects)
        result = self.attempt(payload=self.payload(expected_revision=before.revision),gateway=self.restart())
        self.assertFalse(result.accepted)
        self.assertFalse(result.simulated_dispatch_authorized)
        self.assertEqual(self.current(),before)
        self.assertEqual((self.provider.entries,len(self.provider.effects)),(calls,effects))

    def test_claim_and_gate_preserve_reservation_owner_quota_and_plan(self):
        result = self.attempt()
        self.assertTrue(result.attempt_commit_confirmed)
        self.assertTrue(result.simulated_dispatch_authorized)
        self.assertTrue(result.simulated_provider_entered)
        self.assertTrue(result.requires_reconciliation)
        after = self.current()
        self.assertEqual(after.effect.phase,recovery.OUTCOME_UNKNOWN)
        self.assertEqual(after.base.core.state,self.start.base.core.state)
        self.assertEqual(after.base.authorization,self.start.base.authorization)
        self.assertEqual(after.effect.definition,self.start.effect.definition)
        self.assertEqual(after.effect.attempt.committed_generation,3)
        self.assertEqual(after.base.core.generation,4)
        self.assertEqual((len(self.writes()),self.raw.commits,self.provider.entries,self.provider.wire_attempts,
                          len(self.provider.effects)),(2,2,1,1,1))
        self.assert_spent_no_reissue()

    def test_two_claim_candidates_have_one_successful_atomic_owner_transition(self):
        self.make(claimed=False)
        barrier = Barrier(2)
        self.raw.before_update = lambda _request:barrier.wait(timeout=4)
        revision = self.start.revision
        worker2 = self.auth.worker_invocation(self.token(jti='other-synthetic-invocation'))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda context:self.claim(invocation=context,revision=revision),
                                    (self.worker,worker2)))
        self.assertEqual(sum(result.claim_confirmed for result in results),1)
        self.assertEqual(self.raw.commits,1)
        self.assertEqual(len(self.writes()),2)
        self.assertEqual(self.current().effect.phase,recovery.NOT_STARTED)

    def test_two_concurrent_effect_requests_have_one_gate_and_one_send(self):
        barrier = Barrier(2)
        def meet_only_gate(request):
            if request.get('ExpressionAttributeValues',{}).get(':after',{}).get('M',{}).get('phase',{}).get('S')==recovery.ATTEMPT_COMMITTED:
                barrier.wait(timeout=4)
        self.raw.before_update = meet_only_gate
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _:self.attempt(),range(2)))
        self.assertEqual(sum(result.attempt_commit_confirmed for result in results),1)
        self.assertEqual(sum(result.simulated_dispatch_authorized for result in results),1)
        self.assertEqual((self.provider.entries,len(self.provider.effects)),(1,1))
        self.assertEqual(self.current().effect.attempt.committed_generation,3)

    def test_no_commit_timeout_then_explicit_invocation_retries_db_not_effect(self):
        self.raw.before_update = lambda _request:(_ for _ in ()).throw(TimeoutError())
        first = self.attempt()
        self.assertFalse(first.attempt_commit_confirmed)
        self.assertEqual((len(self.writes()),self.raw.commits,self.provider.entries),(1,0,0))
        self.assertEqual(self.current(),self.start)
        self.raw.before_update = None
        second = self.attempt(gateway=self.restart())
        self.assertTrue(second.attempt_commit_confirmed)
        self.assertTrue(second.simulated_dispatch_authorized)
        # 1 failed gate + 1 committed gate + 1 advisory outcome, not 3 effects.
        self.assertEqual((len(self.writes()),self.raw.commits,self.provider.entries,len(self.provider.effects)),(3,2,1,1))
        self.assert_spent_no_reissue()

    def test_lost_gate_ack_commits_but_never_issues_send_or_read_based_permission(self):
        self.raw.after_commit = lambda _request:(_ for _ in ()).throw(TimeoutError())
        first = self.attempt()
        self.assertFalse(first.attempt_commit_confirmed)
        self.assertEqual((len(self.writes()),self.raw.commits,self.provider.entries),(1,1,0))
        self.assertEqual(self.current().effect.phase,recovery.ATTEMPT_COMMITTED)
        self.raw.after_commit = None
        observed = self.restart().handle('read_control',{},invocation=self.worker)
        self.assertTrue(observed.accepted)
        self.assertFalse(observed.simulated_dispatch_authorized)
        self.assert_spent_no_reissue()

    def test_malformed_incomplete_or_retried_gate_ack_withholds_continuation(self):
        for fault in ('retry','missing_effect','missing_metadata','wrong_generation'):
            self.make()
            def corrupt(response,mode=fault):
                response = deepcopy(response)
                if mode=='retry':response['ResponseMetadata']['RetryAttempts']=1
                elif mode=='missing_effect':response['Attributes'].pop('effect')
                elif mode=='missing_metadata':response.pop('ResponseMetadata')
                else:response['Attributes']['generation']={'N':'999'}
                return response
            self.raw.response_filter = corrupt
            result = self.attempt()
            self.assertFalse(result.attempt_commit_confirmed)
            self.assertEqual((self.raw.commits,self.provider.entries),(1,0))
            self.assert_spent_no_reissue()

    def test_crash_after_confirmed_gate_before_provider_consumes_slot_forever(self):
        self.hooks['gate_confirmed'] = lambda _data:(_ for _ in ()).throw(ProcessInterrupted())
        self.gateway = self.restart()
        with self.assertRaises(ProcessInterrupted):self.attempt()
        self.assertEqual((self.raw.commits,self.provider.entries,len(self.provider.effects)),(1,0,0))
        self.assertEqual(self.current().effect.phase,recovery.ATTEMPT_COMMITTED)
        self.hooks.clear()
        self.assert_spent_no_reissue()
        self.assertFalse(self.reconcile().outcome_independently_verified)  # Absence is not a positive proof.

    def test_same_ephemeral_continuation_cannot_be_used_twice(self):
        captured = []
        def capture_before_consumption(data):
            continuation=data['continuation']
            captured.append(continuation)
            for clone in (copy,deepcopy,pickle.dumps):
                with self.assertRaises(TypeError):clone(continuation)
        self.hooks['gate_confirmed'] = capture_before_consumption
        self.gateway = self.restart()
        self.assertTrue(self.attempt().simulated_dispatch_authorized)
        self.assertEqual(len(captured),1)
        for _ in range(3):self.assertFalse(captured[0].run())
        self.assertEqual((self.provider.entries,self.provider.wire_attempts,len(self.provider.effects)),(1,1,1))
        self.assert_spent_no_reissue()

    def test_crash_after_provider_before_result_write_keeps_owner_and_budget(self):
        self.hooks['after_provider'] = lambda _data:(_ for _ in ()).throw(ProcessInterrupted())
        self.gateway = self.restart()
        with self.assertRaises(ProcessInterrupted):self.attempt()
        self.assertEqual((self.provider.entries,len(self.provider.effects)),(1,1))
        self.assertEqual(self.current().effect.phase,recovery.ATTEMPT_COMMITTED)
        self.assertEqual(self.current().base.core.state,self.start.base.core.state)
        self.assert_spent_no_reissue()
        self.assertTrue(self.reconcile().outcome_independently_verified)
        self.assertEqual(self.current().effect.phase,recovery.OUTCOME_VERIFIED)
        self.assert_spent_no_reissue()

    def test_process_crash_inside_provider_after_effect_does_not_allow_replay(self):
        self.provider.after_effect = lambda:(_ for _ in ()).throw(ProcessInterrupted())
        with self.assertRaises(ProcessInterrupted):self.attempt()
        self.assertEqual((self.provider.wire_attempts,len(self.provider.effects)),(1,1))
        self.assertEqual(self.current().effect.phase,recovery.ATTEMPT_COMMITTED)
        self.assert_spent_no_reissue()

    def test_timeout_connection_loss_500_and_rate_limit_never_retry_provider(self):
        for status,error,applied in [(200,TimeoutError,True),(200,ConnectionResetError,True),
                                      (500,None,True),(500,None,False),(429,None,False)]:
            self.make()
            self.provider.status,self.provider.error,self.provider.apply_effect=status,error,applied
            result = self.attempt()
            self.assertTrue(result.attempt_commit_confirmed)
            self.assertTrue(result.requires_reconciliation)
            self.assertEqual(self.current().effect.phase,recovery.OUTCOME_UNKNOWN)
            self.assertEqual((self.provider.entries,self.provider.wire_attempts),(1,1))
            self.assertEqual(len(self.provider.effects),int(applied))
            self.assert_spent_no_reissue()

    def test_successful_provider_response_is_not_independent_outcome_or_refund(self):
        self.assertTrue(self.attempt().simulated_provider_entered)
        self.assertEqual(self.provider.status,200)
        self.assertEqual(self.current().effect.phase,recovery.OUTCOME_UNKNOWN)
        self.assertIsNone(self.current().effect.evidence)
        self.assertEqual(self.current().base.core.state.control.daily_quota.reserved_usd,'0.40')
        self.assertTrue(self.reconcile().outcome_independently_verified)
        self.assertEqual(self.current().base.core.state.control.daily_quota.reserved_usd,'0.40')
        self.assert_spent_no_reissue()

    def test_unknown_billing_or_absent_effect_cannot_be_called_verified(self):
        for mode in ('billing_unavailable','no_effect','over_cap'):
            self.make()
            if mode=='no_effect':self.provider.apply_effect=False
            if mode=='over_cap':self.provider.cost='0.30'
            self.attempt()
            if mode=='billing_unavailable':self.provider.billing_available=False
            self.assertFalse(self.reconcile().outcome_independently_verified)
            self.assertEqual(self.current().effect.phase,recovery.OUTCOME_UNKNOWN)
            self.assert_spent_no_reissue()

    def test_reconciliation_requires_separate_registered_controller_authority(self):
        self.attempt()
        for context in (self.worker,self.operator,object(),True):
            self.assertFalse(self.reconcile(invocation=context).outcome_independently_verified)
        self.source.available=False
        self.assertFalse(self.reconcile().outcome_independently_verified)
        self.assertEqual(self.current().effect.phase,recovery.OUTCOME_UNKNOWN)

    def test_forged_or_future_independent_evidence_does_not_change_unknown_state(self):
        self.attempt()
        proof=self.source.verify(self.controller,self.current(),now=NOW)
        for value in (True,{'verified':True},replace(proof,operation_id='f'*64),
                      replace(proof,charged_usd='0.30'),replace(proof,checked_at='2026-11-09T12:30:01Z')):
            with patch.object(self.source,'verify',return_value=value):
                self.assertFalse(self.reconcile().outcome_independently_verified)
            self.assertEqual(self.current().effect.phase,recovery.OUTCOME_UNKNOWN)
        self.assertEqual(self.provider.entries,1)

    def test_live_original_with_delayed_gate_ack_gets_only_original_continuation(self):
        waiting,release=Event(),Event()
        def delay_gate(request):
            if request['ExpressionAttributeValues'].get(':after',{}).get('M',{}).get('phase',{}).get('S')==recovery.ATTEMPT_COMMITTED:
                waiting.set()
                if not release.wait(4):raise RuntimeError('test barrier failed')
        self.raw.after_commit=delay_gate
        with ThreadPoolExecutor(max_workers=1) as pool:
            original=pool.submit(self.attempt)
            try:
                self.assertTrue(waiting.wait(4))
                duplicate=self.attempt(payload=self.payload(expected_revision=self.current().revision),gateway=self.restart())
                self.assertFalse(duplicate.simulated_dispatch_authorized)
                self.assertEqual(self.provider.entries,0)
            finally:release.set()
            self.assertTrue(original.result(timeout=4).simulated_dispatch_authorized)
        self.assertEqual((self.provider.entries,len(self.provider.effects)),(1,1))

    def test_late_reply_after_timeout_cannot_resurrect_returned_handler(self):
        delayed=[]
        def lose_reply(response):
            delayed.append(deepcopy(response))
            raise TimeoutError('Synthetic discarded response')
        self.raw.response_filter=lose_reply
        self.assertFalse(self.attempt().attempt_commit_confirmed)
        self.raw.response_filter=None
        self.assertEqual(len(delayed),1)
        self.assertEqual(self.provider.entries,0)
        self.assert_spent_no_reissue()
        self.assertFalse(self.attempt(payload={**self.payload(),'ack':delayed[0]}).accepted)
        self.assertEqual(self.provider.entries,0)

    def test_delayed_advisory_write_cannot_overwrite_independent_confirmation(self):
        waiting,release=Event(),Event()
        def pause(_data):
            waiting.set()
            if not release.wait(4):raise RuntimeError('test barrier failed')
        self.hooks['after_provider']=pause
        self.gateway=self.restart()
        with ThreadPoolExecutor(max_workers=1) as pool:
            original=pool.submit(self.attempt)
            try:
                self.assertTrue(waiting.wait(4))
                self.assertTrue(self.reconcile().outcome_independently_verified)
            finally:release.set()
            original.result(timeout=4)
        self.assertEqual(self.current().effect.phase,recovery.OUTCOME_VERIFIED)
        self.assertEqual(self.current().base.core.generation,4)
        self.assertEqual(len(self.provider.effects),1)

    def test_stop_before_gate_and_stop_between_read_and_write_block_send(self):
        self.assertTrue(self.stop().accepted)
        self.assertFalse(self.attempt(payload=self.payload(expected_revision=self.current().revision)).simulated_dispatch_authorized)
        self.assertEqual(self.provider.entries,0)
        self.make()
        off=replace(self.start,base=replace(self.start.base,core=replace(self.start.base.core,
            state=replace(self.start.base.core.state,control=disable_controller(self.start.base.core.state.control)),generation=3)))
        self.raw.before_update=lambda _request:self.raw.admin_replace(off.item())
        self.assertFalse(self.attempt().attempt_commit_confirmed)
        self.assertEqual(self.provider.entries,0)
        self.assertFalse(self.current().base.core.state.control.enabled)

    def test_stop_before_final_read_blocks_continuation_and_keeps_slot_spent(self):
        self.hooks['before_final_read']=lambda _data:self.assertTrue(self.stop().accepted)
        self.gateway=self.restart()
        result=self.attempt()
        self.assertTrue(result.attempt_commit_confirmed)
        self.assertFalse(result.simulated_dispatch_authorized)
        self.assertEqual(self.provider.entries,0)
        self.assertFalse(self.current().base.core.state.control.enabled)
        self.assert_spent_no_reissue()

    def test_stop_after_final_read_is_explicit_inflight_cancellation_gap(self):
        self.hooks['after_final_read']=lambda _data:self.assertTrue(self.stop().accepted)
        self.gateway=self.restart()
        result=self.attempt()
        self.assertTrue(result.simulated_dispatch_authorized)
        self.assertFalse(self.current().base.core.state.control.enabled)
        self.assertEqual((self.provider.entries,len(self.provider.effects)),(1,1))
        self.assert_spent_no_reissue()

    def test_stop_during_external_operation_does_not_undo_effect_or_allow_second(self):
        self.provider.after_effect=lambda:self.assertTrue(self.stop().accepted)
        self.assertTrue(self.attempt().simulated_provider_entered)
        self.assertFalse(self.current().base.core.state.control.enabled)
        self.assertEqual(len(self.provider.effects),1)
        self.assertEqual(self.current().base.core.state.worker_claim,self.start.base.core.state.worker_claim)
        self.assertEqual(self.current().base.core.state.control.daily_quota,self.start.base.core.state.control.daily_quota)
        self.assert_spent_no_reissue()

    def test_stale_cache_before_write_and_after_ack_never_reissues_or_confirms_gate(self):
        self.raw.read_filter=lambda response:{**response,'Item':self.start.item()}
        result=self.attempt()
        self.assertFalse(result.attempt_commit_confirmed)
        self.assertEqual((self.raw.commits,self.provider.entries),(1,0))
        self.assertEqual(self.current().effect.phase,recovery.ATTEMPT_COMMITTED)
        self.assertFalse(self.attempt(gateway=self.restart()).attempt_commit_confirmed)
        self.assertEqual((self.raw.commits,self.provider.entries),(1,0))

    def test_changed_quota_configuration_or_run_binding_conflicts_atomically(self):
        for mode in ('quota','configuration','run'):
            self.make()
            changed=self.start
            if mode=='quota':
                control=replace(self.start.base.core.state.control,
                    daily_quota=replace(self.start.base.core.state.control.daily_quota,reserved_usd='0.80'))
                changed=replace(changed,base=replace(changed.base,core=replace(changed.base.core,
                    state=replace(changed.base.core.state,control=control))))
            elif mode=='configuration':
                definition=replace(changed.effect.definition,configuration_digest='e'*64)
                changed=replace(changed,effect=replace(changed.effect,definition=definition,
                    operation_id=recovery.operation_id(changed.base,definition)))
            else:
                # A privileged alteration invalidates the already recorded owner;
                # full item equality still rejects it before any effect.
                item=changed.item()
                item['run_authorization']['M']['policy']['M']['run_id']={'S':'999002'}
                self.raw.before_update=lambda _request,value=item:self.raw.admin_replace(value)
            if mode!='run':self.raw.before_update=lambda _request,value=changed:self.raw.admin_replace(value.item())
            self.assertFalse(self.attempt().attempt_commit_confirmed)
            self.assertEqual(self.provider.entries,0)

    def test_other_run_repeated_attempt_or_workflow_cannot_bypass_spent_permission(self):
        for changes in ({'run_id':'999002'},{'run_attempt':'2'},{'sha':'e'*40},{'workflow_sha':'e'*40}):
            context=self.auth.worker_invocation(self.token(**changes))
            self.assertFalse(self.attempt(invocation=context).attempt_commit_confirmed)
        self.assertEqual(self.provider.entries,0)
        self.assertTrue(self.attempt().simulated_provider_entered)
        new_run=self.auth.worker_invocation(self.token(run_id='999002'))
        self.assertFalse(self.attempt(invocation=new_run,payload=self.payload(expected_revision=self.current().revision)).accepted)
        self.assertEqual(self.provider.entries,1)

    def test_forged_request_cannot_choose_payload_owner_permission_config_or_next_operation(self):
        for key,value in [('owner',{}),('verified',True),('payload','SYNTHETIC:other'),
                          ('request_id','f'*32),('attempt_id','f'*64),('configuration_digest','f'*64),
                          ('phase',recovery.NOT_STARTED),('generation',0),('incarnation','b'*32),
                          ('table_name','other'),('permission',{'allowed':True}),('execution_allowed',True)]:
            self.assertFalse(self.attempt(payload={**self.payload(),key:value}).accepted)
        self.assertFalse(self.attempt(payload=self.payload(operation_id='f'*64)).accepted)
        for name in ('reset','release','takeover','restore','simulate_actual_worker','reserve_intent','retry_external'):
            self.assertFalse(self.gateway.handle(name,{},invocation=self.controller).accepted)
        self.assertEqual((len(self.writes()),self.provider.entries),(0,0))

    def test_missing_trust_store_provider_or_verified_owner_fails_closed(self):
        for changes in ({'authentication':None},{'transport':None},{'clock':None},{'providers':{}}):
            self.assertFalse(self.attempt(gateway=self.restart(**changes)).attempt_commit_confirmed)
        self.auth.worker_keys_available=False
        self.assertFalse(self.attempt().attempt_commit_confirmed)
        self.auth.worker_keys_available=True
        self.make(claimed=False)
        self.assertFalse(self.attempt().attempt_commit_confirmed)
        self.assertEqual(self.provider.entries,0)

    def test_identity_keys_unavailable_after_commit_withhold_send_without_releasing_slot(self):
        self.hooks['gate_confirmed']=lambda _data:setattr(self.auth,'worker_keys_available',False)
        self.gateway=self.restart()
        result=self.attempt()
        self.assertTrue(result.attempt_commit_confirmed)
        self.assertFalse(result.simulated_provider_entered)
        self.assertNotEqual(self.current().effect.phase,recovery.NOT_STARTED)
        self.assertEqual(self.provider.entries,0)
        self.assert_spent_no_reissue()

    def test_token_expiry_after_gate_confirmation_never_reopens_permission(self):
        self.hooks['gate_confirmed']=lambda _data:setattr(self.gateway._identity,'_clock',
                                                        lambda:NOW+timedelta(seconds=120))
        self.gateway=self.restart()
        result=self.attempt()
        self.assertTrue(result.attempt_commit_confirmed)
        self.assertFalse(result.simulated_provider_entered)
        self.assertEqual(self.provider.entries,0)
        self.assert_spent_no_reissue()

    def test_stop_lost_ack_preserves_durable_off_and_worker_cannot_authorize_stop(self):
        self.assertFalse(self.stop(invocation=self.worker).accepted)
        self.assertEqual(self.writes(),[])
        self.raw.after_commit=lambda _request:(_ for _ in ()).throw(TimeoutError())
        self.assertFalse(self.stop().accepted)
        self.assertFalse(self.current().base.core.state.control.enabled)
        self.assertEqual(self.current().base.core.state.control.daily_quota,self.start.base.core.state.control.daily_quota)
        self.assertFalse(self.attempt(payload=self.payload(expected_revision=self.current().revision)).simulated_provider_entered)
        self.assertEqual(self.provider.entries,0)

    def test_transport_failure_before_wire_is_unknown_not_automatic_no_effect_recovery(self):
        self.provider.before_wire=lambda:(_ for _ in ()).throw(ConnectionResetError())
        self.assertTrue(self.attempt().attempt_commit_confirmed)
        self.assertEqual((self.provider.entries,self.provider.wire_attempts,len(self.provider.effects)),(1,0,0))
        self.assertEqual(self.current().effect.phase,recovery.OUTCOME_UNKNOWN)
        self.assertFalse(self.reconcile().outcome_independently_verified)
        self.assert_spent_no_reissue()

    def test_database_unavailable_at_final_check_withholds_send(self):
        def fail_read(_data):
            self.raw.read_filter=lambda _response:(_ for _ in ()).throw(ConnectionResetError())
        self.hooks['gate_confirmed']=fail_read
        self.gateway=self.restart()
        result=self.attempt()
        self.assertTrue(result.attempt_commit_confirmed)
        self.assertFalse(result.simulated_provider_entered)
        self.assertEqual(self.provider.entries,0)
        self.raw.read_filter=None
        self.assert_spent_no_reissue()

    def test_lost_advisory_ack_after_external_effect_keeps_durable_block(self):
        def lose_only_outcome(request):
            phase=request['ExpressionAttributeValues'].get(':after',{}).get('M',{}).get('phase',{}).get('S')
            if phase==recovery.OUTCOME_UNKNOWN:raise TimeoutError()
        self.raw.after_commit=lose_only_outcome
        self.assertTrue(self.attempt().simulated_provider_entered)
        self.assertEqual((self.raw.commits,self.provider.entries),(2,1))
        self.assertEqual(self.current().effect.phase,recovery.OUTCOME_UNKNOWN)
        self.assert_spent_no_reissue()

    def test_sdk_like_duplicate_database_send_has_one_commit_and_no_external_grant(self):
        self.raw.after_commit=lambda _request:(_ for _ in ()).throw(ConnectionResetError())
        raw=self.raw
        class DeliberatelyRetryingDb:
            def get_item(self,**request):return raw.get_item(**request)
            def update_item(self,**request):
                try:return raw.update_item(**request)
                except ConnectionResetError:return raw.update_item(**request)
        result=self.attempt(gateway=self.restart(transport=DeliberatelyRetryingDb()))
        self.assertFalse(result.attempt_commit_confirmed)
        self.assertEqual((len(self.writes()),self.raw.commits,self.provider.entries),(2,1,0))
        self.assert_spent_no_reissue()

    def test_idempotent_success_replay_is_not_unique_conditional_ack(self):
        # Deliberately INVALID UpdateItem transport: analogous to naively
        # turning replayed TransactWriteItems success into another send grant.
        # One commit and two success replies do not imply one continuation.
        requests_ready,confirmations_ready,final_reads_ready=Barrier(2),Barrier(2),Barrier(2)
        raw=self.raw
        lock=Lock()
        cached=[]  # Fault instrumentation only, not a proposed control cache.
        class ReplayedSuccess:
            def get_item(self,**request):return raw.get_item(**request)
            def update_item(self,**request):
                phase=request['ExpressionAttributeValues'].get(':after',{}).get('M',{}).get('phase',{}).get('S')
                if phase!=recovery.ATTEMPT_COMMITTED:return raw.update_item(**request)
                requests_ready.wait(timeout=4)
                with lock:
                    if not cached:cached.append(deepcopy(raw.update_item(**request)))
                    return deepcopy(cached[0])
        self.hooks['gate_confirmed']=lambda _data:confirmations_ready.wait(timeout=4)
        self.hooks['after_final_read']=lambda _data:final_reads_ready.wait(timeout=4)
        gateway=self.restart(transport=ReplayedSuccess())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.attempt(gateway=gateway),range(2)))
        self.assertEqual(sum(result.attempt_commit_confirmed for result in results),2)
        gate_writes=[request for request in self.writes() if
            request['ExpressionAttributeValues'].get(':after',{}).get('M',{}).get('phase',{}).get('S')==recovery.ATTEMPT_COMMITTED]
        self.assertEqual(len(gate_writes),1)
        self.assertEqual((self.provider.entries,len(self.provider.effects)),(2,2))
        self.assertTrue(all(not result.execution_allowed for result in results))

    def test_provider_without_idempotency_is_protected_by_durable_admission(self):
        self.assertTrue(self.attempt().simulated_provider_entered)
        for _ in range(3):self.assert_spent_no_reissue()
        self.assertEqual((self.provider.wire_attempts,len(self.provider.effects)),(1,1))
        # Negative control: the provider itself would duplicate the effect.
        definition,operation_id,attempt_id=self.provider.calls[0]
        self.provider.send_once(definition,operation_id,attempt_id)
        self.assertEqual(len(self.provider.effects),2)

    def test_hidden_provider_retry_is_an_explicit_counterexample_not_a_guarantee(self):
        self.provider.physical_attempts_per_call=2
        result=self.attempt()
        self.assertTrue(result.simulated_dispatch_authorized)
        self.assertEqual((self.provider.entries,self.provider.wire_attempts,len(self.provider.effects)),(1,2,2))
        self.assertFalse(self.reconcile().outcome_independently_verified)
        self.assert_spent_no_reissue()

    def test_all_irreversible_operation_kinds_use_the_same_no_retry_protocol(self):
        for kind in ('paid_model','github_dispatch','metaculus_forecast','metaculus_comment'):
            self.make(kind=kind)
            self.assertTrue(self.attempt().simulated_provider_entered)
            self.assertEqual((self.provider.entries,len(self.provider.effects)),(1,1))
            self.assert_spent_no_reissue()

    def test_privileged_whole_snapshot_rollback_breaks_guarantee_explicitly(self):
        self.assertTrue(self.attempt().simulated_provider_entered)
        self.raw.admin_replace(self.unclaimed.item())  # Outside the trusted-writer contract.
        self.assertTrue(self.claim().claim_confirmed)
        restored=self.current()
        second=self.attempt(payload=self.payload(expected_revision=restored.revision),gateway=self.restart())
        self.assertTrue(second.simulated_provider_entered)
        self.assertEqual((self.provider.entries,len(self.provider.effects)),(2,2))
        self.assertFalse(second.execution_allowed)

    def test_restore_with_new_protected_incarnation_is_not_automatic_recovery(self):
        self.attempt()
        self.raw.admin_replace(self.unclaimed.item())
        binding=replace(self.binding,incarnation='b'*32)
        auth=SyntheticCommandAuthentication(binding,keys=(),now=NOW)
        context=auth.operator_invocation()
        gateway=self.restart(binding=binding,authentication=auth)
        self.assertFalse(gateway.handle('read_control',{},invocation=context).accepted)
        self.assertEqual(self.provider.entries,1)

    def test_fresh_process_restart_cannot_reconstruct_a_send_continuation(self):
        self.raw.after_commit=lambda _request:(_ for _ in ()).throw(TimeoutError())
        self.attempt()
        script='''
import json,sys
from test_autonomous_runtime_offline import reject_network,NETWORK_ATTEMPTS
sys.addaudithook(reject_network)
from fake_dynamodb_control import SyntheticDynamoTransport
from command_gateway_offline import SyntheticServerActor
from durable_effect_offline import OfflineEffectRecoveryGateway,RecoverySnapshot
from dynamodb_cas_offline import AuthorityBinding
from datetime import datetime
d=json.load(sys.stdin);b=AuthorityBinding(**d['binding']);raw=SyntheticDynamoTransport(d['path'])
s=RecoverySnapshot.decode(raw.current(),b);context=object()
class Auth:
 def reader(self,invocation,binding,*,now):
  if invocation is not context:raise RuntimeError('untrusted')
  return SyntheticServerActor('synthetic://restart','worker',b.authority_id,b.incarnation,int(now.timestamp())+60)
 def worker(self,*a,**kw):raise AssertionError('spent record must not issue identity-based continuation')
class Provider:
 def send_once(self,*a):raise AssertionError('restart must not send')
g=OfflineEffectRecoveryGateway(transport=raw,binding=b,authentication=Auth(),clock=lambda:datetime.fromisoformat(d['now']),providers={'synthetic://provider':Provider()})
r=g.handle('simulate_external_attempt',dict(intent_id=s.effect.definition.intent_id,approval_epoch=3,operation_id=s.effect.operation_id,expected_revision=s.revision),invocation=context)
print(json.dumps(dict(accepted=r.accepted,authorized=r.simulated_dispatch_authorized,execution_allowed=r.execution_allowed,writes=sum(n=='UpdateItem' for n,_ in raw.calls),phase=s.effect.phase,network_attempts=NETWORK_ATTEMPTS)))
'''
        root=Path(__file__).resolve().parents[1]
        env={n:v for n,v in os.environ.items() if not n.endswith(('_TOKEN','_API_KEY','_SECRET'))}
        env['PYTHONPATH']=str(root/'tests')+os.pathsep+str(root)
        process=subprocess.run([sys.executable,'-B','-c',script],input=json.dumps(
            dict(path=self.path,binding=asdict(self.binding),now=NOW.isoformat())),
            text=True,capture_output=True,cwd=root,env=env,check=True,timeout=5)
        self.assertEqual(json.loads(process.stdout),dict(accepted=False,authorized=False,execution_allowed=False,
            writes=0,phase=recovery.ATTEMPT_COMMITTED,network_attempts=[]))

    def test_spent_record_survives_beyond_aws_ten_minute_window_and_utc_day(self):
        self.raw.after_commit=lambda _request:(_ for _ in ()).throw(TimeoutError())
        self.attempt()
        spent=self.current()
        later=NOW+timedelta(days=1)
        auth=SyntheticCommandAuthentication(self.binding,keys=(),now=later)
        operator=auth.operator_invocation()
        gateway=self.restart(authentication=auth,clock=lambda:later)
        view=gateway.handle('read_control',{},invocation=operator)
        self.assertTrue(view.accepted)
        self.assertEqual(view.control['effect']['phase'],recovery.ATTEMPT_COMMITTED)
        self.assertEqual(self.current(),spent)
        self.assertFalse(view.simulated_dispatch_authorized)

    def test_generation_headroom_is_preserved_for_permanent_off(self):
        self.make(generation=engine.MAX_GENERATION-1)
        self.assertFalse(self.attempt().attempt_commit_confirmed)
        self.assertEqual(self.provider.entries,0)
        self.assertTrue(self.stop().accepted)
        self.assertEqual(self.current().base.core.generation,engine.MAX_GENERATION)
        self.assertFalse(self.current().base.core.state.control.enabled)

    def test_no_migration_real_worker_or_budget_change_is_introduced(self):
        with self.assertRaises(engine.StoreUnavailable):
            command.CommandSnapshotDraft.decode(self.start.item(),self.binding)
        with self.assertRaises(engine.StoreUnavailable):
            recovery.RecoverySnapshot.decode(self.start.base.item(),self.binding)
        self.assertIs(v3.CONTROLLER_STORE,None)
        self.assertIs(worker_identity.CONTROLLER_VERIFIER,None)
        self.assertIs(command.PRODUCTION_COMMAND_GATEWAY,None)
        self.assertIs(recovery.PRODUCTION_EFFECT_RECOVERY,None)
        config=worker_configuration.WORKER_CONFIG
        self.assertEqual((config.research_reports_per_question,config.predictions_per_research_report,
                          config.agent_requests_per_atomic_question,MAX_AUTO_AGENT_POST_ATTEMPTS),(2,3,4,2))
        self.assertFalse(recovery.RecoveryResult(True,'synthetic',simulated_dispatch_authorized=True).execution_allowed)
        self.assertEqual(engine.sdk_config_requirements()['retries']['total_max_attempts'],1)


if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(DurableEffectRecoveryTests)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    print('Unexpected network attempts:',NETWORK_ATTEMPTS)
    raise SystemExit(0 if result.wasSuccessful() and not NETWORK_ATTEMPTS else 1)

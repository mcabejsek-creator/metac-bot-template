"""Actual reserve/bind/create/claim/effect composition, never an effect seed.

Existing SQLite fake, RSA/ingress fixtures, provider and outcome authority are
reused. Metric wrappers only observe validated results, never grant permission.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Lock
import unittest
from unittest.mock import patch

if __name__ == '__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

if __package__:
    from . import test_reserve_bind_offline as admission_fixture
    from . import test_command_gateway_offline as crypto_fixture
    from .fake_effect_recovery import SyntheticIndependentOutcomeAuthority
    from .fake_dynamodb_control import ModelError
    from .test_worker_claim_offline import NOW
    from .test_autonomous_runtime_offline import reject_network, NETWORK_ATTEMPTS
else:
    import test_reserve_bind_offline as admission_fixture
    import test_command_gateway_offline as crypto_fixture
    from fake_effect_recovery import SyntheticIndependentOutcomeAuthority
    from fake_dynamodb_control import ModelError
    from test_worker_claim_offline import NOW
    from test_autonomous_runtime_offline import reject_network, NETWORK_ATTEMPTS

if __name__ == '__main__':
    sys.addaudithook(reject_network)

import create_effect_offline as created
import reserve_bind_offline as admission
import command_gateway_offline as command
import durable_effect_offline as recovery
import dynamodb_cas_offline as engine
import worker_identity
import worker_claim_offline as v3
import worker_configuration
from dispatch_intent_controller import disable_controller
from perplexity_run_budget import MAX_AUTO_AGENT_POST_ATTEMPTS


TRACE_EVIDENCE=[]


class SyntheticCreateAuthority(admission_fixture.SyntheticAdmissionAuthority):
    """Same test ingress/evidence service, extended with registered fixed facts.

    This is NOT a production authority. The independent registrations contain
    no ownership/quota/consumed flags; durable rights remain in the same item.
    """

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.effects={}
        self.effect_approval_available=True
        self.after_effect_verify=None

    def fixed_effect(self,invocation,snapshot,*,now):
        if self.reader(invocation,self._binding,now=now).kind!='controller' or not self.effect_approval_available:
            raise RuntimeError('Independent fixed effect approval unavailable')
        facts,token=self.effects[invocation]
        keys=self.keys.get(facts['trust_generation'])
        if not self.worker_keys_available or keys is None or not keys.not_before<=now<keys.not_after:
            raise RuntimeError('Independent trust generation unavailable')
        identity=worker_identity.GitHubActionsOIDCVerifier(
            snapshot.base.authorization.policy,keys.public_keys).verify(token,now=now)
        proof=created.SyntheticFixedEffectApproval(identity=identity,**facts)
        if self.after_effect_verify:self.after_effect_verify()
        return proof


class IntegratedCreateEffectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        crypto_fixture.CommandGatewayOfflineTests.setUpClass.__func__(cls)

    token=crypto_fixture.CommandGatewayOfflineTests.token
    reserve=admission_fixture.ReserveBindOfflineTests.reserve
    approve_run=admission_fixture.ReserveBindOfflineTests.approve_run
    bind=admission_fixture.ReserveBindOfflineTests.bind
    writes=admission_fixture.ReserveBindOfflineTests.writes

    def setUp(self):
        # Reuse only the existing UNRESERVED bootstrap fixture, not its tests.
        admission_fixture.ReserveBindOfflineTests.setUp(self)
        self.auth=SyntheticCreateAuthority(self.binding,keys=tuple(self.auth.keys.values()),now=NOW)
        self.controller=self.auth.controller_invocation()
        self.operator=self.auth.operator_invocation()
        self.worker=self.auth.worker_invocation(self.token())
        self.auth.reservations[self.controller]=self.proof
        self.outcomes=SyntheticIndependentOutcomeAuthority()
        self.outcomes.register(self.controller,self.provider)
        self.hooks={}
        self.ack_count=self.continuation_count=0
        self.metric_lock=Lock()
        self._observe(admission.OfflineAdmissionGateway,'_write',lambda r:r.accepted)
        self._observe(engine.DynamoConditionalStorePrototype,'compare_and_swap',lambda r:r.confirmed)
        self._observe(recovery._EffectStore,'advance',lambda r:r[0].confirmed)
        self._observe(created.OfflineCreatedEffectGateway,'_create',lambda r:r.accepted)
        self.gateway=self.restart()
        self.assertEqual((len(self.raw.calls),self.raw.commits),(0,0))

    def _observe(self,owner,name,confirmed):
        original=getattr(owner,name)
        def observe(*args,**kwargs):
            result=original(*args,**kwargs)
            if confirmed(result):
                with self.metric_lock:self.ack_count+=1
            return result
        self.enterContext(patch.object(owner,name,observe))

    def _stage(self,name,data):
        if name=='gate_confirmed':
            with self.metric_lock:self.continuation_count+=1
        hook=getattr(self,'hooks',{}).get(name)
        if hook:hook(data)

    def restart(self,**changes):
        hooks={name:(lambda data,name=name:self._stage(name,data)) for name in (
            'gate_confirmed','before_final_read','after_final_read','send_authorized',
            'after_provider','before_outcome_write')}
        options=dict(transport=self.raw,binding=self.binding,authentication=self.auth,clock=lambda:NOW,
            providers={'synthetic://create-effect-provider':self.provider},
            reconciliation=getattr(self,'outcomes',None),hooks=hooks)
        options.update(changes)
        return created.OfflineCreatedEffectGateway(**options)

    def current(self):
        item=self.raw.current()
        return (admission.AdmissionSnapshot.decode(item,self.binding) if set(item)==created.ADMISSION_FIELDS
                else created.CreatedEffectSnapshot.decode(item,self.binding))

    @staticmethod
    def base(snapshot):
        return snapshot.admission if type(snapshot) is created.CreatedEffectSnapshot else snapshot

    def invoke(self,name,payload,invocation=None,gateway=None):
        result=(gateway or self.gateway).handle(name,payload,
            invocation=self.controller if invocation is None else invocation)
        self.assertFalse(result.execution_allowed)
        self.assertFalse(result.external_retry_allowed)
        return result

    def bind_payload(self):
        before=self.current();base=self.base(before)
        return dict(intent_id=base.reservation.intent_id,approval_epoch=3,expected_revision=before.revision)

    def approve_effect(self,*,context=None,token=None,**changes):
        before=self.current();base=self.base(before)
        facts=dict(expected_revision=before.revision,evidence_id=base.reservation.evidence_id,
            authorization_id=base.base.authorization.authorization_id,
            trust_generation=base.base.authorization.trust_generation,
            configuration_digest=base.reservation.scope.configuration_digest,
            artifact_digest=base.reservation.scope.artifact_digest,definition=created.fixed_definition(base))
        facts.update(changes)
        self.auth.effects[self.controller if context is None else context]=(facts,self.token() if token is None else token)
        return facts

    def create(self,*,payload=None,**kwargs):
        return self.invoke('create_fixed_effect',self.bind_payload() if payload is None else payload,**kwargs)

    def claim(self,*,payload=None,**kwargs):
        kwargs.setdefault('invocation',self.worker)
        return self.invoke('claim_reserved_intent',self.bind_payload() if payload is None else payload,**kwargs)

    def attempt(self,*,payload=None,**kwargs):
        before=self.current()
        kwargs.setdefault('invocation',self.worker)
        data={**self.bind_payload(),'operation_id':before.effect.operation_id}
        return self.invoke('simulate_external_attempt',data if payload is None else payload,**kwargs)

    def stop(self,*,gateway=None):
        return self.invoke('emergency_stop',dict(expected_revision=self.current().revision),
                           invocation=self.operator,gateway=gateway)

    def reconcile(self):
        before=self.current()
        return self.invoke('record_independent_outcome',dict(operation_id=before.effect.operation_id,
                           expected_revision=before.revision))

    def to_bound(self):
        self.assertTrue(self.reserve().accepted)
        self.approve_run();self.assertTrue(self.bind().accepted)
        return self.current()

    def to_created(self):
        self.to_bound();self.approve_effect();self.assertTrue(self.create().accepted)
        return self.current()

    def to_owned(self):
        self.to_created();self.assertTrue(self.claim().claim_confirmed)
        return self.current()

    def metrics(self):
        return dict(db_requests=len(self.raw.calls),successful_commits=self.raw.commits,
            positive_acks=self.ack_count,local_continuations=self.continuation_count,
            admitted_transport_entries=self.provider.entries,physical_fake_requests=self.provider.wire_attempts,
            fake_effects=len(self.provider.effects))

    def trace(self,case):
        row=dict(case=case,**self.metrics());TRACE_EVIDENCE.append(row)
        return row

    def assert_no_second_send(self):
        before=(self.continuation_count,self.provider.entries,self.provider.wire_attempts,len(self.provider.effects))
        self.assertFalse(self.attempt(gateway=self.restart()).attempt_commit_confirmed)
        self.assertEqual((self.continuation_count,self.provider.entries,self.provider.wire_attempts,len(self.provider.effects)),before)

    def test_complete_vertical_without_effect_seed_and_all_seven_counts(self):
        self.assertIsNone(self.initial.reservation)
        self.assertIsNone(self.initial.base.authorization)
        self.assertIsNone(self.initial.base.core.state.control.pending)
        self.assertTrue(self.reserve().accepted);self.trace('after_reserve')
        self.approve_run();self.assertTrue(self.bind().accepted);self.trace('after_bind')
        held=self.current()
        self.approve_effect();self.assertTrue(self.create().accepted);self.trace('after_create')
        self.assertTrue(self.claim().claim_confirmed);self.trace('after_claim')
        result=self.attempt()
        self.assertTrue(result.attempt_commit_confirmed)
        self.assertTrue(result.simulated_provider_entered)
        self.assertEqual(self.current().effect.phase,recovery.OUTCOME_UNKNOWN)
        self.trace('after_one_fake_send_unknown')
        self.assertTrue(self.reconcile().outcome_independently_verified);self.trace('after_independent_proof')
        self.assertTrue(self.stop().accepted)
        final=self.current()
        self.assertEqual(final.effect.phase,recovery.OUTCOME_VERIFIED)
        self.assertFalse(final.admission.base.core.state.control.enabled)
        self.assertEqual(final.admission.reservation,held.reservation)
        self.assertEqual(final.admission.base.authorization,held.base.authorization)
        self.assertEqual(final.admission.base.core.state.control.daily_quota,held.base.core.state.control.daily_quota)
        self.assertEqual(final.admission.base.core.generation,9)
        self.assertEqual(self.metrics(),dict(db_requests=34,successful_commits=8,positive_acks=8,
            local_continuations=1,admitted_transport_entries=1,physical_fake_requests=1,fake_effects=1))
        self.trace('complete_vertical_after_stop')

    def test_create_adds_only_two_fields_and_generation_preserving_original_eight(self):
        before=self.to_bound();old=before.item();self.approve_effect()
        self.assertTrue(self.create().accepted)
        after=self.current();item=after.item()
        self.assertEqual(set(item)-set(old),{'recovery_schema','effect'})
        for name in old:
            self.assertEqual(item[name],engine._av(4) if name=='generation' else old[name],name)
        self.assertEqual(after.effect.phase,recovery.NOT_STARTED)
        self.assertIsNone(after.effect.attempt)
        self.assertEqual(after.effect.definition.intent_reserved_usd,before.reservation.run_reserve_usd)
        self.assertEqual((self.continuation_count,self.provider.entries),(0,0))

    def test_real_plan_four_over_two_still_denied_before_reservation(self):
        config=worker_configuration.WORKER_CONFIG
        self.assertEqual((config.research_reports_per_question,config.predictions_per_research_report,
                          config.agent_requests_per_atomic_question,MAX_AUTO_AGENT_POST_ATTEMPTS),(2,3,4,2))
        self.assertFalse(self.reserve(proof=replace(self.proof,plan=None)).accepted)
        self.assertEqual((len(self.writes()),self.raw.commits,self.ack_count),(0,0,0))
        self.assertIsNone(self.current().reservation)
        self.trace('real_four_over_two_denied')

    def test_cannot_create_from_bootstrap_reserved_only_or_already_owned(self):
        payload=dict(intent_id='f'*64,approval_epoch=3,expected_revision=self.initial.revision)
        self.assertFalse(self.create(payload=payload).accepted)
        self.assertTrue(self.reserve().accepted)
        self.assertFalse(self.create().accepted)
        self.approve_run();self.assertTrue(self.bind().accepted)
        self.assertTrue(self.claim().claim_confirmed)  # Existing pre-create claim remains valid.
        self.approve_effect();self.assertFalse(self.create().accepted)
        self.assertEqual(self.raw.commits,3)
        self.assertEqual(self.provider.entries,0)

    def test_two_controllers_creating_one_effect_get_one_ack(self):
        self.to_bound();self.approve_effect()
        other=self.auth.controller_invocation();self.approve_effect(context=other)
        payload=self.bind_payload();barrier=Barrier(2)
        self.raw.before_update=lambda _request:barrier.wait(timeout=4)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda ctx:self.create(payload=payload,invocation=ctx),[self.controller,other]))
        self.assertEqual(sum(r.accepted for r in results),1)
        self.assertEqual((self.raw.commits,self.ack_count,len(self.writes())),(3,3,4))
        self.assertEqual(self.current().admission.base.core.generation,4)

    def test_two_workers_have_one_owner_and_one_fake_send(self):
        self.to_created();payload=self.bind_payload();barrier=Barrier(2)
        other=self.auth.worker_invocation(self.token(jti='other-token'))
        self.raw.before_update=lambda _request:barrier.wait(timeout=4)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda ctx:self.claim(payload=payload,invocation=ctx),[self.worker,other]))
        self.assertEqual(sum(r.claim_confirmed for r in results),1)
        self.raw.before_update=None
        self.assertTrue(self.attempt().simulated_provider_entered)
        self.assert_no_second_send()
        self.assertEqual((self.continuation_count,self.provider.entries,len(self.provider.effects)),(1,1,1))

    def test_same_run_new_run_rerun_and_repeat_create_never_replace_slot(self):
        self.to_created();after=self.current()
        for context in (self.worker,self.auth.worker_invocation(self.token(run_id='999002')),
                        self.auth.worker_invocation(self.token(run_attempt='2'))):
            if context is self.worker:self.assertTrue(self.claim(invocation=context).claim_confirmed)
            else:self.assertFalse(self.claim(invocation=context).claim_confirmed)
        self.assertFalse(self.create().accepted)
        self.assertFalse(self.bind().accepted)
        self.assertEqual(self.current().effect,after.effect)
        self.assertEqual(self.current().admission.reservation,after.admission.reservation)

    def test_caller_cannot_select_definition_provider_payload_cap_or_second_slot(self):
        self.to_bound();self.approve_effect();payload=self.bind_payload()
        for field in ('definition','provider_id','kind','semantic_key','payload','request_cap_usd',
                      'operation_id','verified','owner','plan','table_name','generation','effect','second_effect'):
            self.assertFalse(self.create(payload={**payload,field:True}).accepted,field)
        self.assertEqual(self.raw.commits,2)
        for name in ('create_effects','reset','refund','release','restore','re_enable','compare_and_swap'):
            self.assertFalse(self.invoke(name,{}).accepted)

    def test_wrong_ingress_role_unregistered_context_or_missing_authority_denies(self):
        self.to_bound();self.approve_effect()
        for context in (self.worker,self.operator,object(),{'verified':True}):
            self.assertFalse(self.create(invocation=context).accepted)
        for options in ({'authentication':None},{'transport':None},{'clock':None}):
            self.assertFalse(self.create(gateway=self.restart(**options)).accepted)
        self.auth.effect_approval_available=False
        self.assertFalse(self.create().accepted)
        self.assertEqual(self.raw.commits,2)

    def test_create_checks_exact_jwt_identity_source_workflow_repo_owner_and_attempt(self):
        self.to_bound()
        for changes in [dict(run_id='999002'),dict(run_attempt='2'),dict(run_attempt=1),dict(sha='f'*40),
                        dict(workflow_sha='f'*40),dict(repository_id='11111'),dict(repository_owner_id='11111'),
                        dict(workflow_ref=self.policy.workflow_ref.replace('worker','other')),dict(aud='other')]:
            self.approve_effect(token=self.token(**changes));self.assertFalse(self.create().accepted)
        for token in ('not-jwt',{},self.token(key=self.other_key),self.token(kid='unknown')):
            self.approve_effect(token=token);self.assertFalse(self.create().accepted)
        self.assertEqual(self.raw.commits,2)

    def test_create_rejects_changed_policy_config_artifact_trust_evidence_and_definition(self):
        self.to_bound();definition=created.fixed_definition(self.current())
        for changes in [dict(expected_revision='wrong'),dict(evidence_id='f'*32),dict(authorization_id='f'*32),
                        dict(trust_generation=2),dict(trust_generation=True),dict(configuration_digest='f'*64),dict(artifact_digest='f'*64),
                        dict(definition=replace(definition,payload='SYNTHETIC:other')),
                        dict(definition=replace(definition,request_cap_usd='0.20')),
                        dict(definition=replace(definition,run_id='999002'))]:
            self.approve_effect(**changes);self.assertFalse(self.create().accepted)
        self.assertEqual(self.raw.commits,2)

    def test_expiry_and_revocation_before_create_send_never_renew_authority(self):
        self.to_bound();self.approve_effect()
        self.assertFalse(self.create(gateway=self.restart(clock=lambda:NOW+timedelta(minutes=6))).accepted)
        self.auth.after_effect_verify=lambda:setattr(self.auth,'effect_approval_available',False)
        self.assertFalse(self.create().accepted)
        self.assertEqual(self.raw.commits,2)

    def test_fixed_cap_must_fit_reserved_run_and_create_never_changes_finance(self):
        spend=replace(self.readings.daily_spend,approved_run_reserve_usd='0.05')
        self.auth.reservations[self.controller]=replace(self.proof,readings=replace(self.readings,daily_spend=spend))
        self.to_bound();held=self.current()
        # Fixed 0.10 operation cannot fit 0.05; authority cannot manufacture it.
        with self.assertRaises(engine.StoreUnavailable):self.approve_effect()
        self.assertFalse(self.create().accepted)
        self.assertEqual(self.current(),held)
        self.assertEqual(held.base.core.state.control.daily_quota.reserved_usd,'0.35')

    def test_timeout_before_create_commit_and_no_automatic_retry(self):
        before=self.to_bound();self.approve_effect()
        self.raw.before_update=lambda _request:(_ for _ in ()).throw(TimeoutError())
        self.assertFalse(self.create().accepted)
        self.assertEqual(self.current(),before)
        self.assertEqual((len(self.writes()),self.raw.commits,self.ack_count),(3,2,2))

    def test_create_lost_ack_never_reconfirms_after_read_or_restart(self):
        self.to_bound();self.approve_effect();payload=self.bind_payload()
        self.raw.after_commit=lambda _request:(_ for _ in ()).throw(ConnectionResetError())
        self.assertFalse(self.create(payload=payload).accepted)
        self.assertIsInstance(self.current(),created.CreatedEffectSnapshot)
        self.trace('create_committed_lost_ack')
        self.raw.after_commit=None
        self.assertTrue(self.invoke('read_control',{},gateway=self.restart()).accepted)
        self.assertFalse(self.create(payload=payload,gateway=self.restart()).accepted)
        self.assertFalse(self.create(gateway=self.restart()).accepted)
        self.assertEqual((len(self.writes()),self.raw.commits,self.ack_count),(3,3,2))

    def test_incomplete_all_new_retry_metadata_and_changed_readback_withhold_create_ack(self):
        self.to_bound();self.approve_effect()
        self.raw.response_filter=lambda response:{**response,'Attributes':{'generation':engine._av(4)}}
        self.assertFalse(self.create().accepted)
        self.assertEqual((self.raw.commits,self.ack_count),(3,2))
        self.raw.response_filter=None
        self.assertFalse(self.create().accepted)

    def test_retry_attempts_metadata_one_is_not_accepted_as_unique_ack(self):
        self.to_bound();self.approve_effect()
        self.raw.response_filter=lambda r:{**r,'ResponseMetadata':{'HTTPStatusCode':200,'RetryAttempts':1}}
        self.assertFalse(self.create().accepted)
        self.assertEqual((self.raw.commits,self.ack_count),(3,2))

    def test_off_during_create_readback_rejects_confirmation_but_preserves_slot(self):
        self.to_bound();self.approve_effect()
        # Disable the fault hook before STOP's own write.
        def stop_after_create(_request):
            self.raw.after_commit=None
            self.assertTrue(self.stop().accepted)
        self.raw.after_commit=stop_after_create
        self.assertFalse(self.create().accepted)
        self.assertFalse(self.current().admission.base.core.state.control.enabled)
        self.assertEqual(self.current().effect.phase,recovery.NOT_STARTED)

    def test_delayed_original_cannot_commit_after_stop_or_replay_stale_create(self):
        self.to_bound();self.approve_effect();captured=[]
        def delay(request):
            captured.append(deepcopy(request));raise TimeoutError()
        self.raw.before_update=delay
        self.assertFalse(self.create().accepted)
        self.raw.before_update=None
        self.assertTrue(self.stop().accepted)
        with self.assertRaises(ModelError):self.raw.update_item(**captured[0])
        self.assertEqual(self.raw.commits,3)
        self.assertIsInstance(self.current(),admission.AdmissionSnapshot)

    def test_stop_before_create_after_create_and_before_attempt_preserves_all_evidence(self):
        self.to_bound();held=self.current();self.approve_effect()
        self.assertTrue(self.stop().accepted)
        self.assertFalse(self.create().accepted)
        self.assertEqual(self.current().reservation,held.reservation)
        self.assertEqual(self.current().base.authorization,held.base.authorization)

    def test_stop_after_create_preserves_unstarted_effect_without_claim_or_send(self):
        before=self.to_created()
        self.assertTrue(self.stop().accepted)
        self.assertEqual(self.current().effect,before.effect)
        self.assertEqual(self.current().admission.reservation,before.admission.reservation)
        self.assertFalse(self.claim().claim_confirmed)
        self.assertFalse(self.attempt().simulated_provider_entered)
        self.assertEqual(self.provider.entries,0)

    def test_stop_between_get_and_create_cas_is_an_atomic_conflict(self):
        held=self.to_bound();self.approve_effect()
        def stop(_request):
            self.raw.before_update=None
            self.assertTrue(self.stop().accepted)
        self.raw.before_update=stop
        self.assertFalse(self.create().accepted)
        self.assertIsInstance(self.current(),admission.AdmissionSnapshot)
        self.assertEqual(self.current().reservation,held.reservation)

    def test_stop_before_final_send_check_withholds_provider_but_spends_latch(self):
        self.to_owned();self.hooks['before_final_read']=lambda _:self.stop()
        result=self.attempt()
        self.assertTrue(result.attempt_commit_confirmed)
        self.assertFalse(result.simulated_provider_entered)
        self.assertEqual((self.continuation_count,self.provider.entries),(1,0))
        self.assert_no_second_send()

    def test_stop_after_final_check_can_allow_one_inflight_fake_send(self):
        self.to_owned();self.hooks['after_final_read']=lambda _:self.stop()
        result=self.attempt()
        self.assertTrue(result.simulated_provider_entered)
        self.assertFalse(self.current().admission.base.core.state.control.enabled)
        self.assertEqual((self.provider.entries,len(self.provider.effects)),(1,1))
        self.assert_no_second_send()
        self.trace('off_after_final_check_one_inflight_send')

    def test_each_original_eight_attribute_changed_without_generation_increment_conflicts(self):
        before=self.to_bound();self.approve_effect();original=before.item();payload=self.bind_payload()
        for field in original:
            self.raw.admin_replace(original)
            def mutate(_request,field=field):
                item=deepcopy(original)
                item[field]=engine._av(99) if field=='generation' else engine._av('mutated')
                self.raw.admin_replace(item)
            self.raw.before_update=mutate
            self.assertFalse(self.create(payload=payload).accepted,field)
        self.assertEqual(self.raw.commits,2)

    def test_partial_recovery_attributes_between_get_and_create_are_fenced_atomically(self):
        before=self.to_bound();self.approve_effect();old=before.item();payload=self.bind_payload()
        for field in ('recovery_schema','effect'):
            self.raw.admin_replace(old)
            def add(_request,field=field):
                item=deepcopy(old);item[field]=engine._av(None);self.raw.admin_replace(item)
            self.raw.before_update=add
            self.assertFalse(self.create(payload=payload).accepted)
        self.assertEqual(self.raw.commits,2)

    def test_unknown_ninth_attribute_cannot_produce_positive_create_ack(self):
        self.to_bound();self.approve_effect()
        def add(_request):
            item=self.raw.current();item['unmodeled']=engine._av(True);self.raw.admin_replace(item)
        self.raw.before_update=add
        self.assertFalse(self.create().accepted)
        self.assertEqual((self.raw.commits,self.ack_count),(3,2))  # Explicit privileged-schema counterexample.

    def test_unknown_eleventh_attribute_after_gate_commit_prevents_continuation(self):
        self.to_owned()
        def add_after_commit(_request):
            self.raw.after_commit=None
            item=self.raw.current();item['unmodeled']=engine._av(True);self.raw.admin_replace(item)
        self.raw.after_commit=add_after_commit
        result=self.attempt()
        self.assertFalse(result.attempt_commit_confirmed)
        self.assertEqual((self.continuation_count,self.provider.entries),(0,0))

    def test_all_ten_fields_are_in_claim_and_gate_conditions_including_whole_reservation(self):
        self.to_owned();self.assertTrue(self.attempt().simulated_provider_entered)
        for request in self.writes()[3:]:
            names=request['ExpressionAttributeNames'];condition=request['ConditionExpression']
            compared={field for alias,field in names.items() if alias+' = ' in condition}
            self.assertTrue(created.CREATED_FIELDS.issubset(compared),compared)
        self.assertTrue(all(r['ConsistentRead'] is True for name,r in self.raw.calls if name=='GetItem'))
        self.assertTrue(all(r['ReturnValues']=='ALL_NEW' for r in self.writes()))

    def test_reservation_or_effect_definition_mutation_between_read_and_gate_is_rejected(self):
        before=self.to_owned();old=before.item()
        for field in ('reservation','effect','document','run_authorization'):
            self.raw.admin_replace(old)
            def mutate(_request,field=field):
                item=deepcopy(old);item[field]=engine._av(None);self.raw.admin_replace(item)
            self.raw.before_update=mutate
            self.assertFalse(self.attempt().attempt_commit_confirmed)
        self.assertEqual((self.continuation_count,self.provider.entries),(0,0))

    def test_old_decoders_reject_new_envelope_and_new_decoder_rejects_old_shapes(self):
        before=self.to_created();item=before.item()
        with self.assertRaises(engine.StoreUnavailable):admission.AdmissionSnapshot.decode(item,self.binding)
        with self.assertRaises(engine.StoreUnavailable):recovery.RecoverySnapshot.decode(item,self.binding)
        with self.assertRaises(engine.StoreUnavailable):command.CommandSnapshotDraft.decode(item,self.binding)
        for old in (before.admission.item(),before.kernel.item(),before.admission.base.item()):
            with self.assertRaises(engine.StoreUnavailable):created.CreatedEffectSnapshot.decode(old,self.binding)

    def test_old_four_six_and_both_eight_field_writers_cannot_mutate_hidden_new_envelope(self):
        before=self.to_created();raw=self.raw
        class HiddenFields:
            def __init__(self,fields):self.fields=fields
            def get_item(self,**request):
                response=raw.get_item(**request)
                return {**response,'Item':{k:v for k,v in response['Item'].items() if k in self.fields}}
            def update_item(self,**request):return raw.update_item(**request)
        base=before.admission.base;core=base.core
        owner=command._policy_owner(base.authorization.policy)
        claim=v3.WorkerClaim(base.authorization.intent_id,3,command.server_request_id(self.binding,base.authorization),
                             owner,base.authorization.approved_at)
        proposal=replace(core.state,worker_claim=claim)
        old4=engine.DynamoConditionalStorePrototype(transport=HiddenFields({'pk','incarnation','generation','document'}),binding=self.binding)
        self.assertFalse(old4.compare_and_swap(core.revision,proposal).confirmed)
        payload=dict(intent_id=base.authorization.intent_id,approval_epoch=3,expected_revision=base.revision)
        old6=command.OfflineCommandGateway(transport=HiddenFields(admission._BASE_FIELDS),binding=self.binding,
                                          authentication=self.auth,clock=lambda:NOW)
        self.assertFalse(old6.handle('claim_reserved_intent',payload,invocation=self.worker).accepted)
        old8=admission.OfflineAdmissionGateway(transport=HiddenFields(created.ADMISSION_FIELDS),binding=self.binding,
                                             authentication=self.auth,clock=lambda:NOW)
        self.assertFalse(old8.handle('claim_reserved_intent',{**payload,'expected_revision':before.admission.revision},invocation=self.worker).accepted)
        old_recovery=recovery.OfflineEffectRecoveryGateway(transport=HiddenFields(created.RECOVERY_FIELDS),binding=self.binding,
                                                          authentication=self.auth,clock=lambda:NOW)
        self.assertFalse(old_recovery.handle('emergency_stop',dict(expected_revision=before.kernel.revision),invocation=self.operator).accepted)
        self.assertEqual(self.current(),before)
        self.assertEqual(self.raw.commits,3)

    def test_stale_bound_get_after_create_cannot_create_second_slot_or_ack(self):
        bound=self.to_bound();self.approve_effect();payload=self.bind_payload()
        self.assertTrue(self.create().accepted)
        self.raw.read_filter=lambda response:{**response,'Item':bound.item()}
        self.assertFalse(self.create(payload=payload,gateway=self.restart()).accepted)
        self.assertEqual((self.raw.commits,self.ack_count),(3,3))

    def test_lost_gate_ack_restart_does_not_issue_continuation_or_send(self):
        self.to_owned()
        self.raw.after_commit=lambda _request:(_ for _ in ()).throw(TimeoutError())
        self.assertFalse(self.attempt().attempt_commit_confirmed)
        self.trace('gate_committed_lost_ack_before_restart')
        self.raw.after_commit=None
        self.assert_no_second_send()
        self.assertEqual((self.continuation_count,self.provider.entries),(0,0))
        self.trace('gate_committed_lost_ack_after_restart')

    def test_crash_after_gate_ack_before_send_keeps_permanent_latch(self):
        self.to_owned()
        self.hooks['gate_confirmed']=lambda _:(_ for _ in ()).throw(crypto_fixture.ProcessInterrupted())
        with self.assertRaises(crypto_fixture.ProcessInterrupted):self.attempt()
        self.assertEqual((self.continuation_count,self.provider.entries),(1,0))
        self.hooks.clear();self.assert_no_second_send()

    def test_crash_after_fake_effect_cannot_repeat_on_restart(self):
        self.to_owned()
        self.hooks['after_provider']=lambda _:(_ for _ in ()).throw(crypto_fixture.ProcessInterrupted())
        with self.assertRaises(crypto_fixture.ProcessInterrupted):self.attempt()
        self.assertEqual(self.current().effect.phase,recovery.ATTEMPT_COMMITTED)
        self.hooks.clear();self.assert_no_second_send()
        self.assertEqual((self.provider.entries,len(self.provider.effects)),(1,1))

    def test_billing_unknown_or_duplicate_effect_never_refunds_or_reopens_slot(self):
        self.to_owned();held=self.current().admission.reservation
        self.provider.error=TimeoutError
        self.assertTrue(self.attempt().attempt_commit_confirmed)
        self.provider.billing_available=False
        self.assertFalse(self.reconcile().outcome_independently_verified)
        self.assertEqual(self.current().effect.phase,recovery.OUTCOME_UNKNOWN)
        self.assertEqual(self.current().admission.reservation,held)
        self.assert_no_second_send()

    def test_hidden_provider_retry_is_an_explicit_tcb_counterexample(self):
        self.to_owned();self.provider.physical_attempts_per_call=2
        self.assertTrue(self.attempt().simulated_provider_entered)
        self.assertEqual((self.continuation_count,self.provider.entries,self.provider.wire_attempts,len(self.provider.effects)),(1,1,2,2))
        self.assertFalse(self.reconcile().outcome_independently_verified)
        self.trace('invalid_provider_hidden_retry_two_effects')
        self.assert_no_second_send()

    def test_privileged_same_incarnation_rollback_allows_duplicate_effect_counterexample(self):
        before=self.to_created();self.claim();self.attempt()
        self.raw.admin_replace(before.item())  # Explicit TCB violation, not proposed recovery.
        self.assertTrue(self.claim().claim_confirmed)
        self.assertTrue(self.attempt().simulated_provider_entered)
        self.assertEqual((self.continuation_count,self.provider.entries,len(self.provider.effects)),(2,2,2))
        self.trace('invalid_privileged_snapshot_rollback_two_effects')

    def test_new_protected_incarnation_rejects_restored_old_item(self):
        before=self.to_created();self.claim();self.attempt()
        self.raw.admin_replace(before.item())
        binding=replace(self.binding,incarnation='b'*32)
        auth=SyntheticCreateAuthority(binding,keys=(),now=NOW);ctx=auth.controller_invocation()
        gateway=self.restart(binding=binding,authentication=auth)
        self.assertFalse(gateway.handle('read_control',{},invocation=ctx).accepted)
        self.assertEqual(len(self.provider.effects),1)

    def test_raw_worker_writer_is_rejected_and_raw_privileged_restore_is_tcb(self):
        self.to_created();self.raw.principal='worker'
        self.assertFalse(self.invoke('read_control',{}).accepted)
        with self.assertRaises(ModelError):self.raw.update_item(UpdateExpression='SET generation = :x')
        self.assertEqual(self.raw.commits,3)

    def test_hidden_db_retry_has_one_commit_no_confirmed_create_ack(self):
        self.to_bound();self.approve_effect();raw=self.raw
        raw.after_commit=lambda _request:(_ for _ in ()).throw(ConnectionResetError())
        class Retrying:
            def get_item(self,**request):return raw.get_item(**request)
            def update_item(self,**request):
                try:return raw.update_item(**request)
                except ConnectionResetError:return raw.update_item(**request)
        self.assertFalse(self.create(gateway=self.restart(transport=Retrying())).accepted)
        self.assertEqual((len(self.writes()),self.raw.commits,self.ack_count),(4,3,2))
        self.trace('invalid_db_hidden_retry_no_create_ack')

    def test_replayed_create_success_two_acks_one_commit_is_explicit_counterexample(self):
        self.to_bound();self.approve_effect();raw=self.raw
        barrier=Barrier(2);lock=Lock();cache=[];payload=self.bind_payload()
        class Replayed:
            def get_item(self,**request):return raw.get_item(**request)
            def update_item(self,**request):
                barrier.wait(timeout=4)
                with lock:
                    if not cache:cache.append(deepcopy(raw.update_item(**request)))
                    return deepcopy(cache[0])
        gateway=self.restart(transport=Replayed())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.create(payload=payload,gateway=gateway),range(2)))
        self.assertEqual(sum(r.accepted for r in results),2)
        self.assertEqual((self.raw.commits,self.ack_count),(3,4))
        self.assertEqual((self.continuation_count,self.provider.entries),(0,0))
        self.trace('invalid_replayed_create_ack_two_confirmations_one_commit')

    def test_headroom_for_stop_at_create_is_preserved(self):
        initial=replace(self.initial,base=replace(self.initial.base,
            core=replace(self.initial.base.core,generation=engine.MAX_GENERATION-3)))
        self.raw.admin_replace(initial.item());self.initial=initial
        self.proof=replace(self.proof,expected_revision=initial.revision)
        self.auth.reservations[self.controller]=self.proof
        self.to_bound();self.approve_effect()
        self.assertFalse(self.create().accepted)
        self.assertTrue(self.stop().accepted)
        self.assertEqual(self.current().base.core.generation,engine.MAX_GENERATION)

    def test_headroom_for_stop_at_gate_is_preserved(self):
        initial=replace(self.initial,base=replace(self.initial.base,
            core=replace(self.initial.base.core,generation=engine.MAX_GENERATION-5)))
        self.raw.admin_replace(initial.item());self.initial=initial
        self.proof=replace(self.proof,expected_revision=initial.revision)
        self.auth.reservations[self.controller]=self.proof
        self.to_owned()
        self.assertFalse(self.attempt().attempt_commit_confirmed)
        self.assertEqual((self.continuation_count,self.provider.entries),(0,0))
        self.assertTrue(self.stop().accepted)
        self.assertEqual(self.current().admission.base.core.generation,engine.MAX_GENERATION)

    def test_two_gate_contenders_have_one_continuation_and_one_physical_fake_request(self):
        self.to_owned();payload={**self.bind_payload(),'operation_id':self.current().effect.operation_id}
        other=self.auth.worker_invocation(self.token(jti='other-gate-token'));barrier=Barrier(2)
        def synchronize_only_gate(request):
            phase=request['ExpressionAttributeValues'].get(':after',{}).get('M',{}).get('phase',{}).get('S')
            if phase==recovery.ATTEMPT_COMMITTED:barrier.wait(timeout=4)
        self.raw.before_update=synchronize_only_gate
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda ctx:self.attempt(payload=payload,invocation=ctx),[self.worker,other]))
        self.assertEqual(sum(r.attempt_commit_confirmed for r in results),1)
        self.assertEqual((self.continuation_count,self.provider.entries,self.provider.wire_attempts,len(self.provider.effects)),(1,1,1,1))
        self.trace('concurrent_gate_one_continuation_one_effect')

    def test_stale_unspent_ten_field_get_after_effect_cannot_restore_permission(self):
        before=self.to_owned();payload={**self.bind_payload(),'operation_id':before.effect.operation_id}
        self.assertTrue(self.attempt().simulated_provider_entered)
        self.raw.read_filter=lambda response:{**response,'Item':before.item()}
        self.assertFalse(self.attempt(payload=payload,gateway=self.restart()).attempt_commit_confirmed)
        self.assertEqual((self.continuation_count,self.provider.entries,len(self.provider.effects)),(1,1,1))

    def test_projection_cannot_remove_a_missing_fence_without_exact_pinning(self):
        request=dict(ConditionExpression='#g = :g',ExpressionAttributeNames={'#g':'generation'},
                     ExpressionAttributeValues={':g':engine._av(1)})
        with self.assertRaises(engine.StoreUnavailable):engine._pin_envelope(request,{'effect':engine._av(None)})
        with self.assertRaises(engine.StoreUnavailable):engine._pin_envelope(request,{'document':engine._av(None)})
        fenced=engine._fence_envelope(request,{'pk','incarnation','generation','document'})
        pinned=engine._pin_envelope(fenced,{'effect':engine._av(None)})
        self.assertIn('#shape_effect = :shape_effect',pinned['ConditionExpression'])
        self.assertNotIn('attribute_not_exists(#shape_effect)',pinned['ConditionExpression'])
        self.assertIn('attribute_not_exists(#shape_reservation)',pinned['ConditionExpression'])

    def test_replayed_gate_ack_can_duplicate_effect_under_explicit_transport_tcb_violation(self):
        self.to_owned();raw=self.raw;lock=Lock();cache=[]
        ready,confirmed,final_reads=Barrier(2),Barrier(2),Barrier(2)
        payload={**self.bind_payload(),'operation_id':self.current().effect.operation_id}
        class ReplayedGate:
            def get_item(self,**request):return raw.get_item(**request)
            def update_item(self,**request):
                phase=request['ExpressionAttributeValues'].get(':after',{}).get('M',{}).get('phase',{}).get('S')
                if phase!=recovery.ATTEMPT_COMMITTED:return raw.update_item(**request)
                ready.wait(timeout=4)
                with lock:
                    if not cache:cache.append(deepcopy(raw.update_item(**request)))
                    return deepcopy(cache[0])
        self.hooks['gate_confirmed']=lambda _:confirmed.wait(timeout=4)
        self.hooks['after_final_read']=lambda _:final_reads.wait(timeout=4)
        gateway=self.restart(transport=ReplayedGate())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.attempt(payload=payload,gateway=gateway),range(2)))
        self.assertEqual(sum(r.attempt_commit_confirmed for r in results),2)
        self.assertEqual((self.continuation_count,self.provider.entries,len(self.provider.effects)),(2,2,2))
        self.assertEqual((self.raw.commits,self.ack_count),(6,7))
        self.trace('invalid_replayed_gate_ack_two_continuations_two_effects')

    def test_malformed_commands_and_old_recovery_shape_do_not_auto_upgrade(self):
        for name in (None,True,[],{},'unknown','create_fixed_effect'):
            for payload in ({},None,{'verified':True}):
                self.assertFalse(self.gateway.handle(name,payload,invocation=self.controller).accepted)
        self.to_created();before=self.current()
        self.raw.admin_replace(before.kernel.item())  # Deliberate wrong-schema fault.
        self.assertFalse(self.invoke('read_control',{}).accepted)

    def test_fresh_subprocess_cannot_restore_send_after_spent_gate(self):
        self.to_owned();self.raw.after_commit=lambda _:(_ for _ in ()).throw(TimeoutError())
        self.attempt();self.raw.after_commit=None
        script='''
import json,sys
from datetime import datetime
from test_autonomous_runtime_offline import reject_network,NETWORK_ATTEMPTS
sys.addaudithook(reject_network)
from test_create_effect_offline import SyntheticCreateAuthority
from fake_dynamodb_control import SyntheticDynamoTransport
from create_effect_offline import OfflineCreatedEffectGateway,CreatedEffectSnapshot
from dynamodb_cas_offline import AuthorityBinding
d=json.load(sys.stdin);b=AuthorityBinding(**d['binding']);raw=SyntheticDynamoTransport(d['path']);now=datetime.fromisoformat(d['now'])
a=SyntheticCreateAuthority(b,keys=(),now=now);w=a.worker_invocation('no-reconstruction-from-token')
s=CreatedEffectSnapshot.decode(raw.current(),b)
g=OfflineCreatedEffectGateway(transport=raw,binding=b,authentication=a,clock=lambda:now)
r=g.handle('simulate_external_attempt',dict(intent_id=s.effect.definition.intent_id,approval_epoch=3,operation_id=s.effect.operation_id,expected_revision=s.revision),invocation=w)
print(json.dumps(dict(accepted=r.accepted,continuation=r.attempt_commit_confirmed,entered=r.simulated_provider_entered,execution_allowed=r.execution_allowed,writes=sum(n=='UpdateItem' for n,_ in raw.calls),network_attempts=NETWORK_ATTEMPTS)))
'''
        root=Path(__file__).resolve().parents[1]
        env={k:v for k,v in os.environ.items() if not k.endswith(('_TOKEN','_API_KEY','_SECRET'))}
        env['PYTHONPATH']=str(root/'tests')+os.pathsep+str(root)
        result=subprocess.run([sys.executable,'-B','-c',script],input=json.dumps(dict(
            binding=asdict(self.binding),path=self.path,now=NOW.isoformat())),capture_output=True,text=True,
            check=True,timeout=5,cwd=root,env=env)
        self.assertEqual(json.loads(result.stdout),dict(accepted=False,continuation=False,entered=False,
            execution_allowed=False,writes=0,network_attempts=[]))

    def test_production_anchors_plan_and_external_execution_remain_denied(self):
        self.assertIs(v3.CONTROLLER_STORE,None)
        self.assertIs(worker_identity.CONTROLLER_VERIFIER,None)
        self.assertIs(command.PRODUCTION_COMMAND_GATEWAY,None)
        self.assertIs(recovery.PRODUCTION_EFFECT_RECOVERY,None)
        self.assertEqual(worker_configuration.WORKER_CONFIG.agent_requests_per_atomic_question,4)
        self.assertEqual(MAX_AUTO_AGENT_POST_ATTEMPTS,2)
        self.assertEqual(engine.sdk_config_requirements()['retries']['total_max_attempts'],1)


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(IntegratedCreateEffectTests))
    print('Unexpected network attempts:',NETWORK_ATTEMPTS)
    raise SystemExit(0 if result.wasSuccessful() and not NETWORK_ATTEMPTS else 1)

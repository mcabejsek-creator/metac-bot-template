"""Reserve/bind from an explicitly unreserved ON bootstrap; no provider calls.

The registration service below is an independent SYNTHETIC authority in the
test model, not authentication by a class name/boolean in a worker payload.
Existing SQLite/Dynamo fault model and RSA OIDC verifier are reused unchanged.
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
import tempfile
from threading import Barrier, Lock
import unittest

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __package__:
    from . import test_command_gateway_offline as crypto_fixture
    from .fake_command_auth import SyntheticCommandAuthentication, SyntheticKeySnapshot
    from .fake_effect_recovery import MeteredSyntheticDynamo, NonIdempotentSyntheticProvider
    from .fake_dynamodb_control import ModelError
    from .test_worker_claim_offline import NOW
    from .test_autonomous_runtime_offline import reject_network, NETWORK_ATTEMPTS
else:
    import test_command_gateway_offline as crypto_fixture
    from fake_command_auth import SyntheticCommandAuthentication, SyntheticKeySnapshot
    from fake_effect_recovery import MeteredSyntheticDynamo, NonIdempotentSyntheticProvider
    from fake_dynamodb_control import ModelError
    from test_worker_claim_offline import NOW
    from test_autonomous_runtime_offline import reject_network, NETWORK_ATTEMPTS

if __name__ == '__main__':
    sys.addaudithook(reject_network)

import reserve_bind_offline as admission
import command_gateway_offline as command
import dynamodb_cas_offline as engine
import durable_effect_offline as recovery
import worker_claim_offline as v3
import worker_configuration
import worker_identity
from autonomous_orchestrator import IndependentReadings
from dispatch_intent_controller import ControllerState, DailyQuota, _iso, disable_controller
from perplexity_run_budget import MAX_AUTO_AGENT_POST_ATTEMPTS
from scheduler_policy import DailySpendEvidence


TRACE_EVIDENCE = []  # Diagnostics only; never consulted for admission.


class SyntheticAdmissionAuthority(SyntheticCommandAuthentication):
    """Read-only approval registrations outside command payloads.

    This extends existing synthetic ingress. It records no ownership, quota,
    consumed-approval bit or retry permission: the ONE SQLite item does that.
    A production equivalent and real artifact attestations do not exist.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reservations, self.runs = {}, {}
        self.reservation_available = self.run_approval_available = True
        self.after_reservation_verify = self.after_run_verify = None

    def reservation(self, invocation, snapshot, *, now):
        if self.reader(invocation, self._binding, now=now).kind != 'controller' or not self.reservation_available:
            raise RuntimeError('Independent synthetic reservation unavailable')
        proof = self.reservations[invocation]
        if proof.expected_revision != snapshot.revision:
            raise RuntimeError('Approval does not belong to current whole state')
        if self.after_reservation_verify:
            self.after_reservation_verify()
        return proof

    def approved_run(self, invocation, snapshot, *, now):
        if self.reader(invocation, self._binding, now=now).kind != 'controller' or not self.run_approval_available:
            raise RuntimeError('Independent synthetic run approval unavailable')
        facts, token = self.runs[invocation]
        auth = facts['authorization']
        keys = self.keys.get(auth.trust_generation)
        if not self.worker_keys_available or keys is None or not keys.not_before <= now < keys.not_after:
            raise RuntimeError('Independent trust generation unavailable')
        identity = worker_identity.GitHubActionsOIDCVerifier(auth.policy, keys.public_keys).verify(token, now=now)
        proof = admission.SyntheticApprovedRun(identity=identity, **facts)
        if self.after_run_verify:
            self.after_run_verify()
        return proof


class ReserveBindOfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        crypto_fixture.CommandGatewayOfflineTests.setUpClass.__func__(cls)

    token = crypto_fixture.CommandGatewayOfflineTests.token

    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory(prefix='offline-reserve-bind-'))
        self.path = str(Path(self.directory) / 'control.sqlite')
        self.binding = engine.AuthorityBinding('offline-control', 'controller', 'a'*32,
                                               'offline://account/region/table')
        # This is the ONLY bootstrap: ON/epoch/quota, NO pending, owner or run.
        control = ControllerState(enabled=True, approval_epoch=3,
                                  daily_quota=DailyQuota(NOW.date().isoformat(), 0, '0.20'))
        self.initial = admission.AdmissionSnapshot(command.CommandSnapshotDraft(
            engine.Snapshot(v3.OfflineControlStateV3(control), 1, self.binding), None), None)
        self.raw = MeteredSyntheticDynamo.seed(self.path, self.initial.item())
        keys = SyntheticKeySnapshot(1, (('synthetic-command-kid', self.pem),),
                                    NOW-timedelta(minutes=1), NOW+timedelta(minutes=10))
        self.auth = SyntheticAdmissionAuthority(self.binding, keys=(keys,), now=NOW)
        self.controller = self.auth.controller_invocation()
        self.operator = self.auth.operator_invocation()
        self.worker = self.auth.worker_invocation(self.token())
        self.plan = admission.SyntheticAtomicityPlan()
        self.scope = admission.SyntheticCodeScope(**{
            name: getattr(self.policy, name) for name in self.policy.__dataclass_fields__ if name != 'run_id'},
            configuration_digest=self.plan.configuration_digest, artifact_digest='e'*64)
        spend = DailySpendEvidence(NOW.date(), NOW, '0.30', '2.00', '0.40', 0, 3, True)
        self.readings = IndependentReadings(NOW, 0, 0, NOW, True, NOW, True, spend, NOW, True, True, True, True)
        self.proof = admission.SyntheticReservationEvidence(self.initial.revision, 3, 'c'*32,
            _iso(NOW), _iso(NOW+timedelta(minutes=5)), 1, self.readings, self.scope, self.plan)
        self.auth.reservations[self.controller] = self.proof
        self.provider = NonIdempotentSyntheticProvider()  # Never passed to admission gateway.
        self.positive_acks = 0
        self.gateway = self.restart()

    def restart(self, **changes):
        options = dict(transport=self.raw, binding=self.binding, authentication=self.auth, clock=lambda: NOW)
        options.update(changes)
        return admission.OfflineAdmissionGateway(**options)

    def current(self):
        # Privileged inspection, not an API grant/readback in the protocol trace.
        return admission.AdmissionSnapshot.decode(self.raw.current(), self.binding)

    def writes(self):
        return [request for name, request in self.raw.calls if name == 'UpdateItem']

    def invoke(self, name, payload, invocation=None, gateway=None):
        result = (gateway or self.gateway).handle(name, payload,
            invocation=self.controller if invocation is None else invocation)
        self.assertFalse(result.execution_allowed)
        self.assertFalse(result.retry_allowed)
        if result.accepted and name != 'read_control':
            self.positive_acks += 1
        return result

    def reserve(self, *, proof=None, payload=None, **kwargs):
        if proof is not None:
            self.auth.reservations[self.controller] = proof
        return self.invoke('reserve_intent', payload if payload is not None else
            dict(approval_epoch=3, expected_revision=self.initial.revision), **kwargs)

    def approve_run(self, *, context=None, policy=None, token=None, **changes):
        before = self.current()
        policy = self.policy if policy is None else policy
        auth = command.RunAuthorizationDraft('d'*32, before.reservation.intent_id, 3,
            _iso(NOW), _iso(NOW+timedelta(minutes=5)), 1, policy)
        facts = dict(expected_revision=before.revision, evidence_id=before.reservation.evidence_id,
                     authorization=auth, configuration_digest=self.scope.configuration_digest,
                     artifact_digest=self.scope.artifact_digest)
        facts.update(changes)
        self.auth.runs[self.controller if context is None else context] = (facts, self.token() if token is None else token)
        return facts

    def bind_payload(self):
        before = self.current()
        return dict(intent_id=before.reservation.intent_id, approval_epoch=3, expected_revision=before.revision)

    def bind(self, *, payload=None, **kwargs):
        return self.invoke('bind_approved_run', self.bind_payload() if payload is None else payload, **kwargs)

    def stop(self):
        return self.invoke('emergency_stop', dict(expected_revision=self.current().revision), invocation=self.operator)

    def metrics(self):
        # All seven counts, no resets, no authorization inferred from metrics.
        return dict(db_requests=len(self.raw.calls), successful_commits=self.raw.commits,
                    positive_acks=self.positive_acks, local_continuations=0,
                    admitted_transport_entries=self.provider.entries,
                    physical_fake_requests=self.provider.wire_attempts, fake_effects=len(self.provider.effects))

    def test_positive_reserve_bind_exact_after_state_and_seven_counts(self):
        self.assertIsNone(self.initial.base.core.state.control.pending)
        self.assertIsNone(self.initial.base.authorization)
        reserved = self.reserve()
        self.assertTrue(reserved.accepted)
        before_bind = self.current()
        self.assertEqual(before_bind.base.core.generation, 2)
        control = before_bind.base.core.state.control
        self.assertEqual(control.daily_quota, DailyQuota(NOW.date().isoformat(), 1, '0.70'))
        self.assertEqual(control.pending.reserved_at, _iso(NOW))
        self.assertEqual(before_bind.reservation.scope, self.scope)
        facts = self.approve_run()
        # Use the positive CAS ACK revision, not a reconstruction from GET.
        result = self.bind(payload=dict(intent_id=control.pending.intent_id, approval_epoch=3,
                                       expected_revision=reserved.revision))
        self.assertTrue(result.accepted)
        after = self.current()
        self.assertEqual(after.base.core.generation, 3)
        self.assertEqual(after.base.core.state, before_bind.base.core.state)
        self.assertEqual(after.reservation, before_bind.reservation)
        self.assertEqual(after.base.authorization, facts['authorization'])
        self.assertIsNone(after.base.core.state.worker_claim)
        self.assertEqual(self.metrics(),dict(db_requests=6,successful_commits=2,positive_acks=2,
            local_continuations=0,admitted_transport_entries=0,physical_fake_requests=0,fake_effects=0))
        TRACE_EVIDENCE.append(dict(case='unreserved_bootstrap_to_reserve_to_bind',**self.metrics()))

    def test_real_worker_plan_is_denied_before_any_write_without_replacing_config(self):
        actual = worker_configuration.WORKER_CONFIG
        self.assertEqual((actual.research_reports_per_question, actual.predictions_per_research_report,
                          actual.agent_requests_per_atomic_question, MAX_AUTO_AGENT_POST_ATTEMPTS), (2,3,4,2))
        self.assertFalse(self.reserve(proof=replace(self.proof, plan=None)).accepted)
        self.assertIs(worker_configuration.WORKER_CONFIG, actual)
        self.assertEqual((len(self.writes()),self.raw.commits), (0,0))
        self.assertEqual(self.current(),self.initial)
        TRACE_EVIDENCE.append(dict(case='real_worker_4_over_2_denied',**self.metrics()))

    def test_existing_stricter_v2_independent_source_clocks_and_flags_remain_required(self):
        for field in ('github_checked_at','funded_openrouter_checked_at','perplexity_checked_at','journal_checked_at'):
            for stamp in (None,NOW-timedelta(seconds=301),NOW+timedelta(seconds=1)):
                with self.subTest(field=field,stamp=stamp):
                    self.assertFalse(self.reserve(proof=replace(self.proof,
                        readings=replace(self.readings,**{field:stamp}))).accepted)
        for field in ('funded_openrouter_ok','perplexity_spend_ok','prior_publication_certain',
                      'operator_approval_confirmed','automation_switch_confirmed','is_first_attempt'):
            self.assertFalse(self.reserve(proof=replace(self.proof,
                readings=replace(self.readings,**{field:False}))).accepted)
        self.assertEqual(self.writes(),[])

    def test_v2_spend_daily_counts_ages_caps_and_active_runs_reject(self):
        spend = self.readings.daily_spend
        for changes in [dict(complete=False),dict(checked_at=NOW-timedelta(seconds=301)),
                        dict(checked_at=NOW+timedelta(seconds=1)),dict(day_utc=(NOW-timedelta(days=1)).date()),
                        dict(attempted_dispatches=1),dict(max_daily_dispatches=0),
                        dict(charged_usd='NaN'),dict(charged_usd='1.80'),dict(approved_run_reserve_usd='0'),
                        dict(approved_day_cap_usd='0.60'),dict(approved_run_reserve_usd='0.4000001')]:
            with self.subTest(changes=changes):
                self.assertFalse(self.reserve(proof=replace(self.proof,readings=replace(self.readings,
                    daily_spend=replace(spend,**changes)))).accepted)
        for changes in [dict(active_publishers=1),dict(queued_publishers=1),dict(active_publishers=True)]:
            self.assertFalse(self.reserve(proof=replace(self.proof,readings=replace(self.readings,**changes))).accepted)
        self.assertEqual(self.writes(),[])

    def test_persisted_reserve_floor_and_previous_spacing_cannot_be_erased(self):
        for control in [replace(self.initial.base.core.state.control,daily_quota=None),
                        replace(self.initial.base.core.state.control,daily_quota=DailyQuota('2026-11-08',0,'0')),
                        replace(self.initial.base.core.state.control,daily_quota=DailyQuota('2026-11-09',0,'1.80')),
                        replace(self.initial.base.core.state.control,last_slot=_iso(NOW),last_intent_at=_iso(NOW)),
                        replace(self.initial.base.core.state.control,last_slot=_iso(NOW-timedelta(minutes=15)),
                                last_intent_at=_iso(NOW-timedelta(minutes=10)))]:
            base = replace(self.initial.base,core=replace(self.initial.base.core,state=v3.OfflineControlStateV3(control)))
            item = admission.AdmissionSnapshot(base,None)
            self.raw.admin_replace(item.item())
            self.auth.reservations[self.controller] = replace(self.proof,expected_revision=item.revision)
            self.assertFalse(self.reserve(payload=dict(approval_epoch=3,expected_revision=item.revision)).accepted)
        self.assertEqual(self.writes(),[])

    def test_caller_payload_cannot_supply_evidence_plan_identity_scope_or_store(self):
        reserve_payload = dict(approval_epoch=3,expected_revision=self.initial.revision)
        for key in ('verified','plan','scope','claims','identity','owner','generation','document','table_name',
                    'pk','incarnation','authority_id','ConditionExpression','UpdateExpression','evidence','run_id'):
            self.assertFalse(self.reserve(payload={**reserve_payload,key:True}).accepted)
        self.assertTrue(self.reserve().accepted)
        self.approve_run()
        for key in ('verified','artifact_digest','configuration_digest','run_id','run_attempt','policy','trust_generation'):
            self.assertFalse(self.bind(payload={**self.bind_payload(),key:True}).accepted)
        self.assertEqual(len(self.writes()),1)

    def test_unregistered_or_wrong_role_and_missing_trust_cannot_authorize_commands(self):
        for context in (object(),True,{'verified':True},self.scope,self.worker,self.operator):
            self.assertFalse(self.reserve(invocation=context).accepted)
        for options in ({'authentication':None},{'transport':None},{'clock':None}):
            self.assertFalse(self.reserve(gateway=self.restart(**options)).accepted)
        self.auth.reservation_available=False
        self.assertFalse(self.reserve().accepted)
        self.assertEqual(self.writes(),[])

    def test_two_controllers_racing_reservation_have_one_positive_ack(self):
        other = self.auth.controller_invocation()
        self.auth.reservations[other]=self.proof
        barrier=Barrier(2)
        self.raw.before_update=lambda _request:barrier.wait(timeout=4)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda ctx:self.reserve(invocation=ctx),[self.controller,other]))
        self.assertEqual(sum(r.accepted for r in results),1)
        self.assertEqual((len(self.writes()),self.raw.commits),(2,1))
        self.assertEqual(self.current().base.core.state.control.daily_quota.attempts,1)
        self.assertEqual(self.current().base.core.generation,2)

    def test_two_differently_approved_runs_compete_for_one_immutable_binding(self):
        self.assertTrue(self.reserve().accepted)
        self.approve_run()
        other=self.auth.controller_invocation()
        other_policy=replace(self.policy,run_id='999002')
        self.approve_run(context=other,policy=other_policy,token=self.token(run_id='999002'))
        payload=self.bind_payload()
        barrier=Barrier(2)
        self.raw.before_update=lambda _request:barrier.wait(timeout=4)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda ctx:self.bind(payload=payload,invocation=ctx),[self.controller,other]))
        self.assertEqual(sum(r.accepted for r in results),1)
        self.assertIn(self.current().base.authorization.policy.run_id,('999001','999002'))
        self.assertEqual((len(self.writes()),self.raw.commits),(3,2))
        self.raw.before_update=None
        self.assertFalse(self.bind(payload=payload,invocation=other).accepted)
        self.assertFalse(self.bind(invocation=other).accepted)
        self.assertEqual(self.current().base.core.generation,3)

    def test_repeat_reserve_fresh_revision_and_restart_do_not_add_budget(self):
        self.assertTrue(self.reserve().accepted)
        after=self.current()
        self.assertFalse(self.reserve(gateway=self.restart()).accepted)
        proof=replace(self.proof,expected_revision=after.revision)
        self.assertFalse(self.reserve(proof=proof,payload=dict(approval_epoch=3,expected_revision=after.revision)).accepted)
        self.assertEqual(self.current(),after)
        self.assertEqual((len(self.writes()),self.raw.commits),(1,1))

    def test_repeat_bind_new_approval_and_new_run_never_replace_binding(self):
        self.reserve();self.approve_run()
        payload=self.bind_payload()
        self.assertTrue(self.bind(payload=payload).accepted)
        after=self.current()
        self.assertFalse(self.bind(payload=payload,gateway=self.restart()).accepted)
        self.approve_run(policy=replace(self.policy,run_id='999002'),token=self.token(run_id='999002'))
        self.assertFalse(self.bind().accepted)
        self.assertEqual(self.current(),after)

    def test_wrong_epoch_intent_and_wrong_approval_revision_do_not_write(self):
        self.assertFalse(self.reserve(payload=dict(approval_epoch=4,expected_revision=self.initial.revision)).accepted)
        self.assertFalse(self.reserve(proof=replace(self.proof,expected_revision='wrong')).accepted)
        self.reserve(proof=self.proof);self.approve_run()
        for changes in (dict(intent_id='f'*64),dict(approval_epoch=4),dict(expected_revision='wrong')):
            self.assertFalse(self.bind(payload={**self.bind_payload(),**changes}).accepted)
        self.assertEqual(len(self.writes()),1)

    def test_signed_wrong_run_repo_owner_source_workflow_attempt_and_forged_tokens_reject(self):
        self.reserve()
        for changes in [dict(run_id='999002'),dict(repository_id='11111'),dict(repository_owner_id='11111'),
                        dict(sha='f'*40),dict(workflow_sha='f'*40),dict(run_attempt='2'),dict(run_attempt=1),
                        dict(workflow_ref=self.policy.workflow_ref.replace('worker','other')),
                        dict(aud='other'),dict(iss='https://attacker.invalid')]:
            self.approve_run(token=self.token(**changes))
            self.assertFalse(self.bind().accepted)
        for token in (None,{},'unsigned',self.token(key=self.other_key),self.token(kid='unknown')):
            self.approve_run(token=token if token is not None else '')
            self.assertFalse(self.bind().accepted)
        self.assertEqual(len(self.writes()),1)

    def test_scope_config_artifact_and_trust_mutations_are_rejected(self):
        self.reserve()
        for changes in [dict(configuration_digest='f'*64),dict(artifact_digest='f'*64),dict(evidence_id='f'*32),
                        dict(expected_revision='wrong')]:
            self.approve_run(**changes)
            self.assertFalse(self.bind().accepted)
        for changes in [dict(source_sha='f'*40),dict(workflow_sha='f'*40),dict(repository_id='11111'),
                        dict(repository_owner_id='11111')]:
            policy=replace(self.policy,**changes)
            token_changes={'sha' if k=='source_sha' else k:v for k,v in changes.items()}
            self.approve_run(policy=policy,token=self.token(**token_changes))
            self.assertFalse(self.bind().accepted)
        facts=self.approve_run()
        self.approve_run(authorization=replace(facts['authorization'],trust_generation=2))
        self.assertFalse(self.bind().accepted)
        self.assertEqual(len(self.writes()),1)

    def test_no_bound_authority_renewal_after_expiry_or_day_rollover(self):
        self.reserve();self.approve_run()
        later=NOW+timedelta(minutes=6)
        self.assertFalse(self.bind(gateway=self.restart(clock=lambda:later)).accepted)
        facts=self.approve_run()
        self.approve_run(authorization=replace(facts['authorization'],valid_until=_iso(NOW+timedelta(minutes=6))))
        self.assertFalse(self.bind().accepted)
        self.assertFalse(self.bind(gateway=self.restart(clock=lambda:NOW+timedelta(days=1))).accepted)
        self.assertEqual(len(self.writes()),1)

    def test_approval_revocation_or_clock_change_immediately_before_send_denies(self):
        self.auth.after_reservation_verify=lambda:setattr(self.auth,'reservation_available',False)
        self.assertFalse(self.reserve().accepted)
        self.assertEqual(self.writes(),[])
        self.auth.after_reservation_verify=None;self.auth.reservation_available=True
        self.reserve();self.approve_run()
        self.auth.after_run_verify=lambda:setattr(self.auth,'run_approval_available',False)
        self.assertFalse(self.bind().accepted)
        self.assertEqual(len(self.writes()),1)

    def test_off_before_reserve_and_before_bind_never_releases_reservation(self):
        self.assertTrue(self.stop().accepted)
        self.assertFalse(self.reserve().accepted)
        self.assertEqual(self.current().base.core.state.control.daily_quota,self.initial.base.core.state.control.daily_quota)
        # A separate bootstrap for the after-reservation scenario, not re-enable.
        other=Path(self.directory)/'other.sqlite'
        self.raw=MeteredSyntheticDynamo.seed(str(other),self.initial.item());self.gateway=self.restart()
        self.reserve();held=self.current();self.approve_run()
        self.assertTrue(self.stop().accepted)
        self.assertFalse(self.bind().accepted)
        self.assertEqual(self.current().reservation,held.reservation)
        self.assertEqual(self.current().base.core.state.control.daily_quota,held.base.core.state.control.daily_quota)

    def test_off_between_read_and_reserve_cas_has_no_positive_ack(self):
        def off(_request):
            before=self.current()
            base=replace(before.base,core=replace(before.base.core,generation=before.base.core.generation+1,
                state=replace(before.base.core.state,control=disable_controller(before.base.core.state.control))))
            self.raw.admin_replace(replace(before,base=base).item())
        self.raw.before_update=off
        self.assertFalse(self.reserve().accepted)
        self.assertFalse(self.current().base.core.state.control.enabled)
        self.assertEqual(self.raw.commits,0)

    def test_off_between_read_and_bind_cas_preserves_held_finance(self):
        self.reserve();self.approve_run();held=self.current()
        def off(_request):
            base=replace(held.base,core=replace(held.base.core,generation=held.base.core.generation+1,
                state=replace(held.base.core.state,control=disable_controller(held.base.core.state.control))))
            self.raw.admin_replace(replace(held,base=base).item())
        self.raw.before_update=off
        self.assertFalse(self.bind().accepted)
        self.assertEqual(self.current().reservation,held.reservation)
        self.assertIsNone(self.current().base.authorization)

    def test_whole_item_condition_detects_every_field_changed_without_generation_increment(self):
        # Even a privileged mutation that FORGETS generation must conflict.
        original=self.raw.current()
        for field in original:
            self.raw.admin_replace(original)
            def mutate(_request,field=field):
                item=deepcopy(original)
                if field=='document':item[field]['M']['daily_quota']['M']['reserved_usd']={'S':'0.21'}
                elif field=='generation':item[field]={'N':'2'}
                elif field in ('reservation','run_authorization'):item[field]={'M':{}}
                else:item[field]={'S':'mutated'}
                self.raw.admin_replace(item)
            self.raw.before_update=mutate
            self.assertFalse(self.reserve().accepted,field)
        self.assertEqual(self.raw.commits,0)
        request=self.writes()[0]
        for alias,field in request['ExpressionAttributeNames'].items():
            if field in original:
                self.assertIn(alias+' =',request['ConditionExpression'])

    def test_bind_whole_item_cas_catches_quota_budget_epoch_intent_and_approval_mutations(self):
        self.reserve();self.approve_run()
        original=self.raw.current();payload=self.bind_payload()
        for field in ('daily_quota','pending','approval_epoch','run_authorization','reservation'):
            self.raw.admin_replace(original)
            def mutate(_request,field=field):
                item=deepcopy(original)
                if field=='daily_quota':item['document']['M'][field]['M']['attempts']={'N':'2'}
                elif field=='pending':item['document']['M'][field]['M']['intent_id']={'S':'f'*64}
                elif field=='approval_epoch':item['document']['M'][field]={'N':'4'}
                else:item[field]={'M':{}}
                self.raw.admin_replace(item)
            self.raw.before_update=mutate
            self.assertFalse(self.bind(payload=payload).accepted,field)
        self.assertEqual(self.raw.commits,1)

    def test_lost_reserve_ack_keeps_count_reserve_and_requires_independent_reconciliation(self):
        self.raw.after_commit=lambda _request:(_ for _ in ()).throw(TimeoutError())
        result=self.reserve()
        self.assertFalse(result.accepted);self.assertTrue(result.requires_reconciliation)
        self.assertEqual(self.metrics(),dict(db_requests=2,successful_commits=1,positive_acks=0,
            local_continuations=0,admitted_transport_entries=0,physical_fake_requests=0,fake_effects=0))
        TRACE_EVIDENCE.append(dict(case='reserve_commit_lost_ack',**self.metrics()))
        after=self.current()
        self.raw.after_commit=None
        self.assertFalse(self.reserve(gateway=self.restart()).accepted)
        self.assertEqual(self.current(),after)
        self.assertEqual(len(self.writes()),1)

    def test_lost_bind_ack_restart_and_readback_never_reconfirm(self):
        self.reserve();self.approve_run();payload=self.bind_payload()
        self.raw.after_commit=lambda _request:(_ for _ in ()).throw(ConnectionResetError())
        result=self.bind(payload=payload)
        self.assertFalse(result.accepted);self.assertTrue(result.requires_reconciliation)
        after=self.current();self.assertIsNotNone(after.base.authorization)
        self.raw.after_commit=None
        view=self.invoke('read_control',{},gateway=self.restart())
        self.assertTrue(view.accepted);self.assertIsNotNone(view.control['run_authorization'])
        self.assertFalse(self.bind(payload=payload,gateway=self.restart()).accepted)
        self.assertFalse(self.bind(gateway=self.restart()).accepted)
        self.assertEqual(self.current(),after)
        self.assertEqual((len(self.writes()),self.raw.commits,self.positive_acks),(2,2,1))

    def test_timeout_before_commit_never_retries_automatically(self):
        self.raw.before_update=lambda _request:(_ for _ in ()).throw(TimeoutError())
        result=self.reserve()
        self.assertFalse(result.accepted);self.assertTrue(result.requires_reconciliation)
        self.assertEqual(self.current(),self.initial)
        self.assertEqual((len(self.writes()),self.raw.commits),(1,0))

    def test_http_errors_and_incomplete_or_retrying_ack_cannot_confirm(self):
        for code in ('ConditionalCheckFailedException','ValidationException','AccessDeniedException',
                     'ResourceNotFoundException','InternalServerError','ThrottlingException'):
            self.raw.before_update=lambda _request,code=code:(_ for _ in ()).throw(ModelError(code))
            self.assertFalse(self.reserve().accepted)
        self.assertEqual(self.raw.commits,0)
        self.raw.before_update=None
        self.raw.response_filter=lambda response:{**response,'Attributes':{'generation':{'N':'2'}}}
        self.assertFalse(self.reserve().accepted)
        self.assertEqual(self.raw.commits,1)

    def test_retry_attempt_metadata_and_ack_whole_state_are_required_for_bind(self):
        self.reserve();self.approve_run()
        self.raw.response_filter=lambda response:{**response,'ResponseMetadata':{'HTTPStatusCode':200,'RetryAttempts':1}}
        self.assertFalse(self.bind().accepted)
        self.assertEqual(self.raw.commits,2)
        self.assertFalse(self.bind().accepted)
        self.assertEqual(len(self.writes()),2)

    def test_strong_get_and_all_new_ack_are_explicit_and_no_write_on_bad_read(self):
        self.raw.read_filter=lambda response:{**response,'ResponseMetadata':{'HTTPStatusCode':200}}
        self.assertFalse(self.reserve().accepted)
        self.assertEqual(self.writes(),[])
        self.raw.read_filter=None
        self.reserve()
        self.assertTrue(all(request['ConsistentRead'] is True for method,request in self.raw.calls if method=='GetItem'))
        self.assertTrue(all(request['ReturnValues']=='ALL_NEW' for request in self.writes()))
        self.assertEqual(engine.sdk_config_requirements()['retries']['total_max_attempts'],1)

    def test_stale_get_after_success_does_not_allow_another_commit_or_ack(self):
        stale=self.initial.item()
        self.assertTrue(self.reserve().accepted)
        self.raw.read_filter=lambda response:{**response,'Item':deepcopy(stale)}
        self.assertFalse(self.reserve(gateway=self.restart()).accepted)
        self.assertEqual((len(self.writes()),self.raw.commits,self.positive_acks),(2,1,1))

    def test_changed_readback_after_commit_is_uncertain_not_a_new_grant(self):
        reads=0
        def stale_on_readback(response):
            nonlocal reads
            reads+=1
            return response if reads==1 else {**response,'Item':self.initial.item()}
        self.raw.read_filter=stale_on_readback
        self.assertFalse(self.reserve().accepted)
        self.assertEqual((len(self.writes()),self.raw.commits,self.positive_acks),(1,1,0))

    def test_claim_and_stop_reuse_existing_gateway_without_changing_binding_or_quota(self):
        self.reserve();self.approve_run();self.bind();before=self.current()
        result=self.invoke('claim_reserved_intent',dict(intent_id=before.reservation.intent_id,
            approval_epoch=3,expected_revision=before.revision),invocation=self.worker)
        self.assertTrue(result.accepted)
        owned=self.current()
        self.assertEqual(owned.base.core.state.control,before.base.core.state.control)
        self.assertEqual(owned.base.authorization,before.base.authorization)
        self.assertEqual(owned.reservation,before.reservation)
        self.assertEqual(owned.base.core.state.worker_claim.owner.run_id,self.policy.run_id)
        self.assertTrue(self.stop().accepted)
        stopped=self.current()
        self.assertEqual(stopped.reservation,owned.reservation)
        self.assertEqual(stopped.base.authorization,owned.base.authorization)
        self.assertEqual(stopped.base.core.state.worker_claim,owned.base.core.state.worker_claim)
        self.assertEqual(stopped.base.core.state.control.daily_quota,owned.base.core.state.control.daily_quota)
        self.assertEqual((self.provider.entries,self.provider.wire_attempts,len(self.provider.effects)),(0,0,0))

    def test_existing_claim_projection_still_cannot_mutate_authorization_or_new_reservation(self):
        self.reserve();self.approve_run();self.bind();before=self.current()
        def mutate(_request):
            item=self.raw.current()
            item['reservation']['M']['scope']['M']['artifact_digest']={'S':'f'*64}
            self.raw.admin_replace(item)
        self.raw.before_update=mutate
        result=self.invoke('claim_reserved_intent',dict(intent_id=before.reservation.intent_id,
            approval_epoch=3,expected_revision=before.revision),invocation=self.worker)
        self.assertFalse(result.accepted)
        self.assertIsNone(self.current().base.core.state.worker_claim)

    def test_headroom_for_stop_preserved_for_reserve(self):
        before=replace(self.initial,base=replace(self.initial.base,
            core=replace(self.initial.base.core,generation=engine.MAX_GENERATION-1)))
        self.raw.admin_replace(before.item())
        self.auth.reservations[self.controller]=replace(self.proof,expected_revision=before.revision)
        self.assertFalse(self.reserve(payload=dict(approval_epoch=3,expected_revision=before.revision)).accepted)
        self.assertTrue(self.stop().accepted)
        self.assertEqual(self.current().base.core.generation,engine.MAX_GENERATION)
        self.assertFalse(self.current().base.core.state.control.enabled)

    def test_headroom_for_stop_preserved_for_bind(self):
        before=replace(self.initial,base=replace(self.initial.base,
            core=replace(self.initial.base.core,generation=engine.MAX_GENERATION-2)))
        self.raw.admin_replace(before.item())
        self.auth.reservations[self.controller]=replace(self.proof,expected_revision=before.revision)
        self.assertTrue(self.reserve(payload=dict(approval_epoch=3,expected_revision=before.revision)).accepted)
        self.approve_run()
        self.assertFalse(self.bind().accepted)
        self.assertEqual(len(self.writes()),1)
        self.assertTrue(self.stop().accepted)
        self.assertEqual(self.current().base.core.generation,engine.MAX_GENERATION)

    def test_bind_timeout_before_commit_keeps_null_authorization_without_retry(self):
        self.reserve();self.approve_run();before=self.current()
        self.raw.before_update=lambda _request:(_ for _ in ()).throw(TimeoutError())
        self.assertFalse(self.bind().accepted)
        self.assertEqual(self.current(),before)
        self.assertEqual((len(self.writes()),self.raw.commits),(2,1))
        TRACE_EVIDENCE.append(dict(case='reserve_then_bind_timeout_before_commit',**self.metrics()))

    def test_interruption_after_bind_commit_has_no_ack_or_automatic_recovery(self):
        self.reserve();self.approve_run()
        self.raw.after_commit=lambda _request:(_ for _ in ()).throw(crypto_fixture.ProcessInterrupted())
        with self.assertRaises(crypto_fixture.ProcessInterrupted):self.bind()
        self.assertIsNotNone(self.current().base.authorization)
        self.raw.after_commit=None
        self.assertFalse(self.bind(gateway=self.restart()).accepted)
        self.assertEqual((len(self.writes()),self.raw.commits,self.positive_acks),(2,2,1))

    def test_stale_unbound_get_after_bind_cannot_reconfirm_existing_authorization(self):
        self.reserve();self.approve_run()
        unbound=self.current().item();payload=self.bind_payload()
        self.assertTrue(self.bind().accepted)
        self.raw.read_filter=lambda response:{**response,'Item':deepcopy(unbound)}
        self.assertFalse(self.bind(payload=payload,gateway=self.restart()).accepted)
        self.assertEqual((len(self.writes()),self.raw.commits,self.positive_acks),(3,2,2))

    def test_off_after_last_read_can_coexist_with_accounting_ack_but_never_execution(self):
        self.reserve();self.approve_run()
        def stop_after_response_was_observed(response):
            if response['Item']['generation']==engine._av(3):
                self.raw.read_filter=None
                self.assertTrue(self.stop().accepted)
            return response
        self.raw.read_filter=stop_after_response_was_observed
        result=self.bind()
        self.assertTrue(result.accepted)  # Acknowledges prior accounting, not current ON or permission.
        self.assertFalse(result.execution_allowed)
        self.assertFalse(self.current().base.core.state.control.enabled)
        self.assertEqual(self.provider.entries,0)

    def test_unknown_top_level_attribute_added_by_privileged_writer_withholds_ack(self):
        def add_unmodeled_attribute(_request):
            item=self.raw.current();item['unmodeled']={'BOOL':True}
            self.raw.admin_replace(item)
        self.raw.before_update=add_unmodeled_attribute
        result=self.reserve()
        self.assertFalse(result.accepted)
        self.assertTrue(result.requires_reconciliation)
        # Known-field CAS cannot fence arbitrary new top-level attributes by a
        # privileged writer; exact ACK decoder fails closed. Writers are TCB.
        self.assertEqual((len(self.writes()),self.raw.commits,self.positive_acks),(1,1,0))

    def test_expired_future_and_malformed_reservation_evidence_cannot_authorize(self):
        for changes in [dict(issued_at=_iso(NOW+timedelta(seconds=1))),
                        dict(valid_until=_iso(NOW)),dict(valid_until=_iso(NOW+timedelta(seconds=301))),
                        dict(trust_generation=True),dict(evidence_id='untrusted'),dict(approval_epoch=4),
                        dict(scope=replace(self.scope,configuration_digest='f'*64)),dict(plan={'verified':True})]:
            with self.subTest(changes=changes):
                self.assertFalse(self.reserve(proof=replace(self.proof,**changes)).accepted)
        self.assertEqual(self.writes(),[])

    def test_malformed_commands_and_external_generic_mutations_never_write(self):
        for name in ('reserve_intent','bind_approved_run'):
            for payload in (None,[],True,{},dict(expected_revision=True,approval_epoch=3),
                            dict(expected_revision=self.initial.revision,approval_epoch=True)):
                self.assertFalse(self.gateway.handle(name,payload,invocation=self.controller).accepted)
        for name in (None,[],{},True,'compare_and_swap','release','reset','restore','re_enable'):
            self.assertFalse(self.gateway.handle(name,{},invocation=self.controller).accepted)
        self.assertEqual(self.writes(),[])

    def test_physical_incarnation_mismatch_and_raw_worker_writer_are_denied(self):
        changed=replace(self.binding,incarnation='b'*32)
        auth=SyntheticAdmissionAuthority(changed,keys=(),now=NOW)
        context=auth.controller_invocation()
        gateway=self.restart(binding=changed,authentication=auth)
        self.assertFalse(self.reserve(gateway=gateway,invocation=context).accepted)
        self.raw.principal='worker'
        self.assertFalse(self.reserve().accepted)
        with self.assertRaises(ModelError):self.raw.update_item(UpdateExpression='SET generation = :x')
        self.assertEqual(self.raw.commits,0)

    def test_privileged_rollback_is_a_negative_counterexample_not_prevented_by_python(self):
        self.reserve();self.approve_run();self.bind()
        first=self.current().base.authorization
        self.raw.admin_replace(self.initial.item())  # Deliberate TCB violation.
        self.assertTrue(self.reserve().accepted)
        self.approve_run(policy=replace(self.policy,run_id='999002'),token=self.token(run_id='999002'))
        self.assertTrue(self.bind().accepted)
        self.assertNotEqual(self.current().base.authorization.policy.run_id,first.policy.run_id)
        self.assertEqual(self.raw.commits,4)
        # No production claim; restore fencing remains an external hard gate.
        self.assertEqual(self.provider.entries,0)

    def test_aba_through_off_with_fresh_generation_rejects_old_reservation_revision(self):
        before=self.initial
        off=replace(before,base=replace(before.base,core=replace(before.base.core,generation=2,
            state=replace(before.base.core.state,control=disable_controller(before.base.core.state.control)))))
        self.raw.admin_replace(off.item())
        restored=replace(before,base=replace(before.base,core=replace(before.base.core,generation=3)))
        self.raw.admin_replace(restored.item())  # Privileged ON here, still generation != old A.
        self.assertFalse(self.reserve().accepted)
        self.assertEqual(self.raw.commits,0)

    def test_false_idempotent_ack_replay_is_an_explicit_transport_tcb_counterexample(self):
        barrier=Barrier(2);lock=Lock();cache=[];raw=self.raw
        class ReplayedSuccess:
            def get_item(self,**request):return raw.get_item(**request)
            def update_item(self,**request):
                barrier.wait(timeout=4)
                with lock:
                    if not cache:cache.append(deepcopy(raw.update_item(**request)))
                    return deepcopy(cache[0])
        gateway=self.restart(transport=ReplayedSuccess())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.reserve(gateway=gateway),range(2)))
        self.assertEqual(sum(r.accepted for r in results),2)
        self.assertEqual(self.raw.commits,1)
        self.assertTrue(all(not r.execution_allowed for r in results))
        self.assertEqual(self.provider.entries,0)

    def test_sdk_like_hidden_retry_is_visible_and_never_releases_ambiguous_grant(self):
        raw=self.raw
        raw.after_commit=lambda _request:(_ for _ in ()).throw(ConnectionResetError())
        class RetryingTransport:
            def get_item(self,**request):return raw.get_item(**request)
            def update_item(self,**request):
                try:return raw.update_item(**request)
                except ConnectionResetError:return raw.update_item(**request)
        self.assertFalse(self.reserve(gateway=self.restart(transport=RetryingTransport())).accepted)
        self.assertEqual((len(self.writes()),self.raw.commits,self.positive_acks),(2,1,0))

    def test_process_restart_sees_bound_item_but_cannot_reconfirm_or_rebind(self):
        self.reserve();self.approve_run();self.bind()
        script='''
import json,sys
from datetime import datetime
from test_autonomous_runtime_offline import reject_network,NETWORK_ATTEMPTS
sys.addaudithook(reject_network)
from fake_dynamodb_control import SyntheticDynamoTransport
from test_reserve_bind_offline import SyntheticAdmissionAuthority
from reserve_bind_offline import OfflineAdmissionGateway,AdmissionSnapshot
from dynamodb_cas_offline import AuthorityBinding
d=json.load(sys.stdin);b=AuthorityBinding(**d['binding']);raw=SyntheticDynamoTransport(d['path'])
now=datetime.fromisoformat(d['now']);a=SyntheticAdmissionAuthority(b,keys=(),now=now);ctx=a.controller_invocation()
s=AdmissionSnapshot.decode(raw.current(),b)
g=OfflineAdmissionGateway(transport=raw,binding=b,authentication=a,clock=lambda:now)
r=g.handle('bind_approved_run',dict(intent_id=s.reservation.intent_id,approval_epoch=3,expected_revision=s.revision),invocation=ctx)
print(json.dumps(dict(accepted=r.accepted,execution_allowed=r.execution_allowed,writes=sum(n=='UpdateItem' for n,_ in raw.calls),network_attempts=NETWORK_ATTEMPTS)))
'''
        root=Path(__file__).resolve().parents[1]
        env={n:v for n,v in os.environ.items() if not n.endswith(('_TOKEN','_API_KEY','_SECRET'))}
        env['PYTHONPATH']=str(root/'tests')+os.pathsep+str(root)
        result=subprocess.run([sys.executable,'-B','-c',script],input=json.dumps(
            dict(binding=asdict(self.binding),path=self.path,now=NOW.isoformat())),
            capture_output=True,text=True,check=True,timeout=5,cwd=root,env=env)
        self.assertEqual(json.loads(result.stdout),dict(accepted=False,execution_allowed=False,writes=0,network_attempts=[]))

    def test_existing_effect_schema_is_explicitly_not_seeded_or_migrated(self):
        self.reserve();self.approve_run();self.bind()
        with self.assertRaises(engine.StoreUnavailable):recovery.RecoverySnapshot.decode(self.raw.current(),self.binding)
        for name in ('simulate_external_attempt','create_effect','release','refund','re_enable','takeover','restore','compare_and_swap'):
            self.assertFalse(self.invoke(name,{}).accepted)
        self.assertIs(v3.CONTROLLER_STORE,None)
        self.assertIs(worker_identity.CONTROLLER_VERIFIER,None)
        self.assertIs(command.PRODUCTION_COMMAND_GATEWAY,None)
        self.assertIs(recovery.PRODUCTION_EFFECT_RECOVERY,None)
        self.assertEqual(self.provider.entries,0)


if __name__ == '__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ReserveBindOfflineTests))
    print('Unexpected network attempts:',NETWORK_ATTEMPTS)
    raise SystemExit(0 if result.wasSuccessful() and not NETWORK_ATTEMPTS else 1)

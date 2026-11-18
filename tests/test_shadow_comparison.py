import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import yaml
import shadow_comparison as policy
import shadow_capture as capture
import shadow_runner as worker

N = datetime(2026, 11, 18, tzinfo=timezone.utc)

class PolicyTests(unittest.TestCase):
    def test_finite_lifetime_budget(self):
        self.assertEqual(policy.MAX_ATTEMPTS,20)
        self.assertLess(policy.TOTAL_RESERVATION_USD,5)
        self.assertEqual(policy.TOTAL_RESERVATION_USD, Decimal('4.80'))
        self.assertEqual(policy.MAX_PROMPT_BYTES, 22000)
        self.assertEqual(policy.MAX_INPUT_TOKENS, policy.MAX_PROMPT_BYTES+512)
        worst = policy.estimated_cost(policy.MAX_INPUT_TOKENS, policy.MAX_OUTPUT_TOKENS)
        self.assertEqual(worst, Decimal('0.23256'))
        self.assertLess(worst, policy.RESERVE_PER_ATTEMPT_USD)

    def test_cost_estimate_covers_default_cache_writes_and_rejects_excess_usage(self):
        self.assertEqual(policy.estimated_cost(20000, 6000), Decimal('0.22'))
        for inputs, outputs in ((policy.MAX_INPUT_TOKENS+1,1),(True,1),(1,True),(-1,1),(1,6001)):
            with self.assertRaises(policy.ShadowRejected): policy.estimated_cost(inputs,outputs)

    def test_exact_input_only_fixed_tools_and_token_bounds(self):
        request = policy.request_for('FROZEN EVIDENCE', ['Yes','No'])
        self.assertTrue(request['input'].startswith('FROZEN EVIDENCE'))
        self.assertEqual(request['model'], 'gpt-5.6-sol')
        self.assertEqual(request['reasoning'], {'effort':'high'})
        self.assertEqual(request['tools'],[])
        self.assertEqual(request['store'],False)
        self.assertEqual(request['max_output_tokens'],6000)
        self.assertEqual(request['service_tier'],'default')

    def test_oversized_unicode_never_truncated(self):
        for prompt in ('x'*24000, '甲'*8000, ''):
            with self.assertRaises(policy.ShadowRejected): policy.request_for(prompt,['Yes','No'])

    def test_invalid_distributions(self):
        for values in ([True,0],[float('nan'),1],[float('inf'),0],[-.1,1.1],[.2,.2],[.5]):
            with self.assertRaises(policy.ShadowRejected): policy.probabilities(values,2)
        self.assertEqual(policy.probabilities([0,1],2),[0.,1.])

    def test_expired_future_closed_and_stale_snapshots(self):
        n=N
        self.assertTrue(policy.eligible_time(n,n+timedelta(hours=1),n))
        for a,b,c in ((n,n,n),(n+timedelta(seconds=1),n+timedelta(hours=1),n),
                      (n-timedelta(seconds=901),n+timedelta(hours=1),n),
                      (policy.ENDS_AT,policy.ENDS_AT+timedelta(hours=1),policy.ENDS_AT),
                      (n.replace(tzinfo=None),n,n)):
            self.assertFalse(policy.eligible_time(a,b,c))

    def test_proper_scores_binary_and_multiclass(self):
        score=policy.score_pair([.7,.3],[.8,.2],0)
        self.assertAlmostEqual(score['baseline']['brier'],.09)
        self.assertAlmostEqual(score['candidate']['brier'],.04)
        self.assertLess(score['candidate']['logloss'],score['baseline']['logloss'])
        self.assertEqual(policy.score_pair([0,0,1],[0,0,1],2)['candidate']['brier'],0)

    def test_encryption_round_trip_wrong_key_and_no_plaintext(self):
        raw=capture.cipher('k'*64).encrypt(b'FROZEN_EVIDENCE')
        self.assertNotIn(b'FROZEN_EVIDENCE',raw)
        self.assertEqual(capture.cipher('k'*64).decrypt(raw),b'FROZEN_EVIDENCE')
        with self.assertRaises(Exception): capture.cipher('q'*64).decrypt(raw)

    def test_capture_exception_cannot_change_original_result(self):
        c=capture.Capture()
        async def original(q,p): return 'UNCHANGED'
        b=SimpleNamespace(_binary_prompt_to_forecast=original,_multiple_choice_prompt_to_forecast=original)
        with patch.object(c,'observe',side_effect=RuntimeError('private must not be logged')):
            c.attach(b)
            self.assertEqual(asyncio.run(b._binary_prompt_to_forecast(None,'x')),'UNCHANGED')
        c.detach(b)
        self.assertIs(b._binary_prompt_to_forecast,original)

    def test_three_samples_capture_once_and_disagreement_rejects(self):
        n=datetime.now(timezone.utc)
        q=SimpleNamespace(question_type='binary',id_of_post=1,id_of_question=2,open_time=n-timedelta(hours=1))
        b=SimpleNamespace(_get_effective_close_time=lambda q:n+timedelta(hours=2))
        c=capture.Capture()
        with patch.object(capture,'ENDS_AT',n+timedelta(days=1)):
            for _ in range(3): c.observe(b,q,'unchanged evidence')
            self.assertEqual(len(c.records),1)
            self.assertFalse(c.records[(1,2)]['inconsistent'])
            c.observe(b,q,'different evidence')
        self.assertTrue(c.records[(1,2)]['inconsistent'])

    def test_transports_block_metaculus_post_and_foreign_hosts(self):
        for host,path,method in [('evil.example','/', 'GET'),('www.metaculus.com','/api/posts/1/', 'POST'),
                                 ('api.github.com',worker.ROOT+'/contents/main.py','PUT'),
                                 ('api.openai.com','/v1/chat/completions','POST')]:
            with self.assertRaises(policy.ShadowRejected): worker.network(host,path,method=method)

    def test_unknown_claim_ack_cannot_succeed(self):
        data={'experiment':policy.EXPERIMENT_ID,'schema':1,'slots':[]}
        with patch.object(worker,'gh',return_value={'content':{'sha':'0'*40,'path':worker.LEDGER_PATH}}) as call:
            with self.assertRaises(policy.ShadowRejected): worker.write_state(data,'oldsha')
        self.assertEqual(call.call_count,1)

    def test_missing_ledger_is_not_initialized(self):
        with patch.object(worker,'gh',side_effect=policy.ShadowRejected('missing')) as call:
            with self.assertRaises(policy.ShadowRejected): worker.read_state()
        self.assertEqual(call.call_count,1)

    def test_response_requires_completed_correct_model_valid_usage_and_vector(self):
        response={'model':policy.MODEL,'status':'completed','usage':{'input_tokens':100,'output_tokens':100},
                  'output':[{'type':'message','status':'completed','content':[{'type':'output_text',
                      'text':json.dumps({'probabilities':[.7,.3],'rationale':'Evidence only'})}]}]}
        self.assertEqual(worker.response_result(response,2)['state'],'success')
        for change in ({'status':'incomplete'},{'model':'other'},{'usage':{'input_tokens':True,'output_tokens':1}},
                       {'usage':{'input_tokens':100,'output_tokens':6001}},{'output':[]}):
            with self.assertRaises(Exception): worker.response_result(dict(response,**change),2)

    def test_source_context_rejects_reruns_and_foreign_repos(self):
        env={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':capture.REPO,'GITHUB_REF':'refs/heads/main',
             'GITHUB_RUN_ATTEMPT':'1','GITHUB_EVENT_NAME':'workflow_run',
             'GITHUB_WORKFLOW_REF':capture.REPO+'/'+worker.WORKFLOW+'@refs/heads/main'}
        with patch.dict(os.environ,env,clear=True): worker.check_context()
        for change in ({'GITHUB_RUN_ATTEMPT':'2'},{'GITHUB_EVENT_NAME':'pull_request'},{'GITHUB_REPOSITORY':'other/repo'}):
            with patch.dict(os.environ,dict(env,**change),clear=True):
                with self.assertRaises(policy.ShadowRejected): worker.check_context()

    def test_model_timeout_is_one_attempt_and_second_invocation_denied(self):
        request=policy.request_for('frozen evidence',['Yes','No'])
        ctx={'payload':request,'digest':policy.payload_digest(request),'worker_run':'123','post_id':1,
             'captured_at':N.isoformat(),'closes_at':(N+timedelta(hours=1)).isoformat(),
             'prepared_at':N.isoformat(),'option_count':2}
        with tempfile.TemporaryDirectory() as d, patch.object(worker,'WORK',Path(d)), patch.object(worker,'now',return_value=N), patch.object(worker,'check_context'), patch.dict(os.environ,{'GITHUB_RUN_ID':'123','VRCHCAB_OPENAI_TEST_KEY':'secret'},clear=True), patch.object(worker,'network',side_effect=TimeoutError('PRIVATE RESPONSE')) as call:
            (Path(d)/'request.json').write_text(json.dumps(ctx))
            worker.infer()
            result=json.loads((Path(d)/'result.json').read_text())
            self.assertEqual(result['state'],'failed')
            self.assertNotIn('PRIVATE',json.dumps(result))
            with self.assertRaises(FileExistsError): worker.infer()
            self.assertEqual(call.call_count,1)

    def test_twenty_spent_slots_block_before_download_or_model(self):
        source={'id':123,'repository':{'full_name':capture.REPO},'head_repository':{'full_name':capture.REPO},
                'head_branch':'main','run_attempt':1,'path':worker.SOURCE_WORKFLOW,'status':'completed','conclusion':'success',
                'event':'workflow_dispatch','head_sha':'a'*40}
        with tempfile.TemporaryDirectory() as d:
            event=Path(d)/'event.json'
            event.write_text(json.dumps({'workflow_run':source}))
            with patch.object(worker,'check_context'), patch.object(worker,'now',return_value=N), patch.dict(os.environ,{'GITHUB_EVENT_PATH':str(event)},clear=True), patch.object(worker,'gh',return_value=source), patch.object(worker,'read_state',return_value=({'slots':[{} for _ in range(20)]},'sha')), patch.object(worker,'artifact') as download:
                worker.prepare()
                download.assert_not_called()

    def test_workflow_separates_model_and_publication_credentials(self):
        root=Path(__file__).resolve().parents[1]/'.github/workflows'
        w=yaml.load((root/'openai_shadow.yaml').read_text(),Loader=yaml.BaseLoader)
        p=yaml.load((root/'run_bot_automatic.yaml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(w['on']),{'workflow_run','workflow_dispatch'})
        self.assertNotEqual(w['concurrency']['group'],p['concurrency']['group'])
        steps=w['jobs']['shadow']['steps']
        inference=next(s for s in steps if s.get('run','').endswith('shadow_runner.py infer'))
        self.assertEqual(inference['env'],{'VRCHCAB_OPENAI_TEST_KEY':'${{ secrets.VRCHCAB_OPENAI_TEST_KEY }}'})
        self.assertNotIn('env',w)
        self.assertNotIn('env',w['jobs']['shadow'])
        for s in steps:
            if s is not inference: self.assertNotIn('VRCHCAB_OPENAI_TEST_KEY',str(s))
        self.assertNotIn('VRCHCAB_OPENAI_TEST_KEY',str(p))
        self.assertEqual(p['jobs']['forecast_job']['steps'][-1]['name'],'Queue next automatic pass')

if __name__ == '__main__': unittest.main()


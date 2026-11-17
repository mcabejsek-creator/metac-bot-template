"""Offline regressions: no real credentials and no network access."""
import copy
import io
import json
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tools import probe_openai_bonus as probe


class FakeResponse:
    status = 200

    def __init__(self, data):
        self.body = data if isinstance(data, bytes) else json.dumps(data).encode()

    def read(self, size):
        return self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return FakeResponse(result)


class OpenAIBonusProbeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 17, 22, tzinfo=timezone.utc)
        self.env = {
            'GITHUB_REPOSITORY': probe.REPOSITORY,
            'GITHUB_REF': 'refs/heads/main',
            'GITHUB_EVENT_NAME': 'push',
            'GITHUB_RUN_NUMBER': '1',
            'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_WORKFLOW_REF': f'{probe.REPOSITORY}/{probe.WORKFLOW}@refs/heads/main',
            'VRCHCAB_OPENAI_TEST_KEY': 'synthetic-test-secret-not-real',
        }
        self.completion = {
            'status': 'completed', 'model': probe.MODEL,
            'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': '{"probability":0.3}'}]}],
            'usage': {'input_tokens': 100, 'output_tokens': 80},
        }

    def run_fake(self, results, env=None):
        opener = FakeOpener(results)
        with patch.object(probe.urllib.request, 'build_opener', side_effect=AssertionError('live client forbidden')):
            report = probe.run_probe(self.env if env is None else env, now=self.now, opener=opener)
        return report, opener

    def test_success_one_get_one_post_and_no_publishing(self):
        report, client = self.run_fake([{'id': probe.MODEL}, self.completion])
        self.assertEqual(report['status'], 'success')
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(report['publishes'], 0)
        self.assertFalse(report['billing_attribution_verified'])
        self.assertAlmostEqual(report['estimated_usd_at_2026_11_17_rates'], 0.002)
        self.assertNotIn(self.env['VRCHCAB_OPENAI_TEST_KEY'], json.dumps(report))
        self.assertEqual([r.full_url for r, _ in client.requests], [probe.MODEL_URL, probe.RESPONSE_URL])
        self.assertEqual([r.get_method() for r, _ in client.requests], ['GET', 'POST'])
        sent = json.loads(client.requests[1][0].data)
        self.assertEqual(sent, probe.payload())

    def test_reruns_other_events_repositories_and_workflows_fail_before_transport(self):
        changes = {'GITHUB_REPOSITORY': 'other/repo', 'GITHUB_REF': 'refs/heads/other',
                   'GITHUB_EVENT_NAME': 'pull_request', 'GITHUB_RUN_NUMBER': '2',
                   'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_WORKFLOW_REF': 'other'}
        for key, value in changes.items():
            with self.subTest(key=key):
                report, client = self.run_fake([], {**self.env, key: value})
                self.assertEqual(report['status'], 'not_authorized')
                self.assertFalse(client.requests)

    def test_expiry_blocks_later_execution(self):
        self.assertFalse(probe.authorized(self.env, probe.EXPIRES_AT))

    def test_missing_key_never_falls_back_to_other_keys(self):
        env = {**self.env, 'VRCHCAB_OPENAI_TEST_KEY': '', 'OPENAI_API_KEY': 'do-not-use',
               'OPENROUTER_API_KEY': 'do-not-use'}
        report, client = self.run_fake([], env)
        self.assertEqual(report['status'], 'missing_or_invalid_test_key')
        self.assertFalse(client.requests)

    def test_http_errors_are_sanitized_and_do_not_trigger_post(self):
        secret = self.env['VRCHCAB_OPENAI_TEST_KEY']
        for code in (401, 403, 404, 429, 500):
            with self.subTest(code=code):
                error = urllib.error.HTTPError(probe.MODEL_URL, code, secret, {}, io.BytesIO(secret.encode()))
                report, client = self.run_fake([error])
                self.assertEqual(report['status'], f'http_{code}')
                self.assertEqual(report['response_post_attempts'], 0)
                self.assertEqual(len(client.requests), 1)
                self.assertNotIn(secret, json.dumps(report))

    def test_post_timeout_is_counted_once_and_never_retried(self):
        report, client = self.run_fake([{'id': probe.MODEL}, TimeoutError('sensitive')])
        self.assertEqual(report['status'], 'transport_error_no_retry')
        self.assertEqual(report['response_post_attempts'], 1)
        self.assertEqual(len(client.requests), 2)
        self.assertNotIn('sensitive', json.dumps(report))

    def test_wrong_model_stops_before_post(self):
        report, client = self.run_fake([{'id': 'another-model'}])
        self.assertEqual(report['status'], 'model_not_verified')
        self.assertEqual(len(client.requests), 1)

    def test_no_redirect_and_no_arbitrary_destination(self):
        self.assertIsNone(probe.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com'))
        client = FakeOpener([])
        with self.assertRaises(probe.ProbeError):
            probe.request_json(client, 'fake', 'POST', 'https://example.com')
        self.assertFalse(client.requests)

    def test_malformed_or_large_response_does_not_retry(self):
        for raw in (b'not-json', b'x' * (probe.MAX_RESPONSE_BYTES + 1), b'[]'):
            with self.subTest(length=len(raw)):
                report, client = self.run_fake([raw])
                self.assertNotEqual(report['status'], 'success')
                self.assertEqual(len(client.requests), 1)

    def test_incomplete_completion_is_not_success_and_not_retried(self):
        report, client = self.run_fake([{'id': probe.MODEL}, {**self.completion, 'status': 'incomplete'}])
        self.assertEqual(report['status'], 'completion_not_completed_no_retry')
        self.assertEqual(len(client.requests), 2)

    def test_incorrect_synthetic_answer_and_invalid_usage_are_rejected(self):
        bad_answer = copy.deepcopy(self.completion)
        bad_answer['output'][0]['content'][0]['text'] = '{"probability":0.7}'
        for item in (bad_answer, {**self.completion, 'model': 'wrong-model'},
                     {**self.completion, 'usage': {}},
                     {**self.completion, 'usage': {'input_tokens': True, 'output_tokens': 3}},
                     {**self.completion, 'usage': {'input_tokens': 100, 'output_tokens': 513}}):
            with self.subTest(item=item):
                report, client = self.run_fake([{'id': probe.MODEL}, item])
                self.assertNotEqual(report['status'], 'success')
                self.assertEqual(len(client.requests), 2)

    def test_synthetic_request_has_fixed_caps_no_tools_and_no_storage(self):
        request = probe.payload()
        self.assertEqual(request['max_output_tokens'], 512)
        self.assertEqual(request['reasoning'], {'effort': 'high'})
        self.assertEqual(request['tools'], [])
        self.assertFalse(request['store'])
        self.assertEqual(request['service_tier'], 'default')
        self.assertLess(len(json.dumps(request)), 1500)
        self.assertNotIn('temperature', request)

    def test_workflow_is_isolated_one_shot_and_only_receives_test_key(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.load((root / probe.WORKFLOW).read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['on']), {'push'})
        self.assertEqual(workflow['on']['push']['branches'], ['main'])
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        job = workflow['jobs']['probe']
        self.assertIn('github.run_number == 1', job['if'])
        self.assertIn('github.run_attempt == 1', job['if'])
        self.assertNotIn('env', workflow)
        self.assertNotIn('env', job)
        steps = job['steps']
        live = steps[-1]
        self.assertEqual(live['env'], {'VRCHCAB_OPENAI_TEST_KEY': '${{ secrets.VRCHCAB_OPENAI_TEST_KEY }}'})
        self.assertEqual(steps[0]['with']['persist-credentials'], 'false')
        for name in ('METACULUS_TOKEN', 'OPENROUTER_API_KEY', 'PERPLEXITY_API_KEY', 'GITHUB_TOKEN'):
            self.assertNotIn(name, str(workflow))
        production = root / '.github/workflows/run_bot_automatic.yaml'
        if production.exists():
            self.assertNotIn('VRCHCAB_OPENAI_TEST_KEY', production.read_text())


if __name__ == '__main__':
    unittest.main()

"""Optional isolated SDK lab, NOT discovered by tests.run or tools/test_*.py.

Requires the separately hash-pinned botocore 1.43.126 lab. HTTP or connection
pool transport is replaced before calls. Synthetic credentials, .invalid endpoint,
network audit and sanitized process environment prevent live AWS requests.
No SDK dependency or client factory is added to the production application.
"""

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_autonomous_runtime_offline import reject_network, NETWORK_ATTEMPTS
sys.addaudithook(reject_network)
for name in list(os.environ):
    if name.startswith('AWS_') or name.endswith(('_TOKEN', '_API_KEY', '_SECRET')):
        os.environ.pop(name, None)
os.environ.update(AWS_CONFIG_FILE='/dev/null', AWS_SHARED_CREDENTIALS_FILE='/dev/null',
                  AWS_EC2_METADATA_DISABLED='true')
for method in ('send', 'sendall'):
    original = getattr(socket.socket, method)
    def blocked(sock, *args, _original=original, _method=method, **kwargs):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            NETWORK_ATTEMPTS.append('socket.' + _method)
            raise RuntimeError('Network forbidden in SDK lab')
        return _original(sock, *args, **kwargs)
    setattr(socket.socket, method, blocked)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--report', type=Path, required=True)
args = parser.parse_args()
try:
    version = importlib.metadata.version('botocore')
except importlib.metadata.PackageNotFoundError:
    version = None
if version != '1.43.126':
    args.report.write_text(json.dumps(dict(status='not_run', reason='isolated pinned SDK missing',
                                         installed=version, required='1.43.126')) + '\n')
    raise SystemExit(2)

# urllib3 detects IPv6 support at import by binding ::1. Suppress that unrelated
# local capability probe in this synthetic-HTTP lab; keep every network guard
# active and never discard recorded events. No network family is exercised.
socket.has_ipv6 = False
import botocore.session
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError
import dynamodb_cas_offline as engine
import worker_claim_offline as v3
from fake_dynamodb_control import SyntheticDynamoTransport, ModelError
from test_github_contents_cas_offline import candidate
from test_worker_claim_offline import reserved

OBSERVATIONS = []


class RawResponse:
    def __init__(self, body):
        self.body = body

    def stream(self, *args, **kwargs):
        yield self.body


def response(request, status, body):
    return AWSResponse(request.url, status, {'content-type': 'application/x-amz-json-1.0',
                                           'x-amzn-requestid': 'synthetic-request'},
                       RawResponse(json.dumps(body).encode()))


def client(options):
    return botocore.session.get_session().create_client(
        'dynamodb', region_name='us-east-1', endpoint_url='https://dynamodb.offline.invalid',
        aws_access_key_id='SYNTHETIC_ACCESS', aws_secret_access_key='SYNTHETIC_SECRET',
        config=Config(**options))


class SDKRetryOfflineTests(unittest.TestCase):
    def request(self):
        return dict(TableName='offline-control', Key={'pk': {'S': 'controller'}},
                    UpdateExpression='SET #g = #g + :one', ConditionExpression='#g = :old',
                    ExpressionAttributeNames={'#g': 'generation'},
                    ExpressionAttributeValues={':one': {'N': '1'}, ':old': {'N': '1'}})

    def failure_attempts(self, options, operation='update_item'):
        sdk = client(options)
        calls = []
        def send(request):
            calls.append(json.loads(request.body))
            return response(request, 500, {'__type': 'InternalServerError', 'message': 'synthetic'})
        with patch.object(sdk._endpoint.http_session, 'send', side_effect=send), \
                patch('botocore.endpoint.time.sleep'):
            with self.assertRaises(ClientError):
                if operation == 'get_item':
                    sdk.get_item(TableName='offline-control', Key={'pk': {'S': 'controller'}}, ConsistentRead=True)
                else:
                    sdk.update_item(**self.request())
        OBSERVATIONS.append(dict(case='500', operation=operation, options=options,
                                 resolved_retries=sdk.meta.config.retries, http_attempts=len(calls)))
        self.assertTrue(all(call == calls[0] for call in calls))
        sdk.close()
        return len(calls)

    def test_default_legacy_dynamodb_retries_writes(self):
        attempts = self.failure_attempts(dict(connect_timeout=2, read_timeout=5))
        self.assertGreater(attempts, 1)

    def test_standard_default_retries_writes_three_times(self):
        attempts = self.failure_attempts(dict(retries={'mode': 'standard'}, connect_timeout=2, read_timeout=5))
        self.assertEqual(attempts, 3)

    def test_total_max_attempts_one_disables_get_and_update_retries(self):
        with patch.dict(os.environ, {'AWS_MAX_ATTEMPTS': '9', 'AWS_RETRY_MODE': 'adaptive'}):
            for operation in ('get_item', 'update_item'):
                with self.subTest(operation=operation):
                    self.assertEqual(self.failure_attempts(engine.sdk_config_requirements(), operation), 1)

    def test_real_http_session_disables_nested_urllib3_retries(self):
        from urllib3.exceptions import ReadTimeoutError as PoolReadTimeout
        sdk = client(engine.sdk_config_requirements())
        pool, manager = MagicMock(), MagicMock()
        pool.urlopen.side_effect = PoolReadTimeout(pool, '/offline', 'synthetic')
        manager.connection_from_url.return_value = pool
        # Exercise actual botocore HTTP-session code; only its connection pool
        # is synthetic. Verify that a second retry loop is not delegated to it.
        with patch.object(sdk._endpoint.http_session, '_get_connection_manager', return_value=manager):
            with self.assertRaises(ReadTimeoutError):
                sdk.update_item(**self.request())
        self.assertEqual(pool.urlopen.call_count, 1)
        self.assertIs(pool.urlopen.call_args.kwargs['retries'].total, False)
        OBSERVATIONS.append(dict(case='actual_http_session_with_fake_pool',
                                 pool_calls=pool.urlopen.call_count, urllib3_retries=False))
        sdk.close()

    def test_lost_ack_can_turn_success_into_conditional_failure_without_retry_disable(self):
        for mode, expected_attempts in (('standard_default', 2), ('disabled', 1)):
            with self.subTest(mode=mode):
                options = (engine.sdk_config_requirements() if mode == 'disabled'
                           else dict(retries={'mode': 'standard'}, connect_timeout=2, read_timeout=5))
                sdk = client(options)
                calls = []
                committed = []
                def send(request):
                    calls.append(json.loads(request.body))
                    if not committed:
                        committed.append('synthetic commit')
                        raise ReadTimeoutError(endpoint_url=request.url, error='synthetic lost ACK')
                    return response(request, 400, {'__type': 'ConditionalCheckFailedException',
                                                   'message': 'synthetic old generation'})
                with patch.object(sdk._endpoint.http_session, 'send', side_effect=send), \
                        patch('botocore.endpoint.time.sleep'):
                    with self.assertRaises((ClientError, ReadTimeoutError)):
                        sdk.update_item(**self.request())
                self.assertEqual(len(calls), expected_attempts)
                self.assertEqual(len(committed), 1)
                OBSERVATIONS.append(dict(case='commit_then_lost_ack', mode=mode,
                                         http_attempts=len(calls), modeled_commits=len(committed)))
                sdk.close()

    def test_actual_sdk_serializes_full_prototype_requests_over_synthetic_http(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-sdk-dynamo-') as directory:
            binding = engine.AuthorityBinding('offline-control', 'controller', 'a' * 32,
                                               'offline://account/region/table')
            initial = engine.Snapshot(v3.OfflineControlStateV3(reserved()), 1, binding)
            model = SyntheticDynamoTransport.seed(str(Path(directory) / 'control.sqlite'), initial.item())
            sdk = client(engine.sdk_config_requirements())
            calls = []
            def send(request):
                operation = request.headers['X-Amz-Target']
                if isinstance(operation, bytes):
                    operation = operation.decode()
                operation = operation.rsplit('.', 1)[-1]
                calls.append(operation)
                body = json.loads(request.body)
                try:
                    value = model.get_item(**body) if operation == 'GetItem' else model.update_item(**body)
                except ModelError as error:
                    return response(request, 400, {'__type': error.response['Error']['Code'], 'message': 'synthetic'})
                value.pop('ResponseMetadata', None)  # Botocore creates metadata from HTTP, not the JSON body.
                return response(request, 200, value)
            with patch.object(sdk._endpoint.http_session, 'send', side_effect=send):
                store = engine.DynamoConditionalStorePrototype(transport=sdk, binding=binding)
                result = store.compare_and_swap(initial.revision, candidate(initial.state, '1'))
            self.assertTrue(result.confirmed)
            self.assertFalse(result.execution_allowed)
            self.assertEqual(calls, ['GetItem', 'UpdateItem', 'GetItem'])
            OBSERVATIONS.append(dict(case='prototype_through_actual_sdk', http_operations=calls,
                                     execution_allowed=result.execution_allowed))
            sdk.close()


suite = unittest.defaultTestLoader.loadTestsFromTestCase(SDKRetryOfflineTests)
result = unittest.TextTestRunner(verbosity=2).run(suite)
report = dict(botocore=version, tests_run=result.testsRun, failures=len(result.failures),
              errors=len(result.errors), skipped=len(result.skipped), network_attempts=NETWORK_ATTEMPTS,
              ipv6_capability_probe_disabled_in_test_process=True,
              observations=OBSERVATIONS, success=result.wasSuccessful() and not NETWORK_ATTEMPTS)
args.report.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
sys.exit(0 if report['success'] else 1)

"""Deterministic CAS security audit; stdlib only, never a live V3 adapter.

Reuses the V2 FakeGitHub and the existing SQLite test backend. Its blob model
implements only the documented file-SHA precondition, not real GitHub timing.
Standalone: python -B tests/test_github_contents_cas_offline.py
"""

from dataclasses import replace
import http.client
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __package__:
    from .fake_github_control import FakeGitHub, Reply, blob_sha
    from .test_worker_claim_offline import SQLiteControlStore, reserved, NOW
    from .test_autonomous_runtime_offline import reject_network, NETWORK_ATTEMPTS
else:
    from fake_github_control import FakeGitHub, Reply, blob_sha
    from test_worker_claim_offline import SQLiteControlStore, reserved, NOW
    from test_autonomous_runtime_offline import reject_network, NETWORK_ATTEMPTS

if __name__ == '__main__':
    sys.addaudithook(reject_network)

from dispatch_intent_controller import ControllerState, disable_controller
from github_control_store import GitHubContentsControlStore, ControlStoreUnavailable, TARGET_PATH
import worker_claim_offline as v3
import worker_identity
import worker_configuration
from perplexity_run_budget import MAX_AUTO_AGENT_POST_ATTEMPTS


def candidate(state, request):
    # Synthetic *data*, not identity evidence. Never passed to the real claim
    # function as an authenticated owner and never used to authorize execution.
    owner = v3.ClaimOwner('12345', '67890', 'a' * 40,
                          'synthetic/repo/.github/workflows/worker.yaml@refs/heads/test',
                          'b' * 40, '999001', '1')
    record = v3.WorkerClaim(state.control.pending.intent_id, state.control.approval_epoch,
                            request * 32, owner, NOW.strftime('%Y-%m-%dT%H:%M:%SZ'))
    return replace(state, worker_claim=record)


def blob_record_confirmed(fake, expected_revision, proposal):
    """The storage-only CAS + exact readback checks required by the V3 prototype.

    This omits identity/admission intentionally: the question is whether the
    backend alone meets its declared revision contract. No execution grant.
    """
    if fake.cas_state(expected_revision, proposal) is not True:
        return False
    return fake.state == proposal and fake.revision != expected_revision


class RefBeforeOidModel:
    """Symbolic single-ref subset of documented updateRefs, no GraphQL client.

    OIDs identify fresh modeled commits with a parent and complete document.
    Does not model GitHub durability, permission enforcement or consistency.
    """

    def __init__(self, document):
        self.commits = {}
        self.head = self.commit(None, document)

    def commit(self, parent, document):
        oid = f'{len(self.commits) + 1:040x}'
        self.commits[oid] = (parent, document)
        return oid

    def update(self, before_oid, after_oid, *, force=False):
        if before_oid is not None and self.head != before_oid:
            return False
        if not force:
            ancestor = after_oid
            while ancestor is not None and ancestor != self.head:
                ancestor = self.commits[ancestor][0]
            if ancestor != self.head:
                return False
        self.head = after_oid
        return True


class GitHubContentsCASAuditTests(unittest.TestCase):
    def setUp(self):
        self.initial = reserved()
        self.fake = FakeGitHub(self.initial)
        self.store = self.adapter(self.fake)
        self.proposal = disable_controller(self.initial)

    @staticmethod
    def adapter(fake):
        return GitHubContentsControlStore(token='SYNTHETIC_NOT_A_CREDENTIAL', opener=fake)

    def mutate_reply(self, transform, method):
        original = self.fake.open

        def open_(request, timeout):
            reply = original(request, timeout)
            return transform(reply) if request.get_method() == method else reply

        return patch.object(self.fake, 'open', side_effect=open_)

    @staticmethod
    def json_reply(reply, transform):
        data = json.loads(reply.body)
        transform(data)
        return Reply(data, reply.status)

    def test_shared_fake_uses_git_blob_sha_and_equal_bytes_recycle_revision(self):
        self.assertEqual(blob_sha(b'hello\n'), 'ce013625030ba8dba906f756967f9e9ca394464a')
        state, revision = self.store.read()
        self.assertEqual(revision, blob_sha(state.encode().encode()))
        self.fake.replace_state(self.proposal)
        self.assertNotEqual(self.fake.revision, revision)
        self.fake.replace_state(state)
        self.assertEqual(self.fake.revision, revision)

    def test_two_writers_against_same_blob_revision_one_distinct_update_conflicts(self):
        _, revision_a = self.store.read()
        _, revision_b = self.adapter(self.fake).read()
        self.assertEqual(revision_a, revision_b)
        changed = replace(self.initial, daily_quota=replace(self.initial.daily_quota, reserved_usd='0.80'))
        self.assertTrue(self.store.compare_and_swap(revision_a, self.proposal))
        self.assertFalse(self.adapter(self.fake).compare_and_swap(revision_b, changed))
        self.assertEqual(self.fake.state, self.proposal)
        self.assertEqual([c[0] for c in self.fake.calls], ['GET', 'GET', 'PUT', 'PUT'])

    def test_noop_writes_can_both_succeed_without_advancing_blob_revision(self):
        state, revision = self.store.read()
        self.assertTrue(self.store.compare_and_swap(revision, state))
        self.assertTrue(self.store.compare_and_swap(revision, state))
        self.assertEqual(self.fake.writes, 2)
        self.assertEqual(self.fake.revision, revision)

    def test_contents_off_on_aba_accepts_stale_expected_revision(self):
        state, old_revision = self.store.read()
        self.fake.replace_state(disable_controller(state))
        self.fake.replace_state(state)
        changed = replace(state, daily_quota=replace(state.daily_quota, reserved_usd='0.80'))
        self.assertTrue(self.store.compare_and_swap(old_revision, changed))
        after, after_revision = self.store.read()
        self.assertEqual(after, changed)
        self.assertNotEqual(after_revision, old_revision)
        # These checks passed despite missing the intervening emergency OFF.
        self.assertEqual(self.fake.writes, 3)

    def test_blob_aba_can_confirm_two_different_owners_for_one_intent(self):
        initial = v3.OfflineControlStateV3(self.initial)
        fake = FakeGitHub(initial, codec=v3.OfflineControlStateV3)
        read_by_a = read_by_b = fake.revision
        first, second = candidate(initial, '1'), candidate(initial, '2')
        self.assertTrue(blob_record_confirmed(fake, read_by_a, first))
        fake.replace_state(initial)  # Other writer/rollback discards the owner.
        self.assertTrue(blob_record_confirmed(fake, read_by_b, second))
        self.assertNotEqual(first.worker_claim.request_id, second.worker_claim.request_id)
        self.assertEqual(first.control.pending, second.control.pending)
        self.assertEqual(fake.state, second)
        self.assertFalse(v3.ClaimResult(True, 'synthetic_storage_observation').execution_allowed)

    def test_another_writer_can_overwrite_owner_even_with_fresh_blob_revision(self):
        initial = v3.OfflineControlStateV3(self.initial)
        fake = FakeGitHub(initial, codec=v3.OfflineControlStateV3)
        first, second = candidate(initial, '1'), candidate(initial, '2')
        self.assertTrue(blob_record_confirmed(fake, fake.revision, first))
        self.assertTrue(fake.cas_state(fake.revision, second))
        self.assertEqual(fake.state.worker_claim, second.worker_claim)

    def test_other_writer_changes_entire_control_document_between_get_and_put(self):
        _, revision = self.store.read()
        other = replace(self.initial, daily_quota=replace(self.initial.daily_quota, reserved_usd='0.80'))
        self.fake.replace_state(other)
        self.assertFalse(self.store.compare_and_swap(revision, self.proposal))
        self.assertEqual(self.fake.state, other)
        self.assertEqual(self.fake.writes, 1)

    def test_changed_intent_between_get_and_put_is_not_overwritten(self):
        from datetime import timedelta
        _, revision = self.store.read()
        other = reserved(NOW + timedelta(minutes=15))
        self.fake.replace_state(other)
        self.assertFalse(self.store.compare_and_swap(revision, self.proposal))
        self.assertEqual(self.fake.state.pending, other.pending)

    def test_emergency_off_without_aba_conflicts_and_remains_off(self):
        state, revision = self.store.read()
        self.fake.replace_state(disable_controller(state))
        self.assertFalse(self.store.compare_and_swap(revision, state))
        self.assertFalse(self.fake.state.enabled)

    def test_http_errors_deny_put_and_never_retry(self):
        for code in (409, 422, 404, 401, 403, 500, 502, 503):
            with self.subTest(code=code):
                fake = FakeGitHub(self.initial)
                store = self.adapter(fake)
                _, revision = store.read()
                fake.next_status = code
                if code in (409, 422):
                    self.assertIs(store.compare_and_swap(revision, self.proposal), False)
                else:
                    with self.assertRaises(ControlStoreUnavailable):
                        store.compare_and_swap(revision, self.proposal)
                self.assertEqual([c[0] for c in fake.calls], ['GET', 'PUT'])
                self.assertEqual(fake.writes, 0)

    def test_http_errors_deny_get_without_initialization(self):
        for code in (409, 422, 404, 401, 403, 500, 503):
            with self.subTest(code=code):
                fake = FakeGitHub(self.initial)
                fake.next_status = code
                with self.assertRaises(ControlStoreUnavailable):
                    self.adapter(fake).read()
                self.assertEqual([c[0] for c in fake.calls], ['GET'])
                self.assertEqual(fake.writes, 0)

    def test_timeout_or_disconnect_after_commit_never_confirms_or_retries(self):
        for error in (TimeoutError, ConnectionResetError):
            with self.subTest(error=error.__name__):
                fake = FakeGitHub(self.initial)
                store = self.adapter(fake)
                _, revision = store.read()
                original = fake.open

                def lose_response(request, timeout):
                    response = original(request, timeout)
                    if request.get_method() == 'PUT':
                        raise error('synthetic lost response')
                    return response

                with patch.object(fake, 'open', side_effect=lose_response):
                    with self.assertRaises(ControlStoreUnavailable):
                        store.compare_and_swap(revision, self.proposal)
                self.assertEqual(fake.state, self.proposal)
                self.assertEqual(fake.writes, 1)
                self.assertEqual([c[0] for c in fake.calls], ['GET', 'PUT'])

    def test_timeout_or_disconnect_before_commit_is_not_an_automatic_retry_grant(self):
        for method in ('GET', 'PUT'):
            for error in (TimeoutError, ConnectionResetError):
                with self.subTest(method=method, error=error.__name__):
                    fake = FakeGitHub(self.initial)
                    store = self.adapter(fake)
                    with patch.object(fake, 'open', side_effect=error('synthetic before commit')) as request:
                        with self.assertRaises(ControlStoreUnavailable):
                            if method == 'GET':
                                store.read()
                            else:
                                store.compare_and_swap(fake.revision, self.proposal)
                    self.assertEqual(request.call_count, 1)
                    self.assertEqual(fake.writes, 0)

    def test_restart_after_lost_ack_does_not_make_old_revision_fresh(self):
        _, revision = self.store.read()

        def lost(reply):
            raise TimeoutError('synthetic after commit')

        with self.mutate_reply(lost, 'PUT'):
            with self.assertRaises(ControlStoreUnavailable):
                self.store.compare_and_swap(revision, self.proposal)
        restarted = self.adapter(self.fake)
        self.assertEqual(restarted.read()[0], self.proposal)
        # Deliberate unsafe caller retry in the MODEL, never a library loop.
        self.assertFalse(restarted.compare_and_swap(revision, self.initial))
        self.assertEqual(self.fake.writes, 1)

    def test_incomplete_http_body_is_normalized_to_unavailable_without_retry(self):
        for method in ('GET', 'PUT'):
            with self.subTest(method=method):
                fake = FakeGitHub(self.initial)
                store = self.adapter(fake)
                original = fake.open

                def incomplete(request, timeout):
                    reply = original(request, timeout)
                    if request.get_method() == method:
                        reply.read = lambda *_: (_ for _ in ()).throw(http.client.IncompleteRead(b'{', 40))
                    return reply

                with patch.object(fake, 'open', side_effect=incomplete):
                    with self.assertRaises(ControlStoreUnavailable):
                        if method == 'GET':
                            store.read()
                        else:
                            store.compare_and_swap(fake.revision, self.proposal)
                self.assertEqual(len(fake.calls), 1)

    def test_truncated_json_and_unexpected_success_status_are_not_confirmation(self):
        for status, body in ((200, b'{"content":'), (201, b'{}'), (204, b''),
                             (200, b'null'), (200, b'{"content":null}')):
            with self.subTest(status=status, body=body):
                fake = FakeGitHub(self.initial)
                store = self.adapter(fake)
                revision = fake.revision
                original = fake.open

                def truncated(request, timeout):
                    reply = original(request, timeout)
                    reply.status, reply.body = status, body
                    return reply

                with patch.object(fake, 'open', side_effect=truncated):
                    with self.assertRaises(ControlStoreUnavailable):
                        store.compare_and_swap(revision, self.proposal)
                self.assertEqual(fake.writes, 1)  # Lack of acknowledgment is not absence of commit.
                self.assertEqual(len(fake.calls), 1)

    def test_200_with_wrong_blob_or_resource_cannot_acknowledge_requested_state(self):
        for changes in ({'sha': '0' * 40}, {'sha': 'bad'}, {'path': 'other.json'}, {'type': 'dir'}):
            with self.subTest(changes=changes):
                fake = FakeGitHub(self.initial)
                store = self.adapter(fake)
                revision = fake.revision
                original = fake.open

                def wrong_ack(request, timeout):
                    reply = original(request, timeout)
                    return self.json_reply(reply, lambda data: data['content'].update(changes))

                with patch.object(fake, 'open', side_effect=wrong_ack):
                    with self.assertRaises(ControlStoreUnavailable):
                        store.compare_and_swap(revision, self.proposal)
                self.assertEqual(fake.writes, 1)

        for commit in (None, {}, {'sha': 'bad'}):
            with self.subTest(commit=commit):
                fake = FakeGitHub(self.initial)
                store = self.adapter(fake)
                revision = fake.revision
                original = fake.open

                def wrong_commit(request, timeout):
                    reply = original(request, timeout)
                    return self.json_reply(reply, lambda data: data.update(commit=commit))

                with patch.object(fake, 'open', side_effect=wrong_commit):
                    with self.assertRaises(ControlStoreUnavailable):
                        store.compare_and_swap(revision, self.proposal)
                self.assertEqual(fake.writes, 1)

    def test_matching_200_metadata_still_does_not_prove_actual_entire_state(self):
        revision = self.fake.revision
        response = Reply(dict(content=dict(type='file', path=TARGET_PATH,
                                           sha=blob_sha(self.proposal.encode().encode())),
                              commit=dict(sha='1' * 40)))
        with patch.object(self.fake, 'open', return_value=response) as request:
            self.assertTrue(self.store.compare_and_swap(revision, self.proposal))
            self.assertEqual(request.call_count, 1)
        self.assertNotEqual(self.store.read()[0], self.proposal)
        self.assertEqual(self.fake.writes, 0)

    def test_get_binds_reported_blob_sha_to_decoded_bytes(self):
        def incorrect(reply):
            return self.json_reply(reply, lambda data: data.update(sha='0' * 40))

        with self.mutate_reply(incorrect, 'GET'):
            with self.assertRaises(ControlStoreUnavailable):
                self.store.read()
        self.assertEqual(self.fake.writes, 0)

    def test_get_allows_base64_line_wrapping_but_rejects_ignored_garbage(self):
        def wrapped(reply):
            def change(data):
                raw = data['content']
                data['content'] = '\r\n'.join(raw[i:i+60] for i in range(0, len(raw), 60)) + '\n'
            return self.json_reply(reply, change)

        with self.mutate_reply(wrapped, 'GET'):
            self.assertEqual(self.store.read()[0], self.initial)

        def invalid(reply):
            return self.json_reply(reply, lambda data: data.update(content=data['content'] + '!!!'))

        with self.mutate_reply(invalid, 'GET'):
            with self.assertRaises(ControlStoreUnavailable):
                self.store.read()

    def test_stale_cache_pair_is_internally_valid_but_put_must_conflict(self):
        old_state, old_revision = self.store.read()
        original = self.fake.open
        # Obtain a genuine modeled response for A, then change authoritative state.
        request = self.store._request('GET', url=self.fake.calls[0][1])
        cached = original(request, 12)
        self.fake.replace_state(self.proposal)

        def stale(request, timeout):
            if request.get_method() == 'GET':
                return cached
            return original(request, timeout)

        with patch.object(self.fake, 'open', side_effect=stale):
            self.assertEqual(self.store.read(), (old_state, old_revision))
            self.assertFalse(self.store.compare_and_swap(old_revision, old_state))
        self.assertFalse(self.fake.state.enabled)

    def test_matching_cached_readback_can_hide_later_owner_overwrite(self):
        initial = v3.OfflineControlStateV3(self.initial)
        fake = FakeGitHub(initial, codec=v3.OfflineControlStateV3)
        old_revision = fake.revision
        first, second = candidate(initial, '1'), candidate(initial, '2')
        self.assertTrue(fake.cas_state(old_revision, first))
        cached_state, cached_revision = fake.state, fake.revision
        fake.replace_state(second)
        self.assertEqual(cached_state, first)
        self.assertNotEqual(cached_revision, old_revision)
        self.assertNotEqual(fake.state.worker_claim, cached_state.worker_claim)
        # Expected bytes + changed revision in a stale readback do not prove current ownership.

    def test_existing_generation_store_rejects_aba_without_second_ledger(self):
        initial = v3.OfflineControlStateV3(self.initial)
        with tempfile.TemporaryDirectory(prefix='synthetic-cas-audit-') as directory:
            store = SQLiteControlStore.seed(str(Path(directory) / 'control.sqlite'), initial)
            _, old_revision = store.read()
            off = replace(initial, control=disable_controller(initial.control))
            self.assertTrue(store.compare_and_swap(old_revision, off.encode()))
            _, off_revision = store.read()
            self.assertTrue(store.compare_and_swap(off_revision, initial.encode()))
            self.assertNotEqual(store.read()[1], old_revision)
            self.assertFalse(store.compare_and_swap(old_revision, candidate(initial, '1').encode()))
            self.assertEqual(v3.OfflineControlStateV3.decode(store.read()[0]), initial)

    def test_before_oid_distinguishes_restored_blob_in_fresh_commit(self):
        model = RefBeforeOidModel('A')
        old_oid = model.head
        off = model.commit(old_oid, 'OFF')
        self.assertTrue(model.update(old_oid, off))
        restored_a = model.commit(off, 'A')
        self.assertTrue(model.update(off, restored_a))
        stale_proposal = model.commit(old_oid, 'CLAIM')
        self.assertFalse(model.update(old_oid, stale_proposal))
        self.assertNotEqual(model.head, old_oid)

    def test_ref_force_push_rollback_reintroduces_before_oid_aba(self):
        model = RefBeforeOidModel('A')
        old_oid = model.head
        first = model.commit(old_oid, 'OWNER-1')
        self.assertTrue(model.update(old_oid, first))
        self.assertFalse(model.update(first, old_oid))  # Not a fast-forward.
        self.assertTrue(model.update(first, old_oid, force=True))
        second = model.commit(old_oid, 'OWNER-2')
        self.assertTrue(model.update(old_oid, second))

    def test_before_oid_must_be_present_and_noop_ref_updates_do_not_advance_generation(self):
        model = RefBeforeOidModel('A')
        old_oid = model.head
        self.assertTrue(model.update(old_oid, old_oid))
        self.assertTrue(model.update(old_oid, old_oid))
        fresh = model.commit(old_oid, 'OWNER-1')
        self.assertTrue(model.update(old_oid, fresh))
        arbitrary_overwrite = model.commit(fresh, 'OWNER-2')
        self.assertTrue(model.update(None, arbitrary_overwrite))

    def test_live_v2_adapter_remains_incompatible_with_v3_and_execution_stays_denied(self):
        initial = v3.OfflineControlStateV3(self.initial)
        fake = FakeGitHub(initial, codec=v3.OfflineControlStateV3)
        store = self.adapter(fake)
        with self.assertRaises(ControlStoreUnavailable):
            store.read()
        with self.assertRaises(ControlStoreUnavailable):
            store.compare_and_swap(fake.revision, candidate(initial, '1'))
        self.assertEqual(fake.writes, 0)
        self.assertIsNone(v3.CONTROLLER_STORE)
        self.assertIsNone(worker_identity.CONTROLLER_VERIFIER)
        result = v3.claim_reserved_intent_offline(
            intent_id=self.initial.pending.intent_id, approval_epoch=3, request_id='1' * 32,
            worker_oidc_token='synthetic-not-an-oidc-token', now=NOW)
        self.assertFalse(result.claim_confirmed)
        self.assertFalse(result.execution_allowed)
        config = worker_configuration.WORKER_CONFIG
        self.assertEqual((config.research_reports_per_question, config.predictions_per_research_report), (2, 3))
        self.assertEqual(config.agent_requests_per_atomic_question, 4)
        self.assertEqual(MAX_AUTO_AGENT_POST_ATTEMPTS, 2)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(GitHubContentsCASAuditTests))
    print(f'Offline audit guard: {len(NETWORK_ATTEMPTS)} network attempts.')
    sys.exit(0 if result.wasSuccessful() and not NETWORK_ATTEMPTS else 1)

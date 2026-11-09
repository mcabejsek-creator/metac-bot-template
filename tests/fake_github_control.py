"""Shared, stdlib-only FakeGitHub extracted from the V2 controller tests.

This models ONLY the documented file-blob precondition, not real server
concurrency, durable storage, ref linearizability or cache behavior. Equal
bytes have equal blob SHA, even after intervening writes. A test-only codec
can describe V3 data; no live V3 adapter is implemented.
"""

import base64
from hashlib import sha1
import json
import urllib.error

from dispatch_intent_controller import ControllerState
from github_control_store import TARGET_BRANCH, TARGET_PATH


def blob_sha(binary):
    return sha1(b'blob ' + str(len(binary)).encode('ascii') + b'\0' + binary).hexdigest()


class Reply:
    def __init__(self, body, status=200):
        self.body = json.dumps(body).encode('utf-8')
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return self.body


class FakeGitHub:
    def __init__(self, initial=ControllerState(), *, codec=ControllerState):
        self.state = initial
        self.codec = codec
        self.calls = []
        self.next_status = None
        self.writes = 0

    @property
    def revision(self):
        return blob_sha(self.state.encode().encode('utf-8'))

    @property
    def commit_oid(self):
        return f'{self.writes + 1:040x}'  # Symbolic metadata, not a real Git commit.

    def replace_state(self, state):
        """An independent writer, also used to model unsafe rollback/overwrite."""
        self.state = self.codec.decode(state.encode())
        self.writes += 1

    def cas_state(self, expected_revision, state):
        """Deterministic blob-only interleaving; NOT a GitHub atomicity proof."""
        if expected_revision != self.revision:
            return False
        self.replace_state(state)
        return True

    def open(self, req, timeout):
        self.calls.append((req.get_method(), req.full_url, timeout))
        if self.next_status is not None:
            code = self.next_status
            self.next_status = None
            raise urllib.error.HTTPError(req.full_url, code, 'synthetic', {}, None)
        if req.get_method() == 'GET':
            return Reply(dict(type='file', path=TARGET_PATH, sha=self.revision, encoding='base64',
                              content=base64.b64encode(self.state.encode().encode()).decode()))
        if req.get_method() == 'PUT':
            payload = json.loads(req.data)
            state = self.codec.decode(base64.b64decode(payload['content'], validate=True))
            if payload['branch'] != TARGET_BRANCH or not self.cas_state(payload['sha'], state):
                raise urllib.error.HTTPError(req.full_url, 409, 'synthetic', {}, None)
            return Reply(dict(content=dict(type='file', path=TARGET_PATH, sha=self.revision),
                              commit=dict(sha=self.commit_oid)))
        raise AssertionError('Unexpected outbound network operation')

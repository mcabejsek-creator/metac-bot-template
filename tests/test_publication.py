from unittest.mock import patch

import requests

from .support import OfflineTestCase, app
from publication import CapturingClient, PublicationUncertain, Publisher, SingleAttemptWriter


class PublicationRetryTests(OfflineTestCase):
    def test_lost_acknowledgement_does_not_duplicate_comment(self):
        accepted = set()
        calls = []
        response = requests.Response()
        response.status_code = 200

        def local_server(url, **kwargs):
            calls.append(kwargs)
            key = kwargs.get("headers", {}).get("Idempotency-Key")
            accepted.add(key or f"non-idempotent-request-{len(calls)}")
            if len(calls) == 1:
                raise requests.Timeout("Synthetic acknowledgement lost after local server accepted write")
            return response

        client = app.MetaculusClient()
        capture = CapturingClient(client)
        with capture.capture() as operations:
            capture.post_question_comment(900001, "Local synthetic comment")
        self.publication_store.prepare("comment-test", "{}", operations)
        publisher = Publisher(self.publication_store, SingleAttemptWriter(client))
        # No credentials are necessary: both auth construction and HTTP are fake.
        with patch.object(client, "_get_auth_headers", return_value={"headers": {}}), \
             patch.object(client, "_sleep_between_requests"), \
             patch("time.sleep"), patch.object(requests, "post", local_server):
            with self.assertRaises(PublicationUncertain):
                publisher.drain("comment-test")
            with self.assertRaises(PublicationUncertain):
                publisher.drain("comment-test")
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(calls), 1)

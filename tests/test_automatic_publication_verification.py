"""Bounded, read-only confirmation of a newly published automatic forecast."""

import copy
import json
import types
import unittest
from unittest.mock import patch

from automatic_publication_verification import PublicationVerificationUnavailable, verify_published_question


class PublicationReadbackTests(unittest.TestCase):
    def setUp(self):
        self.question = types.SimpleNamespace(id_of_post=46399, id_of_question=901)
        self.payload = {"id": 46399, "group_of_questions": None, "conditional": None, "notebook": None,
            "question": {"id": 901, "my_forecasts": {
                "history": [{"question_id": 901, "start_time": 1794272400}],
                "latest": {"question_id": 901, "start_time": 1794272400}}}}
        self.calls = []

    def verify(self, payload=None, error=None):
        payload = self.payload if payload is None else payload
        calls = self.calls

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return json.dumps(payload).encode()

        class Opener:
            def open(self, request, timeout):
                calls.append((request, timeout))
                if error is not None: raise error
                return Response()

        with patch.dict("os.environ", {"METACULUS_TOKEN": "fake-token"}), patch(
            "automatic_publication_verification.urllib.request.build_opener", return_value=Opener()
        ) as build:
            result = verify_published_question(self.question)
            self.handler = build.call_args.args[0]
            return result

    def test_one_authenticated_get_confirms_exact_identity_with_nullable_variants(self):
        self.assertEqual(self.verify(), {"post_id": 46399, "question_id": 901, "own_forecast_count": 1})
        self.assertEqual(len(self.calls), 1)
        req, timeout = self.calls[0]
        self.assertEqual(req.method, "GET")
        self.assertEqual(req.full_url, "https://www.metaculus.com/api/posts/46399/")
        self.assertEqual(req.headers["Authorization"], "Token fake-token")
        self.assertEqual(timeout, 15)

    def test_mismatched_post_question_or_history_is_rejected(self):
        for location in ("post", "question", "history"):
            with self.subTest(location=location):
                payload = copy.deepcopy(self.payload)
                if location == "post": payload["id"] += 1
                elif location == "question": payload["question"]["id"] += 1
                else: payload["question"]["my_forecasts"]["history"][0]["question_id"] += 1
                with self.assertRaises(PublicationVerificationUnavailable): self.verify(payload)

    def test_absent_empty_or_malformed_history_does_not_prove_publication(self):
        for history in (None, [], [{}], ["invalid"]):
            with self.subTest(history=history):
                payload = copy.deepcopy(self.payload)
                payload["question"]["my_forecasts"]["history"] = history
                with self.assertRaises(PublicationVerificationUnavailable): self.verify(payload)

    def test_redirect_rejected_and_uncertain_get_is_not_retried(self):
        self.verify()
        with self.assertRaises(PublicationVerificationUnavailable):
            self.handler.redirect_request(None, None, 307, "redirect", {}, "https://other.invalid")
        self.calls.clear()
        with self.assertRaises(PublicationVerificationUnavailable): self.verify(error=TimeoutError())
        self.assertEqual(len(self.calls), 1)

    def test_group_and_invalid_local_identity_are_rejected(self):
        payload = copy.deepcopy(self.payload)
        payload["group_of_questions"] = {"questions": []}
        with self.assertRaises(PublicationVerificationUnavailable): self.verify(payload)
        self.calls.clear()
        self.question.id_of_question = True
        with self.assertRaises(PublicationVerificationUnavailable): self.verify()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

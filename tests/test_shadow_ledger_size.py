"""Bounded full-experiment storage and API-envelope regressions, offline."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import shadow_capture as capture
import shadow_comparison as policy
import shadow_runner as worker


NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)


def full_ledger():
    slots = []
    for index in range(policy.MAX_ATTEMPTS):
        slot = {"post_id": index + 1, "question_id": index + 101,
                "state": "success", "reserved_usd": str(policy.RESERVE_PER_ATTEMPT_USD),
                "snapshot": "c" * worker.MAX_CAPTURE_CIPHER_BYTES,
                "result": "r" * worker.MAX_RESULT_CIPHER_BYTES, "metadata": ""}
        slot["metadata"] = "m" * (worker.MAX_SLOT_BYTES - len(worker.canonical(slot)))
        slots.append(slot)
    return {"experiment": policy.EXPERIMENT_ID, "schema": 1, "slots": slots}


def response_connection(raw):
    connection = MagicMock()
    reply = connection.getresponse.return_value
    reply.status = 200
    reply.getheaders.return_value = []
    reply.read.return_value = raw
    return connection, reply


class ShadowLedgerSizeTests(unittest.TestCase):
    def test_twenty_maximal_slots_read_through_real_base64_envelope(self):
        data = full_ledger()
        raw = worker.canonical(data)
        sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        # Include GitHub's wrapped base64 lines, not only the raw file length.
        envelope = worker.canonical({"encoding": "base64", "path": worker.LEDGER_PATH,
                                     "sha": sha, "content": base64.encodebytes(raw).decode()})
        self.assertLess(len(raw), worker.MAX_LEDGER_RAW_BYTES)
        self.assertGreater(len(envelope), worker.MAX_BYTES)
        self.assertLess(len(envelope), worker.MAX_LEDGER_RESPONSE_BYTES)
        connection, reply = response_connection(envelope)
        with patch.dict(os.environ, {"GITHUB_TOKEN": "synthetic"}, clear=True), \
                patch.object(worker.http.client, "HTTPSConnection", return_value=connection):
            loaded, actual_sha = worker.read_state()
        self.assertEqual(loaded, data)
        self.assertEqual(actual_sha, sha)
        reply.read.assert_called_once_with(worker.MAX_LEDGER_RESPONSE_BYTES + 1)
        with patch.object(worker, "gh", return_value={"content": {"sha": sha, "path": worker.LEDGER_PATH}}) as call:
            self.assertEqual(worker.write_state(data, "previous"), sha)
        self.assertEqual(call.call_args.kwargs["method"], "PUT")

    def test_only_the_exact_ledger_get_receives_the_larger_response_limit(self):
        paths = [
            ("api.openai.com", "/v1/responses", "POST"),
            ("api.github.com", worker.ROOT + "/actions/artifacts/123/zip", "GET"),
            ("api.github.com", worker.STATE_URL, "PUT"),
            ("www.metaculus.com", "/api/posts/123/", "GET"),
        ]
        for host, path, method in paths:
            connection, reply = response_connection(b"x" * (worker.MAX_BYTES + 1))
            with self.subTest(host=host, path=path), \
                    patch.object(worker.http.client, "HTTPSConnection", return_value=connection):
                with self.assertRaisesRegex(policy.ShadowRejected, "response_too_large"):
                    worker.network(host, path, method=method)
            reply.read.assert_called_once_with(worker.MAX_BYTES + 1)
        connection, reply = response_connection(b"x" * (worker.MAX_LEDGER_RESPONSE_BYTES + 1))
        with patch.object(worker.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(policy.ShadowRejected, "response_too_large"):
                worker.network("api.github.com", worker.STATE_URL + "?ref=" + worker.BRANCH)
        reply.read.assert_called_once_with(worker.MAX_LEDGER_RESPONSE_BYTES + 1)

    def test_raw_file_limit_is_checked_before_any_write_or_reset(self):
        oversized = {"padding": "x" * worker.MAX_LEDGER_RAW_BYTES}
        with patch.object(worker, "gh") as call:
            with self.assertRaisesRegex(policy.ShadowRejected, "ledger_file_too_large_no_write"):
                worker.write_state(oversized, "oldsha")
        call.assert_not_called()
        item = {"encoding": "base64", "path": worker.LEDGER_PATH,
                "content": base64.b64encode(b"x" * worker.MAX_LEDGER_RAW_BYTES).decode()}
        with patch.object(worker, "gh", return_value=item) as call:
            with self.assertRaisesRegex(policy.ShadowRejected, "ledger_file_too_large_no_reset"):
                worker.read_state()
        call.assert_called_once_with(worker.STATE_URL + "?ref=" + worker.BRANCH)

    def test_oversized_capture_is_rejected_before_decryption_or_reservation(self):
        source = {"id": 123, "head_sha": "a" * 40, "run_attempt": 1,
                  "repository": {"full_name": capture.REPO}, "head_repository": {"full_name": capture.REPO},
                  "head_branch": "main", "path": worker.SOURCE_WORKFLOW,
                  "status": "completed", "conclusion": "success", "event": "workflow_dispatch"}
        with tempfile.TemporaryDirectory() as tmp:
            event = Path(tmp) / "event.json"
            event.write_text(json.dumps({"workflow_run": source}))
            with patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(event)}, clear=True), \
                    patch.object(worker, "check_context"), patch.object(worker, "now", return_value=NOW), \
                    patch.object(worker, "gh", return_value=source), \
                    patch.object(worker, "read_state", return_value=({"slots": []}, "oldsha")), \
                    patch.object(worker, "artifact", return_value=b"x" * (worker.MAX_CAPTURE_CIPHER_BYTES + 1)), \
                    patch.object(worker, "cipher") as decrypt, patch.object(worker, "write_state") as write:
                with self.assertRaisesRegex(policy.ShadowRejected, "capture_too_large_no_truncation"):
                    worker.prepare()
            decrypt.assert_not_called()
            write.assert_not_called()

    def test_oversized_model_result_stays_spent_and_retains_valid_accounting(self):
        request = policy.request_for("Frozen evidence", ["Yes", "No"])
        digest = policy.payload_digest(request)
        ctx = {"payload": request, "digest": digest, "worker_run": "456", "post_id": 101,
               "captured_at": NOW.isoformat(), "closes_at": (NOW + timedelta(hours=1)).isoformat(),
               "prepared_at": NOW.isoformat(), "option_count": 2}
        response = {"model": policy.MODEL, "status": "completed",
                    "usage": {"input_tokens": 100, "output_tokens": 3000},
                    "output": [{"type": "message", "status": "completed", "content": [{
                        "type": "output_text", "text": json.dumps({
                            "probabilities": [.6, .4], "rationale": "甲" * 3000}, ensure_ascii=False)}]}]}
        ledger = {"experiment": policy.EXPERIMENT_ID, "schema": 1, "slots": [{
            "post_id": 101, "state": "reserved", "worker_run": "456", "digest": digest,
            "reserved_usd": str(policy.RESERVE_PER_ATTEMPT_USD), "result": None}]}
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "request.json").write_bytes(worker.canonical(ctx))
            with patch.object(worker, "WORK", work), patch.object(worker, "check_context"), \
                    patch.dict(os.environ, {"GITHUB_RUN_ID": "456", "VRCHCAB_OPENAI_TEST_KEY": "synthetic"}, clear=True), \
                    patch.object(worker, "now", return_value=NOW), \
                    patch.object(worker, "network", return_value=(200, {}, worker.canonical(response))) as call:
                worker.infer()
            call.assert_called_once()
            raw_result = (work / "result.json").read_bytes()
            result = json.loads(raw_result)
            self.assertLessEqual(len(raw_result), worker.MAX_RESULT_BYTES)
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["reason"], "result_too_large_no_retry")
            self.assertEqual(result["input_tokens"], 100)
            self.assertEqual(result["output_tokens"], 3000)
            self.assertGreater(float(result["estimated_usd"]), 0)
            self.assertEqual(result["completed_at"], NOW.isoformat())
            self.assertNotIn("rationale", result)
            with patch.object(worker, "WORK", work), patch.object(worker, "check_context"), \
                    patch.dict(os.environ, {"GITHUB_RUN_ID": "456", "METACULUS_TOKEN": "k" * 64}, clear=True), \
                    patch.object(worker, "read_state", return_value=(ledger, "oldsha")), \
                    patch.object(worker, "write_state", return_value="newsha") as write:
                worker.finish()
            write.assert_called_once()
            slot = ledger["slots"][0]
            self.assertEqual(slot["state"], "failed")
            self.assertEqual(slot["reserved_usd"], "0.24")
            self.assertLessEqual(len(slot["result"].encode()), worker.MAX_RESULT_CIPHER_BYTES)
            self.assertEqual(json.loads(capture.cipher("k" * 64).decrypt(slot["result"].encode())), result)

    def test_maximum_plaintext_result_encryption_fits_reserved_storage(self):
        raw = b"x" * worker.MAX_RESULT_BYTES
        encrypted = capture.cipher("k" * 64).encrypt(raw)
        self.assertLessEqual(len(encrypted), worker.MAX_RESULT_CIPHER_BYTES)
        self.assertLess(worker.MAX_ATTEMPTS * worker.MAX_SLOT_BYTES + 1000, worker.MAX_LEDGER_RAW_BYTES)


if __name__ == "__main__":
    unittest.main()

"""Synthetic keys/JWTs only; no GitHub token, JWKS, secrets, claim or worker.

Standalone: python -B tests/test_worker_identity_offline.py
Required compatibility target: PyJWT 2.10.1 and cryptography 45.0.4 from the
unchanged poetry.lock. A run with other versions is exploratory evidence only.
The standard tests.run runner also discovers these tests when its SDK is present.
"""

import base64
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


NETWORK_ATTEMPTS = []


def reject_network(event, args):
    if event in {
        "socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
        "socket.gethostbyaddr", "socket.sendto", "socket.bind",
        "http.client.connect", "http.client.send",
    }:
        NETWORK_ATTEMPTS.append(event)
        raise RuntimeError("Network forbidden in OIDC offline tests")


if __name__ == "__main__":
    sys.addaudithook(reject_network)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker_configuration
import worker_identity as identity
from autonomous_orchestrator import IndependentReadings, evaluate_offline_worker_handoff
from dispatch_intent_controller import ControllerState, DailyQuota, PendingIntent, _intent_id
from perplexity_run_budget import MAX_AUTO_AGENT_POST_ATTEMPTS
from scheduler_policy import DailySpendEvidence


NOW = datetime(2026, 11, 9, 12, 30, tzinfo=timezone.utc)
STAMP = int(NOW.timestamp())
REAL_CONFIG = worker_configuration.WORKER_CONFIG
IN_LIMIT_TEST_CONFIG = replace(REAL_CONFIG, research_reports_per_question=1)


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class ReadOnlyControl:
    """Already-reserved SYNTHETIC state; no reservation/CAS operation performed."""

    def __init__(self):
        slot = "2026-11-09T12:30:00Z"
        self.state = ControllerState(
            enabled=True, approval_epoch=3, last_intent_at=slot, last_slot=slot,
            pending=PendingIntent(_intent_id(3, slot), slot, slot),
            daily_quota=DailyQuota("2026-11-09", 1, "0.40"),
        )
        self.writes = 0
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.state, "synthetic-revision"

    def compare_and_swap(self, *args):
        self.writes += 1
        raise AssertionError("Handoff must never write or claim")


class SyntheticSource:
    def __init__(self):
        self.calls = 0

    def read(self):
        self.calls += 1
        return IndependentReadings(
            github_checked_at=NOW, active_publishers=0, queued_publishers=0,
            funded_openrouter_checked_at=NOW, funded_openrouter_ok=True,
            perplexity_checked_at=NOW, perplexity_spend_ok=True,
            daily_spend=DailySpendEvidence(NOW.date(), NOW, "0.10", "1.00", "0.40", 1, 1, True),
            journal_checked_at=NOW, prior_publication_certain=True,
            operator_approval_confirmed=True, automation_switch_confirmed=True,
            is_first_attempt=True,
        )


class WorkerIdentityOfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        cls.jwt = jwt
        cls.serialization = serialization
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = cls.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        cls.policy = identity.ControllerOIDCPolicy(
            audience="urn:synthetic:vrchcab:controller",
            subject="repo:synthetic-owner/synthetic-repo:ref:refs/heads/test",
            repository="synthetic-owner/synthetic-repo", repository_id="12345",
            repository_owner_id="67890", source_sha="a" * 40,
            workflow_ref="synthetic-owner/synthetic-repo/.github/workflows/worker.yaml@" + "b" * 40,
            workflow_sha="b" * 40, run_id="999001",
        )
        cls.verifier = identity.GitHubActionsOIDCVerifier(cls.policy, (("synthetic-kid", cls.pem),))

    def claims(self, **changes):
        data = dict(
            iss=identity.GITHUB_ISSUER, aud=self.policy.audience, sub=self.policy.subject,
            iat=STAMP - 30, nbf=STAMP - 30, exp=STAMP + 120,
            repository=self.policy.repository, repository_id=self.policy.repository_id,
            repository_owner_id=self.policy.repository_owner_id, sha=self.policy.source_sha,
            workflow_ref=self.policy.workflow_ref, workflow_sha=self.policy.workflow_sha,
            run_id=self.policy.run_id, run_attempt="1",
        )
        return {**data, **changes}

    def token(self, *, claims=None, headers=None, key=None):
        return self.jwt.encode(
            self.claims() if claims is None else claims,
            self.key if key is None else key, algorithm="RS256",
            headers={"kid": "synthetic-kid", **(headers or {})},
        )

    def raw_token(self, *, header=None, payload=None):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        header = header if header is not None else '{"alg":"RS256","typ":"JWT","kid":"synthetic-kid"}'
        payload = payload if payload is not None else json.dumps(self.claims())
        body = b64(header.encode()) + "." + b64(payload.encode())
        signature = self.key.sign(body.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        return body + "." + b64(signature)

    def rejected(self, token, *, verifier=None, now=NOW):
        with self.assertRaises(identity.WorkerIdentityRejected) as caught:
            (self.verifier if verifier is None else verifier).verify(token, now=now)
        if type(token) is str and token:
            self.assertNotIn(token, str(caught.exception))

    def handoff(self, token=None, *, verifier=None, config=IN_LIMIT_TEST_CONFIG, now=NOW, **kwargs):
        control, source = ReadOnlyControl(), SyntheticSource()
        before = control.state.encode()
        with patch.object(worker_configuration, "WORKER_CONFIG", config), \
                patch.object(identity, "CONTROLLER_VERIFIER", verifier):
            result = evaluate_offline_worker_handoff(
                control, source, now=now, intent_id=control.state.pending.intent_id,
                approval_epoch=3, activation_confirmed=True,
                worker_oidc_token=token, **kwargs,
            )
        self.assertFalse(result.allowed)
        self.assertFalse(result.durably_reserved)
        self.assertEqual(control.state.encode(), before)
        self.assertEqual(control.writes, 0)
        return result, control, source

    def test_valid_rs256_context_is_immutable_and_not_launch_authority(self):
        result = self.verifier.verify(self.token(), now=NOW)
        self.assertIs(type(result), identity.ActionsOIDCIdentity)
        self.assertEqual(result.source_sha, self.policy.source_sha)
        self.assertEqual(result.repository_id, "12345")
        self.assertEqual(result.run_id, "999001")
        self.assertEqual(result.run_attempt, "1")
        self.assertFalse(hasattr(result, "verified"))
        with self.assertRaises(FrozenInstanceError):
            result.source_sha = "c" * 40
        with self.assertRaises(FrozenInstanceError):
            self.verifier.policy = None
        with self.assertRaises(TypeError):
            self.verifier._keys["injected"] = self.other_key.public_key()

    def test_policy_and_independent_public_keys_are_mandatory(self):
        for policy, keys in ((None, ()), ({"verified": True}, ()),
                             (self.policy, ()), (self.policy, {"synthetic-kid": self.pem}),
                             (self.policy, (("synthetic-kid", self.pem),) * 2)):
            with self.subTest(policy_type=type(policy).__name__, key_type=type(keys).__name__):
                with self.assertRaises(identity.WorkerIdentityRejected):
                    identity.GitHubActionsOIDCVerifier(policy, keys)
        for changes in (
            {"audience": ""}, {"repository_id": True}, {"repository_owner_id": "01"},
            {"source_sha": "main"}, {"run_id": 999001}, {"workflow_sha": "B" * 40},
            {"workflow_ref": "unrelated/workflow@main"},
            {"max_token_lifetime_seconds": True}, {"max_token_lifetime_seconds": 601},
        ):
            with self.subTest(changes=changes), self.assertRaises(identity.WorkerIdentityRejected):
                replace(self.policy, **changes)

    def test_private_non_rsa_weak_or_malformed_keys_are_rejected(self):
        from cryptography.hazmat.primitives.asymmetric import ec, rsa

        private_pem = self.key.private_bytes(
            self.serialization.Encoding.PEM, self.serialization.PrivateFormat.PKCS8,
            self.serialization.NoEncryption())
        ec_pem = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            self.serialization.Encoding.PEM, self.serialization.PublicFormat.SubjectPublicKeyInfo)
        weak_pem = rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key().public_bytes(
            self.serialization.Encoding.PEM, self.serialization.PublicFormat.SubjectPublicKeyInfo)
        for pem in (private_pem, ec_pem, weak_pem, b"bad", "not bytes", b"x" * 16385):
            with self.subTest(kind=type(pem).__name__), self.assertRaises(identity.WorkerIdentityRejected):
                identity.GitHubActionsOIDCVerifier(self.policy, (("synthetic-kid", pem),))

    def test_unsigned_and_hmac_algorithm_confusion_are_rejected(self):
        self.rejected(self.jwt.encode(self.claims(), key=None, algorithm="none",
                                     headers={"kid": "synthetic-kid"}))
        self.rejected(self.jwt.encode(self.claims(), key=b"synthetic-only-secret" * 3,
                                     algorithm="HS256", headers={"kid": "synthetic-kid"}))
        self.rejected(self.raw_token(header='{"alg":"RS512","typ":"JWT","kid":"synthetic-kid"}'))

    def test_unknown_kid_and_embedded_or_remote_key_headers_are_rejected(self):
        self.rejected(self.token(headers={"kid": "not-trusted"}))
        for header in (
            {"jku": "https://invalid.example/jwks"}, {"x5u": "https://invalid.example/cert"},
            {"jwk": {"kty": "RSA"}}, {"x5c": ["fake"]}, {"crit": ["b64"]},
            {"typ": "not-a-JWT"},
        ):
            with self.subTest(header=header):
                self.rejected(self.token(headers=header))
        self.rejected(self.raw_token(header='{"alg":"RS256","typ":"JWT","kid":"synthetic-kid","b64":false}'))
        self.rejected(self.raw_token(header='{"alg":"RS256","typ":"JWT","kid":1}'))

    def test_wrong_signature_and_payload_tampering_are_rejected(self):
        self.rejected(self.token(key=self.other_key))
        parts = self.token().split(".")
        parts[1] = b64(json.dumps(self.claims(sha="c" * 40)).encode())
        self.rejected(".".join(parts))
        parts = self.token().split(".")
        signature = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
        parts[2] = b64(bytes([signature[0] ^ 1]) + signature[1:])
        self.rejected(".".join(parts))

    def test_issuer_is_exact_and_not_token_selected(self):
        for issuer in ("https://token.actions.githubusercontent.com/", "https://invalid.example",
                       "token.actions.githubusercontent.com", "", None, [identity.GITHUB_ISSUER], True, 1):
            with self.subTest(issuer=issuer):
                self.rejected(self.raw_token(payload=json.dumps(self.claims(iss=issuer))))

    def test_audience_is_one_exact_string_without_membership_matching(self):
        for aud in ([self.policy.audience], [self.policy.audience, "another"],
                    "another", " " + self.policy.audience, None, 1, []):
            with self.subTest(aud=aud):
                self.rejected(self.token(claims=self.claims(aud=aud)))

    def test_every_required_claim_must_be_present(self):
        for name in self.claims():
            claims = self.claims()
            del claims[name]
            with self.subTest(name=name):
                self.rejected(self.token(claims=claims))

    def test_validity_boundaries_and_bounded_lifetime_use_trusted_clock(self):
        token = self.token(claims=self.claims(iat=STAMP, nbf=STAMP, exp=STAMP + 1))
        self.verifier.verify(token, now=NOW)  # inclusive nbf
        self.rejected(token, now=NOW + timedelta(seconds=1))  # exclusive exp
        for changes in (
            {"exp": STAMP}, {"nbf": STAMP + 1}, {"iat": STAMP + 1},
            {"iat": STAMP - 601, "nbf": STAMP - 601, "exp": STAMP + 1},
            {"iat": STAMP - 1, "nbf": STAMP - 2}, {"exp": STAMP - 31},
        ):
            with self.subTest(changes=changes):
                self.rejected(self.token(claims=self.claims(**changes)))
        self.rejected(self.token(), now=NOW.replace(tzinfo=None))
        self.rejected(self.token(), now="2026-11-09T12:30:00Z")
        self.verifier.verify(self.token(), now=NOW.astimezone(timezone(timedelta(hours=1))))

    def test_numeric_dates_never_coerce_strings_booleans_or_fractions(self):
        for name in ("iat", "nbf", "exp"):
            for value in (str(STAMP), float(STAMP), True, None, -1, [], {}):
                with self.subTest(name=name, kind=type(value).__name__):
                    self.rejected(self.token(claims=self.claims(**{name: value})))

    def test_repository_and_owner_ids_are_exact_and_strictly_typed(self):
        for name in ("repository_id", "repository_owner_id"):
            for value in ("99999", int(getattr(self.policy, name)), True, "012345", "", None):
                with self.subTest(name=name, value=value):
                    self.rejected(self.token(claims=self.claims(**{name: value})))
        self.rejected(self.token(claims=self.claims(repository="fork/synthetic-repo")))
        self.rejected(self.token(claims=self.claims(sub="repo:fork/repo:ref:refs/heads/test")))

    def test_source_and_workflow_commits_and_workflow_are_independently_checked(self):
        for name in ("sha", "workflow_sha", "workflow_ref"):
            for value in ("c" * 40, "main", None, 1):
                with self.subTest(name=name, value=value):
                    self.rejected(self.token(claims=self.claims(**{name: value})))
        self.rejected(self.token(claims=self.claims(sha=self.policy.source_sha.upper())))
        branch_ref = self.policy.repository + "/.github/workflows/worker.yaml@refs/heads/test"
        branch_policy = replace(self.policy, workflow_ref=branch_ref)
        verifier = identity.GitHubActionsOIDCVerifier(branch_policy, self.verifier.trusted_public_keys)
        verifier.verify(self.token(claims=self.claims(workflow_ref=branch_ref)), now=NOW)
        self.rejected(self.token(claims=self.claims(workflow_ref=branch_ref, workflow_sha="c" * 40)),
                      verifier=verifier)
        self.rejected(self.token(claims=self.claims(workflow_ref=branch_ref + "-other")), verifier=verifier)

    def test_run_id_is_controller_selected_and_only_first_attempt_is_valid(self):
        for value in ("999002", 999001, True, "0999001", "", None):
            with self.subTest(run_id=value):
                self.rejected(self.token(claims=self.claims(run_id=value)))
        for value in ("2", 1, True, "01", "1.0", None):
            with self.subTest(attempt=value):
                self.rejected(self.token(claims=self.claims(run_attempt=value)))

    def test_malformed_duplicate_or_oversized_tokens_never_supply_identity(self):
        for value in (None, True, {}, {"verified": True}, self.claims(), b"jwt", "", "a.b",
                      "a.b.c.d", "a..b", "!.b.c", "x" * 16385):
            with self.subTest(kind=type(value).__name__):
                self.rejected(value)
        self.rejected(self.raw_token(header='{"alg":"HS256","alg":"RS256","typ":"JWT","kid":"synthetic-kid"}'))
        self.rejected(self.raw_token(payload=json.dumps(self.claims())[:-1] + ',"run_attempt":"1"}'))
        self.rejected(self.raw_token(payload='[]'))
        self.rejected(self.raw_token(payload=json.dumps(self.claims())[:-1] + ',"extra":NaN}'))

    def test_production_trust_is_absent_and_environment_claims_cannot_supply_it(self):
        self.assertIsNone(identity.CONTROLLER_VERIFIER)
        for untrusted in (None, True, {"verified": True}, self.claims()):
            with patch.object(identity, "CONTROLLER_VERIFIER", untrusted):
                with self.assertRaisesRegex(identity.WorkerIdentityRejected, "controller_oidc_trust_unavailable"):
                    identity.verify_controller_worker_token(self.token(), now=NOW)
        env = {
            "GITHUB_SHA": self.policy.source_sha, "GITHUB_RUN_ID": self.policy.run_id,
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REPOSITORY_ID": self.policy.repository_id,
            "WORKER_IDENTITY_VERIFIED": "true", "GITHUB_OIDC_TOKEN": self.token(),
        }
        with patch.dict(os.environ, env):
            for token in (self.token(), {"verified": True}, self.claims()):
                with self.assertRaisesRegex(identity.WorkerIdentityRejected, "controller_oidc_trust_unavailable"):
                    identity.verify_controller_worker_token(token, now=NOW)
            self.assertEqual(self.handoff()[0].reason, "worker_execution_identity_unverified")
        with self.assertRaises(TypeError):
            identity.verify_controller_worker_token(self.token(), now=NOW, policy=self.policy)
        with self.assertRaises(TypeError):
            identity.verify_controller_worker_token(self.token(), now=NOW, public_keys=self.verifier.trusted_public_keys)

    def test_handoff_rejects_untrusted_tokens_claim_objects_and_verified_flags(self):
        for value in (self.token(), "malformed", self.claims(), {"verified": True}, True):
            with self.subTest(kind=type(value).__name__):
                self.assertEqual(self.handoff(value)[0].reason, "worker_oidc_identity_unverified")
        for token in (self.token(key=self.other_key), self.token(claims=self.claims(run_attempt="2")),
                      self.token(claims=self.claims(sha="c" * 40)), self.verifier.verify(self.token(), now=NOW)):
            self.assertEqual(self.handoff(token, verifier=self.verifier)[0].reason, "worker_oidc_identity_unverified")
        for kwargs in ({"verified": True}, {"approved_source_sha": self.policy.source_sha},
                       {"worker_config": REAL_CONFIG}, {"identity_policy": self.policy}):
            with self.subTest(kwargs=tuple(kwargs)), self.assertRaises(TypeError):
                self.handoff(**kwargs)

    def test_even_genuinely_signature_checked_token_always_denies_handoff(self):
        token = self.token()
        with patch.object(identity, "verify_controller_worker_token",
                          wraps=identity.verify_controller_worker_token) as checked:
            for _ in range(2):
                result, control, source = self.handoff(token, verifier=self.verifier)
                self.assertEqual(result.reason, "worker_execution_identity_unverified")
                self.assertEqual(result.intent_id, control.state.pending.intent_id)
                self.assertEqual(control.reads, 2)
                self.assertEqual(source.calls, 1)
            self.assertEqual(checked.call_count, 2)

    def test_real_two_by_three_plan_and_two_request_cap_still_deny_before_readers(self):
        self.assertEqual(REAL_CONFIG.research_reports_per_question, 2)
        self.assertEqual(REAL_CONFIG.predictions_per_research_report, 3)
        self.assertEqual(REAL_CONFIG.agent_phases, ("initial", "fact-check"))
        self.assertEqual(REAL_CONFIG.agent_requests_per_atomic_question, 4)
        self.assertEqual(MAX_AUTO_AGENT_POST_ATTEMPTS, 2)
        with patch.object(identity, "verify_controller_worker_token",
                          side_effect=AssertionError("budget gate must precede crypto")):
            result, _, source = self.handoff(self.token(), verifier=self.verifier, config=REAL_CONFIG)
            self.assertEqual(result.reason, "worker_research_plan_exceeds_automatic_attempt_cap")
            self.assertEqual(source.calls, 0)

    def test_handoff_evaluates_token_expiry_after_source_reads_without_claiming(self):
        token = self.token(claims=self.claims(exp=STAMP + 1))
        clock = iter((NOW, NOW + timedelta(seconds=1)))
        result, control, source = self.handoff(token, verifier=self.verifier, now=lambda: next(clock))
        self.assertEqual(result.reason, "worker_oidc_identity_unverified")
        self.assertEqual(control.reads, 2)
        self.assertEqual(source.calls, 1)


if __name__ == "__main__":
    versions = {name: importlib.metadata.version(name) for name in ("PyJWT", "cryptography")}
    print("Crypto dependencies:", versions, flush=True)
    print("Locked-version compatibility:",
          "target versions present; see test result" if versions == {"PyJWT": "2.10.1", "cryptography": "45.0.4"}
          else "NOT VERIFIED (different dependency versions; exploratory run only)", flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(WorkerIdentityOfflineTests))
    print(f"Offline audit guard: {len(NETWORK_ATTEMPTS)} network attempts.")
    sys.exit(0 if result.wasSuccessful() and not NETWORK_ATTEMPTS else 1)

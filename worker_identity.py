"""Offline GitHub Actions OIDC verification, never worker launch authority.

Keys and expected claims must come from a trusted controller, independently of
the token, worker arguments, environment and checkout. No production trust has
been provisioned: CONTROLLER_VERIFIER deliberately remains None. A valid JWT
identifies an Actions context, NOT the code/configuration actually executing.
There is no discovery, JWKS download, token acquisition, replay claim or I/O.
"""

import base64
from dataclasses import dataclass, field
from datetime import datetime
import json
import re
from types import MappingProxyType


GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
_SHA = re.compile(r"[0-9a-f]{40}")
_ID = re.compile(r"[1-9][0-9]{0,19}")
_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
_REQUIRED = (
    "iss", "aud", "sub", "iat", "nbf", "exp", "repository",
    "repository_id", "repository_owner_id", "sha", "workflow_ref",
    "workflow_sha", "run_id", "run_attempt",
)


class WorkerIdentityRejected(ValueError):
    """Invalid token or unavailable trust; messages never contain token data."""


def _text(value):
    return type(value) is str and 0 < len(value) <= 1024 and value == value.strip()


@dataclass(frozen=True)
class ControllerOIDCPolicy:
    """Expected values supplied independently by a TRUSTED controller.

    Constructing this object confers no trust. The worker must never choose it.
    Both the source commit and the workflow commit are independently pinned.
    An exact workflow_ref alone (which can name a branch/tag) is insufficient;
    the separate workflow_sha comparison always pins its commit as well.
    """

    audience: str
    subject: str
    repository: str
    repository_id: str
    repository_owner_id: str
    source_sha: str
    workflow_ref: str
    workflow_sha: str
    run_id: str
    max_token_lifetime_seconds: int = 600

    def __post_init__(self):
        for value in (self.audience, self.subject, self.repository, self.workflow_ref):
            if not _text(value):
                raise WorkerIdentityRejected("invalid_controller_policy")
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None:
            raise WorkerIdentityRejected("invalid_controller_repository")
        for value in (self.repository_id, self.repository_owner_id, self.run_id):
            if type(value) is not str or _ID.fullmatch(value) is None:
                raise WorkerIdentityRejected("invalid_controller_identifier")
        for value in (self.source_sha, self.workflow_sha):
            if type(value) is not str or _SHA.fullmatch(value) is None:
                raise WorkerIdentityRejected("invalid_controller_commit")
        workflow_prefix = self.repository + "/.github/workflows/"
        if not self.workflow_ref.startswith(workflow_prefix):
            raise WorkerIdentityRejected("invalid_controller_workflow")
        workflow = self.workflow_ref[len(workflow_prefix):]
        if re.fullmatch(
            r"[A-Za-z0-9_.-]+\.ya?ml@(?:[0-9a-f]{40}|refs/(?:heads|tags)/[A-Za-z0-9_./-]+)",
            workflow,
        ) is None:
            raise WorkerIdentityRejected("invalid_controller_workflow_reference")
        ref = workflow.split("@", 1)[1]
        if _SHA.fullmatch(ref) is not None and ref != self.workflow_sha:
            raise WorkerIdentityRejected("controller_workflow_commit_mismatch")
        if (type(self.max_token_lifetime_seconds) is not int
                or not 1 <= self.max_token_lifetime_seconds <= 600):
            raise WorkerIdentityRejected("invalid_controller_token_lifetime")


@dataclass(frozen=True)
class ActionsOIDCIdentity:
    """Signature-checked context only; no 'verified' switch or launch permission.

    A caller can construct Python objects. Handoff therefore accepts the signed
    compact token, never this object as evidence. Do not persist the raw token.
    """

    repository_id: str
    repository_owner_id: str
    source_sha: str
    workflow_ref: str
    workflow_sha: str
    run_id: str
    run_attempt: str
    expires_at: int


def _dependencies():
    # Lazy imports keep unrelated stdlib-only offline safeguards usable when
    # the locked crypto dependencies are unavailable. No fallback verifier.
    try:
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        raise WorkerIdentityRejected("oidc_crypto_dependencies_unavailable") from None
    return jwt, serialization, rsa


def _json_object(segment):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("nonfinite")

    try:
        raw = base64.b64decode(segment + "=" * (-len(segment) % 4), altchars=b"-_", validate=True)
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                         parse_constant=invalid_constant)
        if type(obj) is not dict:
            raise ValueError("not_object")
    except (ValueError, UnicodeError, RecursionError):
        raise WorkerIdentityRejected("invalid_oidc_json") from None
    return obj


@dataclass(frozen=True)
class GitHubActionsOIDCVerifier:
    """A snapshot of controller policy and independently trusted RSA public keys.

    Explicit key IDs map ONLY to these local public keys. Header key material,
    URLs and algorithm negotiation are rejected. A caller-supplied key set is
    suitable for synthetic tests, not evidence of production trust provenance.
    """

    policy: ControllerOIDCPolicy
    trusted_public_keys: tuple[tuple[str, bytes], ...] = field(repr=False)
    _keys: object = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if type(self.policy) is not ControllerOIDCPolicy:
            raise WorkerIdentityRejected("controller_policy_unavailable")
        if type(self.trusted_public_keys) is not tuple or not 1 <= len(self.trusted_public_keys) <= 16:
            raise WorkerIdentityRejected("trusted_public_keys_unavailable")
        _, serialization, rsa = _dependencies()
        parsed = {}
        for item in self.trusted_public_keys:
            if type(item) is not tuple or len(item) != 2:
                raise WorkerIdentityRejected("invalid_trusted_public_key")
            kid, pem = item
            if (not _text(kid) or kid in parsed or type(pem) is not bytes
                    or not 1 <= len(pem) <= 16384):
                raise WorkerIdentityRejected("invalid_trusted_public_key")
            try:
                key = serialization.load_pem_public_key(pem)
            except (ValueError, TypeError):
                raise WorkerIdentityRejected("invalid_trusted_public_key") from None
            if not isinstance(key, rsa.RSAPublicKey) or not 2048 <= key.key_size <= 8192:
                raise WorkerIdentityRejected("trusted_key_must_be_rsa_public_key")
            parsed[kid] = key
        object.__setattr__(self, "_keys", MappingProxyType(parsed))

    def verify(self, token, *, now):
        """Verify signature BEFORE using claims, with an injected trusted clock.

        PyJWT verifies RS256, issuer, required claims and strict string audience.
        NumericDates are checked below against `now`, rather than PyJWT's wall
        clock, for deterministic offline checks. No leeway/coercion is allowed.
        """
        if (not isinstance(now, datetime) or now.tzinfo is None
                or now.utcoffset() is None):
            raise WorkerIdentityRejected("trusted_aware_clock_required")
        if type(token) is not str or not 1 <= len(token) <= 16384:
            raise WorkerIdentityRejected("compact_oidc_token_required")
        parts = token.split(".")
        if len(parts) != 3 or any(_SEGMENT.fullmatch(part) is None for part in parts):
            raise WorkerIdentityRejected("malformed_oidc_token")
        if len(parts[0]) > 2048:
            raise WorkerIdentityRejected("invalid_oidc_header")
        header = _json_object(parts[0])
        if (set(header) - {"alg", "typ", "kid"} or header.get("alg") != "RS256"
                or header.get("typ") != "JWT" or not _text(header.get("kid"))):
            raise WorkerIdentityRejected("unsupported_oidc_header")
        key = self._keys.get(header["kid"])
        if key is None:
            raise WorkerIdentityRejected("untrusted_oidc_key_id")
        jwt, _, _ = _dependencies()
        try:
            claims = jwt.decode(
                token, key=key, algorithms=["RS256"], issuer=GITHUB_ISSUER,
                audience=self.policy.audience,
                options={
                    "verify_signature": True, "verify_iss": True,
                    "verify_aud": True, "strict_aud": True,
                    "verify_exp": False, "verify_iat": False, "verify_nbf": False,
                    "require": list(_REQUIRED),
                },
            )
        except (jwt.InvalidTokenError, ValueError, TypeError, OverflowError):
            raise WorkerIdentityRejected("oidc_signature_or_claims_rejected") from None
        # Detect duplicate JSON keys even in a correctly signed payload. Values
        # in this payload are consulted only AFTER signature verification.
        if _json_object(parts[1]) != claims:
            raise WorkerIdentityRejected("ambiguous_oidc_claims")
        expected = {
            "iss": GITHUB_ISSUER, "aud": self.policy.audience,
            "sub": self.policy.subject, "repository": self.policy.repository,
            "repository_id": self.policy.repository_id,
            "repository_owner_id": self.policy.repository_owner_id,
            "sha": self.policy.source_sha, "workflow_ref": self.policy.workflow_ref,
            "workflow_sha": self.policy.workflow_sha,
            "run_id": self.policy.run_id, "run_attempt": "1",
        }
        if any(type(claims.get(k)) is not str or claims[k] != v for k, v in expected.items()):
            raise WorkerIdentityRejected("oidc_context_mismatch")
        if any(type(claims.get(k)) is not int or claims[k] < 0 for k in ("iat", "nbf", "exp")):
            raise WorkerIdentityRejected("invalid_oidc_numeric_dates")
        issued, not_before, expires = (claims[k] for k in ("iat", "nbf", "exp"))
        if not (issued <= not_before <= now.timestamp() < expires
                and 0 < expires - issued <= self.policy.max_token_lifetime_seconds):
            raise WorkerIdentityRejected("oidc_outside_validity_window")
        return ActionsOIDCIdentity(
            repository_id=claims["repository_id"], repository_owner_id=claims["repository_owner_id"],
            source_sha=claims["sha"], workflow_ref=claims["workflow_ref"],
            workflow_sha=claims["workflow_sha"], run_id=claims["run_id"],
            run_attempt=claims["run_attempt"], expires_at=expires,
        )


# No production key provenance, approved policy, rotation or run-ID source has
# been established. Do not populate from a worker, env vars, JWT or local HEAD.
CONTROLLER_VERIFIER: GitHubActionsOIDCVerifier | None = None


def verify_controller_worker_token(token, *, now):
    """The handoff accepts no caller override for the controller trust anchor."""
    verifier = CONTROLLER_VERIFIER
    if type(verifier) is not GitHubActionsOIDCVerifier:
        raise WorkerIdentityRejected("controller_oidc_trust_unavailable")
    return verifier.verify(token, now=now)

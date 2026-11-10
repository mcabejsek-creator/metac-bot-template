"""GitHub Contents CAS storage for encrypted publication journal snapshots.

Importing this module performs no network I/O. Runtime writes use a dedicated
state branch, never the source branch. Missing/corrupt state is never treated as
an empty journal. Initialization is a separate, explicit operator operation.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
import zlib
from pathlib import Path
from urllib.parse import quote

import requests
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from publication_journal import PublicationStore, canonical
from runtime_limits import check_blocking_cancelled

MAX_FILE = 900_000  # Below GitHub's 1 MiB Contents API inline-file limit.
MAX_CLEAR = 64 * 1024 * 1024


class GitHubStateError(RuntimeError):
    pass


class MissingStateFile(GitHubStateError):
    pass


class StateConflict(GitHubStateError):
    """Conditional update was rejected, not permission to publish."""


def git_blob_sha(content):
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


class JournalCodec:
    """Domain-separated AES-GCM; no secret or journal plaintext in Git commits."""

    def __init__(self, token, context):
        if not token or len(token) < 32 or token.strip() != token:
            raise GitHubStateError("A stable, high-entropy Metaculus token is required for journal encryption")
        self.aad = canonical(["metaculus-publication-journal", 1, context]).encode()
        key = HKDF(algorithm=hashes.SHA256(), length=32,
                   salt=hashlib.sha256(self.aad).digest(), info=b"journal-encryption-only").derive(token.encode())
        self.aead = AESGCM(key)
        self.name_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                             info=b"journal-resource-filenames").derive(key)

    def resource_name(self, resource):
        return hmac.new(self.name_key, resource.encode(), hashlib.sha256).hexdigest()

    def _aad(self, resource):
        return self.aad + canonical(["resource", resource]).encode()

    def encode(self, database, resource=None):
        if len(database) > MAX_CLEAR:
            raise GitHubStateError("Journal exceeds the supported snapshot size; do not reset it")
        nonce = os.urandom(12)
        ciphertext = self.aead.encrypt(nonce, zlib.compress(database), self._aad(resource))
        encoded = canonical({"version": 1, "data": base64.b64encode(nonce + ciphertext).decode()}).encode()
        if len(encoded) > MAX_FILE:
            raise GitHubStateError("Encrypted journal exceeds the GitHub inline-file budget; publication is blocked")
        return encoded

    def decode(self, content, resource=None):
        if len(content) > MAX_FILE:
            raise GitHubStateError("Remote journal exceeds the supported size")
        try:
            envelope = json.loads(content)
            if envelope["version"] != 1:
                raise ValueError("Unsupported envelope version")
            sealed = base64.b64decode(envelope["data"], validate=True)
            compressed = self.aead.decrypt(sealed[:12], sealed[12:], self._aad(resource))
            inflater = zlib.decompressobj()
            database = inflater.decompress(compressed, MAX_CLEAR + 1)
            if len(database) > MAX_CLEAR or not inflater.eof or inflater.unconsumed_tail or inflater.unused_data:
                raise ValueError("Invalid snapshot")
            return database
        except (ValueError, KeyError, TypeError, InvalidTag, zlib.error) as error:
            raise GitHubStateError("Journal authentication/format failed; do not reinitialize after key changes or corruption") from error


class GitHubContents:
    """Only a successful conditional PUT acknowledges a changed journal state."""

    def __init__(self, repository, branch, path, token, *, session=None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise GitHubStateError("Invalid GitHub repository")
        if not branch or branch.startswith("refs/") or ".." in branch:
            raise GitHubStateError("Invalid state branch")
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", path) or ".." in path or path.startswith("/"):
            raise GitHubStateError("Invalid journal path")
        if not token:
            raise GitHubStateError("GITHUB_TOKEN is required for the state repository")
        self.repository, self.branch, self.path = repository, branch, path
        self.root = "https://api.github.com/repos/" + repository
        self.url = self.root + "/contents/" + quote(path, safe="/")
        self.session = session or requests.Session()
        self.headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28", "Cache-Control": "no-cache"}

    def _request(self, method, url, **kwargs):
        check_blocking_cancelled()
        try:
            return self.session.request(method, url, headers=self.headers, timeout=(10, 30),
                                        allow_redirects=False, **kwargs)
        except requests.RequestException as error:
            raise GitHubStateError("GitHub journal request outcome is unavailable; no Metaculus write is authorized") from error

    @staticmethod
    def _json(response):
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
            return data
        except ValueError as error:
            raise GitHubStateError("Malformed GitHub state response") from error

    def read(self):
        response = self._request("GET", self.url, params={"ref": self.branch})
        try:
            if response.status_code == 404:
                raise MissingStateFile("GitHub state file missing; existence must be checked against the manifest")
            if response.status_code != 200:
                raise GitHubStateError(f"GitHub journal unavailable (HTTP {response.status_code}); never initialize automatically")
            data = self._json(response)
            try:
                if data["type"] != "file" or data["path"] != self.path or data["encoding"] != "base64":
                    raise ValueError("Wrong state object")
                if not 0 < data["size"] <= MAX_FILE:
                    raise ValueError("Wrong state size")
                content = base64.b64decode("".join(data["content"].split()), validate=True)
                if len(content) != data["size"] or git_blob_sha(content) != data["sha"]:
                    raise ValueError("State hash mismatch")
                return content, data["sha"]
            except (KeyError, ValueError, TypeError) as error:
                raise GitHubStateError("Incomplete or inconsistent GitHub journal response") from error
        finally:
            response.close()

    def at_path(self, path):
        return GitHubContents(self.repository, self.branch, path,
                              self.headers["Authorization"].removeprefix("Bearer "), session=self.session)

    def create(self, content):
        # GitHub requires sha to UPDATE an existing file. Omitting it is an
        # atomic create-if-absent, not permission to overwrite another runner.
        self._put(None, content)

    def compare_and_swap(self, sha, content):
        self._put(sha, content)

    def _put(self, sha, content):
        payload = {
            "message": "Checkpoint Metaculus publication journal", "branch": self.branch,
            "content": base64.b64encode(content).decode(),
        }
        if sha is not None:
            payload["sha"] = sha
        response = self._request("PUT", self.url, json=payload)
        try:
            if response.status_code == 409 or (sha is None and response.status_code == 422):
                raise StateConflict("Another runner updated the journal")
            if response.status_code != (200 if sha is not None else 201):
                raise GitHubStateError(f"GitHub journal checkpoint not acknowledged (HTTP {response.status_code})")
            data = self._json(response)
            if (data.get("content", {}).get("sha") != git_blob_sha(content)
                    or not re.fullmatch(r"[0-9a-f]{40}", data.get("commit", {}).get("sha", ""))):
                raise GitHubStateError("GitHub did not acknowledge this exact journal checkpoint")
        finally:
            response.close()

    def initialize(self, content, source_sha):
        """Create the ref with a complete encrypted tree, never reset an existing ref."""
        if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            raise GitHubStateError("Initialization requires a source commit SHA")
        ref_url = self.root + "/git/refs"
        response = self._request("GET", self.root + "/git/ref/heads/" + quote(self.branch, safe=""))
        try:
            if response.status_code != 404:
                raise GitHubStateError("State ref exists or cannot be inspected; initialization must not overwrite it")
        finally:
            response.close()

        def create(endpoint, payload):
            response = self._request("POST", self.root + endpoint, json=payload)
            try:
                if response.status_code != 201:
                    raise GitHubStateError(f"Journal initialization not acknowledged (HTTP {response.status_code})")
                data = self._json(response)
                if not re.fullmatch(r"[0-9a-f]{40}", data.get("sha", "")):
                    raise GitHubStateError("Missing Git object acknowledgement")
                return data["sha"]
            finally:
                response.close()

        blob = create("/git/blobs", {"encoding": "base64", "content": base64.b64encode(content).decode()})
        tree = create("/git/trees", {"tree": [{"path": self.path, "mode": "100644", "type": "blob", "sha": blob}]})
        # An orphan state history contains no source/workflow files. This avoids
        # editing/deleting .github/workflows via a contents-only GITHUB_TOKEN.
        commit = create("/git/commits", {"message": "Initialize encrypted Metaculus publication journal from " + source_sha,
            "tree": tree, "parents": [],
            "author": {"name": "github-actions[bot]", "email": "41898282+github-actions[bot]@users.noreply.github.com"}})
        response = self._request("POST", ref_url, json={"ref": "refs/heads/" + self.branch, "sha": commit})
        try:
            if response.status_code != 201 or self._json(response).get("object", {}).get("sha") != commit:
                raise GitHubStateError("Initialization ref not acknowledged; inspect state, never reset/retry blindly")
        finally:
            response.close()


class GitHubPublicationStore:
    """Run unchanged SQLite transactions on snapshots; commit remotely before return.

    A lost PUT acknowledgement never authorizes a Metaculus write. A stale GET
    cannot overwrite a newer snapshot because PUT must match the current blob SHA.
    Only explicit conflicts are retried, and only on journal-only transactions.
    """

    def __init__(self, contents, codec):
        self.contents, self.codec = contents, codec

    def preflight(self):
        # Validate the configured snapshot even if there are no open questions.
        # This does not create state or authorize any publication.
        self._manifest()

    def registered_resources(self):
        """One authenticated index read for ordering; absence never permits a write."""
        manifest, _ = self._manifest()
        return frozenset(manifest["resources"])

    def _manifest(self):
        content, sha = self.contents.read()
        try:
            manifest = json.loads(self.codec.decode(content))
            if (manifest["version"] != 1 or not isinstance(manifest["resources"], list)
                    or len(manifest["resources"]) != len(set(manifest["resources"]))
                    or not all(isinstance(key, str) and key for key in manifest["resources"])):
                raise ValueError("Invalid resource manifest")
            return manifest, sha
        except (ValueError, KeyError, TypeError) as error:
            raise GitHubStateError("Invalid resource manifest; never reset it automatically") from error

    def _register(self, resource):
        for _ in range(3):
            manifest, sha = self._manifest()
            if resource in manifest["resources"]:
                return
            manifest["resources"] = sorted([*manifest["resources"], resource])
            try:
                self.contents.compare_and_swap(sha, self.codec.encode(canonical(manifest).encode()))
            except StateConflict:
                continue
            return
        raise GitHubStateError("Resource registration conflicted repeatedly; publication is blocked")

    def _call(self, method, *args, mutate=False, **kwargs):
        resource = args[0].split("/", 1)[0]
        shard = self.contents.at_path(self.contents.path + ".d/" + self.codec.resource_name(resource) + ".enc")
        recovery_key = None
        for _ in range(3):
            manifest, _ = self._manifest()
            registered = resource in manifest["resources"]
            try:
                content, sha = shard.read()
            except MissingStateFile as error:
                if registered:
                    raise GitHubStateError("A registered question journal is missing; do not recreate it") from error
                if method not in ("prepare", "prepare_for_run", "select_for_run", "report", "pending", "steps"):
                    raise GitHubStateError("A previously staged publication journal is missing") from error
                database, sha = None, None
            else:
                database = self.codec.decode(content, resource)
                if not database.startswith(b"SQLite format 3\0"):
                    raise GitHubStateError("Invalid question journal snapshot")
                # Recover a staged file whose registration acknowledgement was
                # lost. Positive authenticated file existence is required; never
                # reconstruct a registered-but-missing question as an empty DB.
                if not registered:
                    self._register(resource)
                    registered = True
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "journal.sqlite3"
                if database is not None:
                    path.write_bytes(database)
                local = PublicationStore(path)
                before = path.read_bytes()
                options = {**kwargs, "_recovery_key": recovery_key} if recovery_key is not None else kwargs
                result = getattr(local, method)(*args, **options)
                after = path.read_bytes()
                if mutate and before != after:
                    if method in ("select_for_run", "prepare_for_run") and result[0] != args[0]:
                        # CAS may lose to a runner which finishes this plan.
                        # Replay the observed recovery choice, never reinterpret
                        # its disappearance from pending as a new forecast.
                        recovery_key = result[0]
                    try:
                        encoded = self.codec.encode(after, resource)
                        if sha is None:
                            shard.create(encoded)
                        else:
                            shard.compare_and_swap(sha, encoded)
                    except StateConflict:
                        continue
                    # Only a positively acknowledged file and durable index may
                    # authorize a subsequent claim/Metaculus POST.
                    if not registered:
                        self._register(resource)
                return result
        raise GitHubStateError("Journal update conflicted repeatedly; publication is blocked")

    def report(self, key):
        return self._call("report", key)

    def pending(self, resource):
        return self._call("pending", resource)

    def steps(self, key):
        return self._call("steps", key)

    def prepare(self, key, report, operations):
        return self._call("prepare", key, report, operations, mutate=True)

    def select_for_run(self, key, skip_previous, require_saved=False):
        return self._call("select_for_run", key, skip_previous, require_saved, mutate=True)

    def prepare_for_run(self, key, report, operations, skip_previous, require_saved=False):
        return self._call("prepare_for_run", key, report, operations, skip_previous, require_saved, mutate=True)

    def claim(self, key, position):
        return self._call("claim", key, position, mutate=True)

    def finish(self, key, position, state):
        return self._call("finish", key, position, state, mutate=True)

    def reconcile(self, key, position, operation_hash, *, accepted):
        return self._call("reconcile", key, position, operation_hash, accepted=accepted, mutate=True)


def configured_store():
    """Called lazily by the bot; constructing configuration itself performs no I/O."""
    repository = os.getenv("GITHUB_REPOSITORY", "")
    branch = os.getenv("METACULUS_PUBLICATION_STATE_BRANCH", "metaculus-publication-state")
    path = os.getenv("METACULUS_PUBLICATION_STATE_PATH", "publication.enc")
    scope = os.getenv("METACULUS_PUBLICATION_SCOPE", "default-bot")
    contents = GitHubContents(repository, branch, path, os.getenv("GITHUB_TOKEN", ""))
    codec = JournalCodec(os.getenv("METACULUS_TOKEN", ""), [repository, branch, path, scope])
    return GitHubPublicationStore(contents, codec)


# Explicitly preserve the distinction between abandoning legacy attempts and
# proving the outcome of every old Metaculus POST. This authenticated metadata
# remains encrypted with the rest of the journal, and registry updates preserve it.
LEGACY_FREEZE_POLICY = {
    "mode": "legacy-frozen-no-retries",
    "historic_outcomes": "not-fully-reconciled",
    "disposition": "abandoned-without-replay",
}


def initial_legacy_frozen_manifest():
    """New journal starts a NEW publication epoch, never imports old intents."""
    return canonical({
        "version": 1,
        "resources": [],
        "legacy_cutover": LEGACY_FREEZE_POLICY,
    }).encode()


def main():
    parser = argparse.ArgumentParser(
        description="Initialize once, after explicit permanent freeze of historical runs; never reset an existing journal"
    )
    parser.add_argument("--initialize", action="store_true", required=True)
    parser.add_argument("--confirm-legacy-frozen-no-retries", action="store_true", required=True)
    parser.parse_args()
    store = configured_store()
    manifest = initial_legacy_frozen_manifest()
    store.contents.initialize(store.codec.encode(manifest), os.getenv("GITHUB_SHA", ""))
    print("New legacy-frozen encrypted journal initialized; no Metaculus request was made.")


if __name__ == "__main__":
    main()


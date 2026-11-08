"""Opt-in read-only audit of the *existing encrypted GitHub publication journal*.

No Metaculus API calls, forecast or comment POSTs, model calls, GitHub writes,
secret logging, raw journal exports, or side effects on import.

The --live switch must be explicitly enabled by a separately approved job;
normally CI only runs --self-check on fully synthetic data.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github_publication import (
    GitHubStateError, LEGACY_FREEZE_POLICY, configured_store,
    GitHubContents, GitHubPublicationStore, JournalCodec,
)

MAX_RESOURCES = 100
MAX_GITHUB_GETS = 2 + 2 * MAX_RESOURCES
STATES = ("ready", "sending", "confirmed", "rejected", "uncertain")


class GetOnlyBoundedSession:
    """Enforce the operator-approved GET budget on every GitHub HTTP attempt.

    Shard-specific GitHubContents instances share this one session and budget.
    No PUT, POST, redirects, other hosts, or non-state-branch reads are allowed.
    """

    def __init__(self, transport, contents):
        self.transport = transport
        self.calls = 0
        self.branch = contents.branch
        self.root_url = contents.url

    def request(self, method, url, **kwargs):
        if (method != "GET" or
                not isinstance(url, str) or
                not (url == self.root_url or url.startswith(self.root_url + ".d/")) or
                kwargs.get("params") != {"ref": self.branch} or
                kwargs.get("allow_redirects") is not False):
            raise GitHubStateError("Read-only audit refused an unsafe network request")
        if self.calls >= MAX_GITHUB_GETS:
            raise GitHubStateError("Read-only audit exhausted 202 GitHub GET attempts")
        self.calls += 1  # count attempts, including failed HTTP requests
        return self.transport.request(method, url, **kwargs)


def _count_shard(blob):
    if not blob.startswith(b"SQLite format 3\x00"):
        raise GitHubStateError("Audit refused: invalid shard SQLite header")
    db = sqlite3.connect(":memory:")
    try:
        if not hasattr(db, "deserialize"):
            raise GitHubStateError("Audit refused: SQLite memory deserialize unsupported")
        db.deserialize(blob)
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise GitHubStateError("Audit refused: corrupt SQLite shard")
        if db.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise GitHubStateError("Audit refused: unexpected journal schema")
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not {"publications", "steps", "publication_aliases"}.issubset(tables):
            raise GitHubStateError("Audit refused: missing journal tables")
        publications = db.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
        rows = db.execute("SELECT state, COUNT(*) FROM steps GROUP BY state").fetchall()
        counts = dict.fromkeys(STATES, 0)
        for state, amount in rows:
            if state not in counts or amount < 0:
                raise GitHubStateError("Audit refused: unknown step state")
            counts[state] = amount
        if publications != db.execute(
            "SELECT COUNT(*) FROM (SELECT p.key FROM publications p "
            "LEFT JOIN steps s ON s.publication_key=p.key "
            "GROUP BY p.key HAVING COUNT(s.position) > 0)"
        ).fetchone()[0]:
            raise GitHubStateError("Audit refused: publication without operations")
        if db.execute(
            "SELECT COUNT(*) FROM steps s LEFT JOIN publications p "
            "ON s.publication_key=p.key WHERE p.key IS NULL"
        ).fetchone()[0]:
            raise GitHubStateError("Audit refused: orphaned journal step")
        return publications, counts
    except sqlite3.DatabaseError as error:
        raise GitHubStateError("Audit refused: malformed SQLite shard") from error
    finally:
        db.close()


def audit_store(store):
    """All calls to GitHubPublicationStore are reads; output only safe counts."""
    manifest, first_manifest_sha = store._manifest()
    resources = manifest["resources"]
    if len(resources) > MAX_RESOURCES:
        raise GitHubStateError("Audit refused: registered journal exceeds bounded shard cap")

    policy = manifest.get("legacy_cutover")
    # Exact legacy policy is evidence of a frozen new publication epoch, NOT a
    # reconciliation of older Metaculus posts or legacy journal attempts.
    recognized_policy = policy == LEGACY_FREEZE_POLICY
    if policy is not None and not isinstance(policy, dict):
        raise GitHubStateError("Audit refused: unexpected legacy policy schema")

    steps = dict.fromkeys(STATES, 0)
    plans = 0
    observed = []
    for resource in resources:
        # Resource hashes hide IDs and text in paths and reports.
        path = store.contents.path + ".d/" + store.codec.resource_name(resource) + ".enc"
        shard = store.contents.at_path(path)
        content, sha = shard.read()
        decoded = store.codec.decode(content, resource)
        local_plans, local_steps = _count_shard(decoded)
        plans += local_plans
        for state in STATES:
            steps[state] += local_steps[state]
        observed.append((shard, sha))

    # Re-read every shard and the index. A changing journal cannot be given
    # an authoritative whole-inventory status; fail closed instead.
    for shard, expected in observed:
        _, sha = shard.read()
        if sha != expected:
            raise GitHubStateError("Audit refused: journal changed during inspection")
    manifest_after, after_sha = store._manifest()
    if after_sha != first_manifest_sha or manifest_after != manifest:
        raise GitHubStateError("Audit refused: manifest changed during inspection")

    uncertain = steps["sending"] + steps["uncertain"]
    pending = steps["ready"] + steps["rejected"]
    return {
        "mode": "github_encrypted_journal_readonly_audit",
        "snapshot_consistent_on_second_read": True,
        "encrypted_shards_authenticated": True,
        "registered_resources": len(resources),
        "frozen_publication_intents": plans,
        "step_counts": steps,
        "uncertain_in_flight_steps": uncertain,
        "ready_or_rejected_steps": pending,
        "all_new_epoch_steps_confirmed": pending == 0 and uncertain == 0,
        "expected_legacy_freeze_policy": recognized_policy,
        "historic_outcomes_fully_reconciled": False,
        "live_metaculus_requests": 0,
        "live_metaculus_posts": 0,
        "paid_model_calls": 0,
        "github_write_requests": 0,
        "question_ids_titles_reports_not_logged": True,
        "limitations": (
            "This reads only the current encrypted GitHub journal epoch. "
            "Legacy forecasts are marked frozen without a completed external "
            "reconciliation, and this audit does not prove whether old "
            "Metaculus forecasts were accepted. No publication is authorized."
        ),
    }


def self_check():
    # Only synthetic secrets and the in-memory fake GitHub transport.
    from tests.test_github_publication import (
        MemoryGitHub, METACULUS_KEY, GITHUB_KEY, REPOSITORY, BRANCH, PATH, CONTEXT,
    )

    codec = JournalCodec(METACULUS_KEY, CONTEXT)
    manifest = {
        "version": 1, "resources": [], "legacy_cutover": LEGACY_FREEZE_POLICY,
    }
    fake = MemoryGitHub(codec.encode(json.dumps(manifest).encode()))

    def new_store():
        return GitHubPublicationStore(
            GitHubContents(REPOSITORY, BRANCH, PATH, GITHUB_KEY, session=fake), codec
        )

    store = new_store()
    operation = [
        {"kind": "forecast", "question_id": 1234,
         "payload": {"probability_yes": .65}},
        {"kind": "comment", "post_id": 4567,
         "text": "private test text", "is_private": True,
         "included_forecast": True},
    ]
    store.prepare("synthetic/initial", '{"private":"test-report"}', operation)
    assert store.claim("synthetic/initial", 0)
    store.finish("synthetic/initial", 0, "confirmed")
    assert store.claim("synthetic/initial", 1)

    write_count = sum(method != "GET" for method, _ in fake.calls)
    report = audit_store(new_store())
    assert sum(method != "GET" for method, _ in fake.calls) == write_count
    assert report["registered_resources"] == 1
    assert report["frozen_publication_intents"] == 1
    assert (report["step_counts"]["confirmed"], report["step_counts"]["sending"]) == (1, 1)
    assert not report["all_new_epoch_steps_confirmed"]
    assert report["expected_legacy_freeze_policy"]
    assert report["historic_outcomes_fully_reconciled"] is False
    public = json.dumps(report)
    assert "test-report" not in public and "test text" not in public
    assert "1234" not in public and "4567" not in public

    # Test the same HTTP boundary used by the live audit without any network.
    from unittest.mock import Mock
    transport = Mock()
    contents = new_store().contents
    gate = GetOnlyBoundedSession(transport, contents)
    legal_kwargs = {"params": {"ref": BRANCH}, "allow_redirects": False}
    for forbidden_method, forbidden_url, forbidden_kwargs in (
        ("POST", contents.url, legal_kwargs),
        ("PUT", contents.url, legal_kwargs),
        ("GET", "https://www.metaculus.com/api/posts/", legal_kwargs),
        ("GET", contents.url, {"params": {"ref": "main"}, "allow_redirects": False}),
        ("GET", contents.url, {"params": {"ref": BRANCH}, "allow_redirects": True}),
    ):
        try:
            gate.request(forbidden_method, forbidden_url, **forbidden_kwargs)
        except GitHubStateError:
            pass
        else:
            raise AssertionError("Read-only HTTP boundary accepted unsafe request")
    assert gate.calls == 0 and transport.request.call_count == 0
    for _ in range(MAX_GITHUB_GETS):
        gate.request("GET", contents.url, **legal_kwargs)
    assert gate.calls == MAX_GITHUB_GETS
    try:
        gate.request("GET", contents.url, **legal_kwargs)
    except GitHubStateError:
        pass
    else:
        raise AssertionError("Audit permitted a 203rd GitHub GET")
    assert transport.request.call_count == MAX_GITHUB_GETS

    # A torn read fails closed rather than reporting a misleading safe result.
    fake.stale_reads = []  # The fake does not serve stale root manifests.
    fake.status = 403
    try:
        audit_store(new_store())
    except GitHubStateError:
        pass
    else:
        raise AssertionError("Unreadable encrypted index was considered healthy")
    print("Offline journal audit self-check passed; 0 network/POST/model operations",
          flush=True)


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--self-check", action="store_true")
    modes.add_argument("--approved-readonly", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if os.environ.get("VRCHCAB_APPROVE_READONLY_JOURNAL_AUDIT") != "YES_GITHUB_GET_ONLY":
        print("Journal audit refused: fresh one-time authorization required",
              file=sys.stderr)
        sys.exit(2)
    if not os.getenv("METACULUS_TOKEN") or not os.getenv("GITHUB_TOKEN"):
        print("Journal audit refused: credentials missing", file=sys.stderr)
        sys.exit(2)
    try:
        store = configured_store()
        bounded_session = GetOnlyBoundedSession(store.contents.session, store.contents)
        store.contents.session = bounded_session
        report = audit_store(store)
        report["github_get_attempts"] = bounded_session.calls
        report["maximum_github_get_attempts"] = MAX_GITHUB_GETS
        print(json.dumps(report, sort_keys=True), flush=True)
    except Exception:
        # Never echo API responses, hashed resources, decrypted data or secrets.
        print("READ_ONLY_JOURNAL_AUDIT_FAILED_CLOSED; no state changed",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

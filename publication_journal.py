"""Standard-library publication journal and coordinator; no SDK or network imports."""

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class PublicationUncertain(RuntimeError):
    """The write may be in flight or accepted; external reconciliation is needed."""


class PublicationRejected(RuntimeError):
    """An explicit HTTP rejection: this step can be attempted on a later run."""

    def __init__(self, status):
        self.status = status
        super().__init__(f"Publication explicitly rejected (HTTP {status})")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class PublicationStore:
    """Atomic claims are committed before HTTP; no locks/transactions span HTTP."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise RuntimeError("Unsupported publication journal schema")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS publications (
                    key TEXT PRIMARY KEY, resource TEXT NOT NULL, report TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS steps (
                    publication_key TEXT NOT NULL REFERENCES publications(key),
                    position INTEGER NOT NULL, operation TEXT NOT NULL,
                    operation_hash TEXT NOT NULL, state TEXT NOT NULL,
                    PRIMARY KEY (publication_key, position),
                    CHECK (state IN ('ready', 'sending', 'confirmed', 'rejected', 'uncertain'))
                );
                CREATE INDEX IF NOT EXISTS publications_resource ON publications(resource);
                CREATE TABLE IF NOT EXISTS publication_aliases (
                    key TEXT PRIMARY KEY,
                    publication_key TEXT NOT NULL REFERENCES publications(key),
                    CHECK (key != publication_key)
                );
                PRAGMA user_version = 2;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA synchronous = FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def report(self, key):
        with self.connect() as db:
            key = self._canonical_key(db, key)
            row = db.execute("SELECT report FROM publications WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

    @staticmethod
    def _canonical_key(db, key):
        alias = db.execute("SELECT publication_key FROM publication_aliases WHERE key = ?", (key,)).fetchone()
        if alias is None:
            return key
        target = db.execute("SELECT resource FROM publications WHERE key = ?", (alias[0],)).fetchone()
        collision = db.execute("SELECT 1 FROM publications WHERE key = ?", (key,)).fetchone()
        if target is None or target[0] != key.split("/", 1)[0] or collision:
            raise PublicationUncertain("Invalid publication binding; do not create a new forecast")
        return alias[0]

    @staticmethod
    def _pending(db, resource):
        rows = db.execute("""SELECT DISTINCT p.key FROM publications p JOIN steps s ON s.publication_key = p.key
            WHERE p.resource = ? AND s.state != 'confirmed' LIMIT 2""", (resource,)).fetchall()
        if len(rows) > 1:
            raise PublicationUncertain("Multiple incomplete plans for one question; reconcile before continuing")
        return rows[0][0] if rows else None

    def _select_for_run(self, db, key, skip_previous, recovery_key):
        canonical_key = self._canonical_key(db, key)
        exists = db.execute("SELECT 1 FROM publications WHERE key = ?", (canonical_key,)).fetchone()
        resource = key.split("/", 1)[0]
        if recovery_key is not None:
            if exists:
                return canonical_key  # An already committed binding wins.
            target = db.execute("SELECT resource FROM publications WHERE key = ?", (recovery_key,)).fetchone()
            if target is None or target[0] != resource:
                raise PublicationUncertain("Observed recovery plan disappeared or changed resource; do not reforecast")
            selected = recovery_key
        else:
            selected = self._pending(db, resource)
        if selected is None and exists:
            return canonical_key
        if selected is None and skip_previous:
            # A confirmed journal receipt is stronger than a stale API history
            # flag. Initial/tournament mode must not reforecast a completed Cup.
            row = db.execute("""SELECT p.key FROM publications p WHERE p.resource = ?
                AND EXISTS (SELECT 1 FROM steps s WHERE s.publication_key = p.key)
                AND NOT EXISTS (SELECT 1 FROM steps s WHERE s.publication_key = p.key AND s.state != 'confirmed')
                ORDER BY p.rowid DESC LIMIT 1""", (resource,)).fetchone()
            selected = row[0] if row else None
        if selected is not None and not exists:
            # Commit this immutable binding BEFORE draining the old plan. A
            # crash after its last ACK must not turn a rerun into a new intent.
            db.execute("INSERT INTO publication_aliases VALUES (?, ?)", (key, selected))
        return selected

    @staticmethod
    def _insert_plan(db, key, report, operations):
        if not operations:
            raise ValueError("A publication must contain at least one write")
        encoded = [canonical(operation) for operation in operations]
        db.execute("INSERT INTO publications VALUES (?, ?, ?)", (key, key.split("/", 1)[0], report))
        db.executemany("INSERT INTO steps VALUES (?, ?, ?, ?, 'ready')",
                       [(key, i, operation, digest(operation)) for i, operation in enumerate(encoded)])

    def _for_run(self, key, skip_previous, report=None, operations=None, recovery_key=None, require_saved=False):
        if type(skip_previous) is not bool or type(require_saved) is not bool:
            raise ValueError("Publication selection flags must be booleans")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            selected = self._select_for_run(db, key, skip_previous, recovery_key)
            if selected is not None:
                saved = db.execute("SELECT report FROM publications WHERE key = ?", (selected,)).fetchone()[0]
                return selected, saved, False
            if require_saved:
                raise PublicationUncertain("Reforecasting workflow rerun has no durable intent binding; review the previous attempt before starting a new run")
            if report is None:
                return key, None, False
            self._insert_plan(db, key, report, operations)
            return key, report, True

    def select_for_run(self, key, skip_previous, require_saved=False, *, _recovery_key=None):
        """Durably bind recovery, or find a saved run, before any model work."""
        return self._for_run(key, skip_previous, recovery_key=_recovery_key, require_saved=require_saved)

    def prepare_for_run(self, key, report, operations, skip_previous, require_saved=False, *, _recovery_key=None):
        """Atomically recheck competing plans after model work, before saving."""
        return self._for_run(key, skip_previous, report, operations, _recovery_key, require_saved)

    def prepare(self, key, report, operations):
        """Freeze the entire first report/plan before its first outbound write."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            key = self._canonical_key(db, key)
            existing = db.execute("SELECT report FROM publications WHERE key = ?", (key,)).fetchone()
            if existing:
                return existing[0], False
            resource = key.split("/", 1)[0]
            pending = db.execute("""SELECT p.key FROM publications p JOIN steps s ON s.publication_key = p.key
                WHERE p.resource = ? AND s.state != 'confirmed' LIMIT 1""", (resource,)).fetchone()
            if pending:
                raise PublicationUncertain(f"Previous publication {pending[0]} is incomplete; finish/reconcile it first")
            self._insert_plan(db, key, report, operations)
            return report, True

    def pending(self, resource):
        with self.connect() as db:
            return self._pending(db, resource)

    def steps(self, key):
        with self.connect() as db:
            key = self._canonical_key(db, key)
            return [dict(row) for row in db.execute(
                "SELECT position, operation, operation_hash, state FROM steps WHERE publication_key = ? ORDER BY position", (key,))]

    def claim(self, key, position):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            key = self._canonical_key(db, key)
            row = db.execute("SELECT state FROM steps WHERE publication_key = ? AND position = ?", (key, position)).fetchone()
            if row is None:
                raise RuntimeError("Missing publication step")
            if row[0] == "confirmed":
                return False
            if row[0] in ("sending", "uncertain"):
                raise PublicationUncertain(f"Publication {key}, step {position}: in flight or uncertain; do not resend")
            db.execute("UPDATE steps SET state = 'sending' WHERE publication_key = ? AND position = ?", (key, position))
            return True

    def finish(self, key, position, state):
        if state not in ("confirmed", "rejected", "uncertain"):
            raise ValueError("Invalid publication outcome")
        with self.connect() as db:
            key = self._canonical_key(db, key)
            changed = db.execute("UPDATE steps SET state = ? WHERE publication_key = ? AND position = ? AND state = 'sending'",
                                 (state, key, position)).rowcount
            if changed != 1:
                raise RuntimeError("Publication claim lost; stop and reconcile")

    def reconcile(self, key, position, operation_hash, *, accepted):
        """Operator-only: call after verifying remote state AND stopping old workers.

        accepted=True records an already-applied write. False permits a new send
        only after externally establishing that the original was NOT applied.
        Never guess from an empty/stale remote read. This method does no API I/O.
        """
        if not isinstance(accepted, bool):
            raise ValueError("accepted must be an explicit boolean")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            key = self._canonical_key(db, key)
            changed = db.execute("""UPDATE steps SET state = ?
                WHERE publication_key = ? AND position = ? AND operation_hash = ?
                AND state IN ('sending', 'uncertain')""",
                ("confirmed" if accepted else "ready", key, position, operation_hash)).rowcount
            if changed != 1:
                raise ValueError("Reconciliation requires the exact uncertain operation")


class Publisher:
    def __init__(self, store, writer):
        self.store = store
        self.writer = writer

    def drain(self, key):
        steps = self.store.steps(key)
        if not steps:
            raise ValueError("Missing publication plan")
        for step in steps:
            if not self.store.claim(key, step["position"]):
                continue
            try:
                self.writer(json.loads(step["operation"]))
            except PublicationRejected:
                self.store.finish(key, step["position"], "rejected")
                raise
            except BaseException as error:
                self.store.finish(key, step["position"], "uncertain")
                if not isinstance(error, Exception):
                    raise
                raise PublicationUncertain(f"Publication {key}, step {step['position']}: outcome uncertain; do not resend") from error
            else:
                # If the acknowledgement cannot be persisted, 'sending' remains
                # durable and future processes refuse to resend this operation.
                self.store.finish(key, step["position"], "confirmed")

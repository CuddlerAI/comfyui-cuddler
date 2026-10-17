"""Job and upload records in SQLite.

A job record holds a run fingerprint, the idempotency key used to create the
job, what the create request added to the run's inputs (upload ids, max_usd and
the client tag, so that an unanswered request can be sent again unchanged), the
job id, its last known status and an opaque credential binding. No key, prompt or media.

Every failure surfaces as CuddlerError("state_unavailable"): a locked, read-only
or unopenable database, a full disk. A damaged database raises recovery_required
and is kept intact: replacing it with an empty one could forget an accepted,
paid request and charge for it again.
"""
from contextlib import closing, contextmanager
import os
import sqlite3
import time

from .errors import CuddlerError
from .files import ensure_private_dir, write_private

JOB_TTL = 7 * 86400              # the Platform keeps a result for 7 days after the job finishes
JOB_GRACE = 86400                # allow a day beyond the terminal job's output retention
UPLOAD_TTL = 29 * 86400          # ready uploads are kept 30 days; reuse them one day less
UNFINISHED_UPLOAD_TTL = 3600     # a PUT must happen within 15 minutes
INITIALIZED_MARKER = "state.initialized"

_DAMAGED = (11, 26)              # SQLITE_CORRUPT, SQLITE_NOTADB
_REQUIRED_COLUMNS = {
    "jobs": {"fingerprint", "kind", "idempotency_key", "job_id", "status", "created_at", "updated_at", "request"},
    "uploads": {"digest", "idempotency_key", "upload_id", "status", "created_at"},
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  fingerprint TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  job_id TEXT,
  status TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  request TEXT,
  credential_identity TEXT
);
CREATE TABLE IF NOT EXISTS uploads (
  digest TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL,
  upload_id TEXT,
  status TEXT NOT NULL,
  created_at REAL NOT NULL,
  credential_identity TEXT
);
"""


@contextmanager
def _coded_failures():
    """Turn a database or file-system failure into the one error the plugin explains."""
    try:
        yield
    except sqlite3.Error as error:
        if _is_damaged(error):
            raise CuddlerError("recovery_required", status=500) from error
        raise CuddlerError("state_unavailable", status=500, detail={"reason": str(error)}) from error
    except OSError as error:
        raise CuddlerError("state_unavailable", status=500,
                           detail={"reason": error.strerror or type(error).__name__}) from error


def _is_damaged(error):
    """True when the file is not a readable database (as opposed to busy, read-only or full)."""
    code = getattr(error, "sqlite_errorcode", None)  # Python 3.11+
    if code is not None:
        return (code & 0xFF) in _DAMAGED
    return type(error) is sqlite3.DatabaseError and any(
        text in str(error) for text in ("not a database", "malformed"))


def _exists(path):
    """A permission/I/O failure is not proof that the file was never there."""
    try:
        path.stat()
        return True
    except FileNotFoundError:
        return False


class StateDB:
    def __init__(self, directory, clock=time.time, *, timeout=10):
        self._clock = clock
        self._timeout = timeout
        with _coded_failures():
            self.path = ensure_private_dir(directory) / "state.sqlite3"
            self.marker = self.path.with_name(INITIALIZED_MARKER)
            self._check_missing()
            self._prepare()
            # Initialization must finish before a runner can submit anything.
            # A failed marker write makes opening the state fail as well.
            if not _exists(self.marker):
                write_private(self.marker, b"1\n")

    def _check_missing(self):
        if not _exists(self.path) and (_exists(self.marker) or _exists(self.path.with_name("credentials.json"))):
            raise CuddlerError("recovery_required", status=500)

    def _prepare(self):
        with closing(self._connect(initialize=True)) as db:
            if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("database disk image is malformed")
            db.executescript(_SCHEMA)
            # Existing records have no trusted identity. Keep NULL until a read
            # with the current key proves access to their known job/upload ID.
            for table in ("jobs", "uploads"):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if "credential_identity" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN credential_identity TEXT")
        if os.name == "posix":
            os.chmod(self.path, 0o600)

    def _connect(self, *, initialize=False):
        self._check_missing()
        existing = _exists(self.path)
        if not existing and not initialize:
            raise CuddlerError("recovery_required", status=500)
        db = sqlite3.connect(self.path, timeout=self._timeout)
        db.row_factory = sqlite3.Row
        try:
            if existing:
                # A zero-byte file or another valid but empty SQLite database
                # is lost state, not a first install. Validate before CREATE,
                # and on ordinary reads/writes if the file changed at runtime.
                for table, required in _REQUIRED_COLUMNS.items():
                    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                    if not required <= columns:
                        raise CuddlerError("recovery_required", status=500)
        except BaseException:
            db.close()
            raise
        return db

    def _one(self, sql, args):
        with _coded_failures(), closing(self._connect()) as db:
            row = db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _write(self, sql, args=()):
        with _coded_failures(), closing(self._connect()) as db, db:
            db.execute(sql, args)

    # Jobs.
    def job(self, fingerprint):
        return self._one("SELECT * FROM jobs WHERE fingerprint = ?", (fingerprint,))

    def begin_job(self, fingerprint, kind, idempotency_key, request=None, *, credential_identity=None):
        """Record the key a create request is about to carry, and (`request`, JSON text) what that request adds to
        the run's inputs. Replaces whatever the run had: the caller has checked it is dead or absent."""
        now = self._clock()
        self._write("INSERT OR REPLACE INTO jobs (fingerprint, kind, idempotency_key, job_id, status, created_at, "
                    "updated_at, request, credential_identity) VALUES (?, ?, ?, NULL, 'submitting', ?, ?, ?, ?)",
                    (fingerprint, kind, idempotency_key, now, now, request, credential_identity))

    def update_job(self, fingerprint, *, status, job_id=None, credential_identity=None):
        self._write("UPDATE jobs SET status = ?, job_id = COALESCE(?, job_id), updated_at = ?, "
                    "credential_identity = COALESCE(?, credential_identity) WHERE fingerprint = ?",
                    (status, job_id, self._clock(), credential_identity, fingerprint))

    # Uploads.
    def upload(self, digest):
        return self._one("SELECT * FROM uploads WHERE digest = ?", (digest,))

    def begin_upload(self, digest, idempotency_key, *, credential_identity=None):
        self._write("INSERT OR REPLACE INTO uploads "
                    "(digest, idempotency_key, upload_id, status, created_at, credential_identity) "
                    "VALUES (?, ?, NULL, 'creating', ?, ?)",
                    (digest, idempotency_key, self._clock(), credential_identity))

    def remember_upload(self, digest, upload_id, *, status="creating", credential_identity=None):
        self._write("UPDATE uploads SET upload_id = ?, status = ?, created_at = ?, "
                    "credential_identity = COALESCE(?, credential_identity) WHERE digest = ?",
                    (upload_id, status, self._clock(), credential_identity, digest))

    def finish_upload(self, digest, upload_id, *, credential_identity=None):
        self.remember_upload(digest, upload_id, status="ready", credential_identity=credential_identity)

    def drop_upload(self, digest):
        self._write("DELETE FROM uploads WHERE digest = ?", (digest,))

    def cleanup(self):
        """Forget what the Platform has forgotten. A job is kept from its last update, not its start, and a
        day longer: its result's seven days run from when it finished, and keeping a row too long costs one
        GET, while dropping it too soon costs a second payment."""
        now = self._clock()
        with _coded_failures(), closing(self._connect()) as db, db:
            # Unknown or unfinished paid requests must outlive output retention:
            # forgetting them would turn a recovery into a new paid request.
            db.execute("DELETE FROM jobs WHERE status IN ('succeeded', 'failed', 'canceled') AND updated_at < ?",
                       (now - JOB_TTL - JOB_GRACE,))
            db.execute("DELETE FROM uploads WHERE (status = 'ready' AND created_at < ?) "
                       "OR (status != 'ready' AND upload_id IS NOT NULL AND credential_identity IS NOT NULL "
                       "AND created_at < ?)",
                       (now - UPLOAD_TTL, now - UNFINISHED_UPLOAD_TTL))

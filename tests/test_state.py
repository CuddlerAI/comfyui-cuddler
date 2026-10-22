from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import unittest
from unittest import mock

from cuddler_plugin.cuddler import state
from cuddler_plugin.cuddler.errors import CuddlerError
from cuddler_plugin.cuddler.state import INITIALIZED_MARKER, JOB_GRACE, JOB_TTL, UNFINISHED_UPLOAD_TTL, UPLOAD_TTL, StateDB


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1_000_000.0
        self.db = StateDB(Path(self.tmp.name), clock=lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def test_job_lifecycle(self):
        self.assertIsNone(self.db.job("fp"))
        self.db.begin_job("fp", "video", "job-key-1")
        self.assertEqual(self.db.job("fp")["status"], "submitting")
        self.db.update_job("fp", expected_key="job-key-1", status="queued", job_id="vid_x")
        self.db.update_job("fp", expected_key="job-key-1", status="succeeded")
        row = self.db.job("fp")
        self.assertEqual((row["status"], row["job_id"], row["idempotency_key"]), ("succeeded", "vid_x", "job-key-1"))

    def test_begin_job_replaces_an_old_attempt(self):
        self.db.begin_job("fp", "video", "job-key-1")
        self.db.update_job("fp", expected_key="job-key-1", status="failed", job_id="vid_x")
        self.db.begin_job("fp", "video", "job-key-2", previous_key="job-key-1")
        row = self.db.job("fp")
        self.assertEqual((row["idempotency_key"], row["job_id"], row["status"]), ("job-key-2", None, "submitting"))

    def test_stale_reservation_cannot_replace_a_newer_attempt(self):
        self.db.begin_job("fp", "video", "old")
        self.db.update_job("fp", expected_key="old", status="failed")
        self.db.begin_job("fp", "video", "new", previous_key="old")
        self.db.update_job("fp", expected_key="new", status="failed")
        with self.assertRaises(CuddlerError) as caught:
            self.db.begin_job("fp", "video", "stale", previous_key="old")
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertEqual(self.db.job("fp")["idempotency_key"], "new")

    def test_terminal_reservation_does_not_authorize_credential_change(self):
        self.db.begin_job("fp", "video", "old", credential_identity="first")
        self.db.update_job("fp", expected_key="old", status="failed")
        with self.assertRaises(CuddlerError):
            self.db.begin_job("fp", "video", "new", previous_key="old", credential_identity="second")
        self.assertEqual(self.db.job("fp")["credential_identity"], "first")

    def test_delayed_updates_cannot_mutate_a_replacement_attempt(self):
        self.db.begin_job("fp", "video", "old", credential_identity="first")
        self.db.update_job("fp", expected_key="old", status="failed")
        self.db.begin_job("fp", "video", "new", previous_key="old", credential_identity="first")
        self.db.update_job("fp", expected_key="new", status="queued", job_id="vid_new")
        original = self.db.job("fp")
        self.now += 60
        for status in ("queued", "running", "succeeded", "failed", "canceled"):
            with self.subTest(status=status), self.assertRaises(CuddlerError) as caught:
                self.db.update_job("fp", expected_key="old", status=status,
                                   job_id="vid_old", credential_identity="stale")
            self.assertEqual(caught.exception.code, "recovery_required")
            self.assertEqual(self.db.job("fp"), original)

    def test_a_missing_attempt_is_not_silently_recreated_by_a_reply(self):
        with self.assertRaises(CuddlerError) as caught:
            self.db.update_job("missing", expected_key="old", status="queued", job_id="vid_old")
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertIsNone(self.db.job("missing"))

    def test_upload_lifecycle(self):
        self.db.begin_upload("d:image", "upl-key")
        self.assertEqual(self.db.upload("d:image")["status"], "creating")
        self.db.finish_upload("d:image", "upl_x")
        self.assertEqual(self.db.upload("d:image")["upload_id"], "upl_x")
        self.db.drop_upload("d:image")
        self.assertIsNone(self.db.upload("d:image"))

    def test_cleanup_honours_platform_retention(self):
        self.db.begin_job("old", "video", "k1")
        self.db.begin_upload("ready-old", "k2")
        self.db.finish_upload("ready-old", "upl_a")
        self.db.begin_upload("stuck", "k3")
        self.now += UNFINISHED_UPLOAD_TTL + 1
        self.db.begin_job("new", "video", "k4")
        self.db.cleanup()
        self.assertIsNotNone(self.db.upload("stuck"))  # its unanswered create cannot be treated as never sent
        self.assertIsNotNone(self.db.upload("ready-old"))
        self.now += max(JOB_TTL, UPLOAD_TTL)
        self.db.cleanup()
        self.assertIsNotNone(self.db.job("old"))  # unanswered paid requests are never aged out
        self.assertIsNone(self.db.upload("ready-old"))

    def test_survives_reopening(self):
        self.db.begin_job("fp", "image", "k")
        again = StateDB(Path(self.tmp.name), clock=lambda: self.now)
        self.assertEqual(again.job("fp")["kind"], "image")


class StateFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.now = 1_000_000.0

    def open(self, **kwargs):
        return StateDB(self.dir, clock=lambda: self.now, **kwargs)

    def test_a_file_that_is_not_a_database_blocks_recovery_without_replacing_it(self):
        garbage = b"this is not a database " * 40
        (self.dir / "state.sqlite3").write_bytes(garbage)
        for _ in range(2):
            with self.assertRaises(CuddlerError) as caught:
                self.open()
            self.assertEqual(caught.exception.code, "recovery_required")
            self.assertEqual((self.dir / "state.sqlite3").read_bytes(), garbage)
        self.assertEqual(os.listdir(self.dir), ["state.sqlite3"])

    def test_a_journal_left_by_a_damaged_file_is_not_moved_or_replayed_into_a_new_file(self):
        for name in ("state.sqlite3", "state.sqlite3-journal"):
            (self.dir / name).write_bytes(b"left over " + name.encode())
        with mock.patch.object(StateDB, "_prepare", side_effect=sqlite3.DatabaseError("file is not a database")):
            with self.assertRaises(CuddlerError) as caught:
                self.open()
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertEqual(sorted(os.listdir(self.dir)), ["state.sqlite3", "state.sqlite3-journal"])
        self.assertEqual((self.dir / "state.sqlite3-journal").read_bytes(), b"left over state.sqlite3-journal")

    def test_only_an_unreadable_file_counts_as_damaged(self):
        for error in (sqlite3.DatabaseError("file is not a database"),
                      sqlite3.DatabaseError("database disk image is malformed"),
                      sqlite3.DatabaseError("malformed database schema (jobs)")):
            self.assertTrue(state._is_damaged(error), error)
        for error in (sqlite3.OperationalError("database is locked"),
                      sqlite3.OperationalError("unable to open database file"),
                      sqlite3.OperationalError("attempt to write a readonly database"),
                      sqlite3.OperationalError("database or disk is full"),
                      sqlite3.DatabaseError("something else")):
            self.assertFalse(state._is_damaged(error), error)

    def test_a_damaged_table_page_is_found_when_opening(self):
        db = self.open()
        db.begin_job("fp", "video", "k")
        raw = bytearray(db.path.read_bytes())
        size = int.from_bytes(raw[16:18], "big")
        size = 65536 if size == 1 else size
        raw[size:2 * size] = b"\xff" * size  # page 2 holds the jobs table
        db.path.write_bytes(bytes(raw))
        with self.assertRaises(CuddlerError) as caught:
            self.open()
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertEqual(db.path.read_bytes(), bytes(raw))
        self.assertNotIn("state.sqlite3.damaged", os.listdir(self.dir))

    def test_a_locked_database_is_reported_and_left_alone(self):
        db = self.open(timeout=0.05)
        db.begin_job("fp", "video", "k")
        holder = sqlite3.connect(db.path, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")
        calls = (lambda: db.job("fp"), lambda: db.begin_job("other", "video", "k2"),
                 lambda: db.update_job("fp", expected_key="k", status="queued"), db.cleanup,
                 lambda: StateDB(self.dir, timeout=0.05))
        for call in calls:
            with self.assertRaises(CuddlerError) as caught:
                call()
            self.assertEqual((caught.exception.code, caught.exception.status), ("state_unavailable", 500))
            self.assertEqual(caught.exception.detail["reason"], "database is locked")
        holder.execute("ROLLBACK")
        self.assertEqual(db.job("fp")["idempotency_key"], "k")  # nothing was lost, nothing moved
        self.assertEqual(sorted(os.listdir(self.dir)), [INITIALIZED_MARKER, "state.sqlite3"])

    def test_a_path_that_cannot_be_opened_is_reported_not_replaced(self):
        (self.dir / "state.sqlite3").mkdir()
        with self.assertRaises(CuddlerError) as caught:
            self.open()
        self.assertEqual(caught.exception.code, "state_unavailable")
        self.assertTrue((self.dir / "state.sqlite3").is_dir())

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_the_database_and_its_journal_are_private(self):
        db = self.open()
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(db.path).st_mode), 0o600)
        connection = sqlite3.connect(db.path)
        self.addCleanup(connection.close)
        connection.execute("INSERT INTO jobs (fingerprint, kind, idempotency_key, status, created_at, updated_at) "
                           "VALUES ('a', 'video', 'k', 'submitting', 1, 1)")
        journal = db.path.with_name("state.sqlite3-journal")  # what a crash would leave behind
        self.assertEqual(stat.S_IMODE(os.stat(journal).st_mode), 0o600)
        connection.rollback()

    def test_threads_share_one_database(self):
        db = self.open()
        failures = []

        def work(number):
            try:
                for turn in range(25):
                    fingerprint = f"fp-{number}-{turn % 3}"
                    previous = db.job(fingerprint)
                    if previous:
                        db.update_job(fingerprint, expected_key=previous["idempotency_key"], status="failed")
                    db.begin_job(fingerprint, "video", f"k-{number}-{turn}",
                                 previous_key=previous["idempotency_key"] if previous else None)
                    db.update_job(fingerprint, expected_key=f"k-{number}-{turn}", status="queued", job_id=f"vid_{number}")
                    self.assertEqual(db.job(fingerprint)["job_id"], f"vid_{number}")
                    db.cleanup()
            except BaseException as error:  # collected here: an exception in a thread would otherwise vanish
                failures.append(error)

        threads = [threading.Thread(target=work, args=(number,)) for number in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])

    def test_cleanup_keeps_each_record_for_its_whole_retention(self):
        db = self.open()
        start = self.now
        db.begin_job("submitting", "video", "k1")
        db.begin_job("finished", "video", "k2")
        db.begin_upload("ready", "k3")
        db.finish_upload("ready", "upl_1")
        db.begin_upload("pending", "k4")
        db.remember_upload("pending", "upl_pending", credential_identity="binding")
        self.now = start + 2400
        db.update_job("finished", expected_key="k2", status="succeeded", job_id="vid_1")  # the result exists from here on
        for moment, expect in (
                (UNFINISHED_UPLOAD_TTL - 1, dict(pending=True, ready=True, submitting=True, finished=True)),
                (UNFINISHED_UPLOAD_TTL + 1, dict(pending=False, ready=True, submitting=True, finished=True)),
                (JOB_TTL + 60, dict(pending=False, ready=True, submitting=True, finished=True)),
                (JOB_TTL + JOB_GRACE - 1, dict(pending=False, ready=True, submitting=True, finished=True)),
                (JOB_TTL + JOB_GRACE + 60, dict(pending=False, ready=True, submitting=True, finished=True)),
                (2400 + JOB_TTL + JOB_GRACE + 1, dict(pending=False, ready=True, submitting=True, finished=False)),
                (UPLOAD_TTL - 1, dict(pending=False, ready=True, submitting=True, finished=False)),
                (UPLOAD_TTL + 1, dict(pending=False, ready=False, submitting=True, finished=False))):
            self.now = start + moment
            db.cleanup()
            kept = dict(pending=db.upload("pending") is not None, ready=db.upload("ready") is not None,
                        submitting=db.job("submitting") is not None, finished=db.job("finished") is not None)
            self.assertEqual(kept, expect, f"{moment} seconds after the start")

    def test_legacy_rows_are_migrated_without_inventing_a_credential_binding(self):
        path = self.dir / "state.sqlite3"
        with closing(sqlite3.connect(path)) as old, old:
            old.executescript("""
                CREATE TABLE jobs (fingerprint TEXT PRIMARY KEY, kind TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    job_id TEXT, status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, request TEXT);
                CREATE TABLE uploads (digest TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, upload_id TEXT,
                    status TEXT NOT NULL, created_at REAL NOT NULL);
                INSERT INTO jobs VALUES ('fp', 'video', 'old-key', NULL, 'submitting', 1, 1, '{}');
                INSERT INTO uploads VALUES ('digest', 'old-upload', NULL, 'creating', 1);
            """)
        db = self.open()
        self.assertIsNone(db.job("fp")["credential_identity"])
        self.assertIsNone(db.upload("digest")["credential_identity"])
        self.assertEqual(db.job("fp")["idempotency_key"], "old-key")

    def test_cleanup_keeps_unknown_and_running_paid_jobs_after_output_retention(self):
        db = self.open()
        for name, status in (("unknown", "submitting"), ("queued", "queued"), ("running", "running")):
            db.begin_job(name, "video", f"key-{name}", credential_identity="key-binding")
            db.update_job(name, expected_key=f"key-{name}", status=status)
        self.now += 90 * 86400
        db.cleanup()
        for name in ("unknown", "queued", "running"):
            self.assertIsNotNone(db.job(name))

    def test_cleanup_does_not_forget_unanswered_or_unbound_uploads(self):
        db = self.open()
        db.begin_upload("unknown", "unknown-key", credential_identity="binding")
        db.begin_upload("legacy", "legacy-key")
        self.now += 90 * 86400
        db.cleanup()
        self.assertIsNotNone(db.upload("unknown"))
        self.assertIsNotNone(db.upload("legacy"))

    def test_first_install_writes_a_private_initialization_marker_before_use(self):
        db = self.open()
        self.assertEqual(db.marker.read_bytes(), b"1\n")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(db.marker.stat().st_mode), 0o600)

    def test_a_missing_initialized_database_is_not_replaced(self):
        db = self.open()
        db.begin_job("fp", "video", "unanswered-key", credential_identity="binding")
        db.path.unlink()
        for call in (self.open, lambda: db.job("fp")):
            with self.assertRaises(CuddlerError) as caught:
                call()
            self.assertEqual(caught.exception.code, "recovery_required")
            self.assertFalse(db.path.exists())
        self.assertEqual(db.marker.read_bytes(), b"1\n")

    def test_credentials_without_a_database_cannot_be_assumed_to_be_a_first_install(self):
        (self.dir / "credentials.json").write_text('{"key":"opaque existing credential"}')
        with self.assertRaises(CuddlerError) as caught:
            self.open()
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertFalse((self.dir / "state.sqlite3").exists())
        self.assertFalse((self.dir / INITIALIZED_MARKER).exists())

    def test_a_marker_write_failure_prevents_initialization(self):
        with mock.patch.object(state, "write_private", side_effect=OSError("disk full")):
            with self.assertRaises(CuddlerError) as caught:
                self.open()
        self.assertEqual(caught.exception.code, "state_unavailable")
        self.assertFalse((self.dir / INITIALIZED_MARKER).exists())

    def test_zero_byte_or_empty_sqlite_cannot_replace_initialized_job_records(self):
        db = self.open()
        db.begin_job("fp", "video", "unanswered-key", credential_identity="binding")
        for empty_kind in ("zero-bytes", "empty-sqlite"):
            with self.subTest(empty_kind=empty_kind):
                db.path.write_bytes(b"")
                if empty_kind == "empty-sqlite":
                    with closing(sqlite3.connect(db.path)) as empty, empty:
                        empty.execute("PRAGMA user_version=1")  # valid SQLite with no job/upload tables
                before = db.path.read_bytes()
                for call in (self.open, lambda: db.job("fp"), lambda: db.begin_job("fp", "video", "new-key")):
                    with self.assertRaises(CuddlerError) as caught:
                        call()
                    self.assertEqual(caught.exception.code, "recovery_required")
                    self.assertEqual(db.path.read_bytes(), before)
                self.assertEqual(db.marker.read_bytes(), b"1\n")

    def test_an_existing_database_with_missing_legacy_columns_is_not_reinitialized(self):
        path = self.dir / "state.sqlite3"
        with closing(sqlite3.connect(path)) as incomplete, incomplete:
            incomplete.executescript("CREATE TABLE jobs (fingerprint TEXT PRIMARY KEY); "
                                     "CREATE TABLE uploads (digest TEXT PRIMARY KEY);")
        before = path.read_bytes()
        with self.assertRaises(CuddlerError) as caught:
            self.open()
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.dir / INITIALIZED_MARKER).exists())


if __name__ == "__main__":
    unittest.main()

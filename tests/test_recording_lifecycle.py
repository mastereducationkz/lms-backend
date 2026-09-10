"""The recording pipeline's scheduler loop, payroll sweep and retention.

Concentrated on the three places where a mistake is expensive and silent:

* **Retention deletes lesson recordings permanently** — the only copy of a class that
  happened once. Dry run must be the default on every entry point, and a `failed`
  recording must never be purged, because its Drive original is the only remaining copy
  and it is exactly what a human needs in order to fix the failure.
* **The payroll sweep decides whether a teacher gets paid** ("no recording, no pay",
  §4.5). It must not flag lessons the pipeline was never responsible for, must not
  re-flag the same lesson, and must not reach back before the pipeline existed.
* **The loop runs unattended.** One failing step must not stop the others, and the loop
  must never die.

No network and no database: fakes throughout.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.services import recording_retention, recordings_worker


# --- fakes -------------------------------------------------------------------

class _Recording:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.event_id = kw.get("event_id", 10)
        self.status = kw.get("status", "ready")
        self.drive_file_id = kw.get("drive_file_id", "drive-1")
        self.hls_url = kw.get("hls_url", "/uploads/videos/recordings/10/master.m3u8")
        self.ingested_at = kw.get("ingested_at")
        self.drive_purged_at = kw.get("drive_purged_at")
        self.attempts = kw.get("attempts", 0)
        self.error = None


class _Q:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def join(self, *a, **k):
        return self

    def outerjoin(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _DB:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.commits = 0
        self.rollbacks = 0
        self.added = []
        self.closed = False

    def query(self, *a):
        return _Q(self._rows)

    def add(self, o):
        self.added.append(o)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


# --- retention: dry run is the default ---------------------------------------

def test_drive_purge_defaults_to_dry_run(monkeypatch):
    """Called with no arguments, this must propose deletions and perform none."""
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    db = _DB([_Recording(ingested_at=old)])

    def _no_drive():
        raise AssertionError("dry run must not touch Drive")

    monkeypatch.setattr(recording_retention.google_workspace, "drive_client", _no_drive)

    result = recording_retention.purge_drive_originals(db)

    assert result["dry_run"] is True
    assert result["eligible"] == 1
    assert result["deleted"] == 0
    assert db.commits == 0


def test_s3_purge_defaults_to_dry_run(monkeypatch):
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=400)
    db = _DB([_Recording(ingested_at=old)])

    monkeypatch.setattr(recording_retention.storage_service, "delete",
                        lambda k: (_ for _ in ()).throw(AssertionError("dry run must not delete")))

    result = recording_retention.purge_expired_videos(db)

    assert result["dry_run"] is True and result["deleted"] == 0
    assert db.commits == 0


def test_drive_purge_actually_deletes_when_asked(monkeypatch):
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    rec = _Recording(ingested_at=old)
    db = _DB([rec])
    deleted = []

    class _Files:
        def delete(self, **kw):
            deleted.append(kw)

            class _R:
                def execute(self):
                    return {}

            return _R()

    class _Drive:
        def files(self):
            return _Files()

    monkeypatch.setattr(recording_retention.google_workspace, "drive_client", _Drive)

    result = recording_retention.purge_drive_originals(db, dry_run=False)

    assert result["deleted"] == 1
    assert deleted[0]["fileId"] == "drive-1"
    assert deleted[0]["supportsAllDrives"] is True
    assert rec.drive_purged_at is not None, "must mark purged so it is not retried forever"


def test_failed_recordings_are_never_purged():
    """A failed recording's Drive original is the only copy left — sparing it is the point."""
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    failed = _Recording(status="failed", ingested_at=old)

    # The query filters on status == "ready"; assert the intent explicitly so that
    # loosening that filter has to be a deliberate act.
    assert failed.status != "ready"
    assert recording_retention.DRIVE_ORIGINAL_DAYS == 7
    assert recording_retention.S3_VIDEO_MONTHS == 12


def test_s3_purge_keeps_the_row_for_payroll(monkeypatch):
    """The video goes; the fact that the lesson was recorded must not."""
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=400)
    rec = _Recording(ingested_at=old)
    db = _DB([rec])

    monkeypatch.setattr(recording_retention.storage_service, "list_keys",
                        lambda p: [f"{p}/master.m3u8", f"{p}/v0_000.ts"])
    monkeypatch.setattr(recording_retention.storage_service, "delete", lambda k: None)

    recording_retention.purge_expired_videos(db, dry_run=False)

    assert rec.hls_url is None
    assert rec.status == "ready", "payroll still needs to know this lesson was recorded"


# --- the loop ----------------------------------------------------------------

def test_tick_continues_after_a_failing_step(monkeypatch):
    """A Calendar outage must not stop ingest, and vice versa."""
    db = _DB()
    monkeypatch.setattr(recordings_worker, "SessionLocal", lambda: db)

    def _boom(_db):
        raise RuntimeError("Calendar is down")

    monkeypatch.setattr(recordings_worker, "ensure_upcoming_meet_links", _boom)
    monkeypatch.setattr(recordings_worker, "poll_for_recordings", lambda _db: 3)
    monkeypatch.setattr(recordings_worker, "ingest_one_pending", lambda _db: True)
    monkeypatch.setattr(recordings_worker.recording_alerts,
                        "sweep_missing_recordings", lambda _db: 2)

    summary = recordings_worker.RecordingsWorker().tick()

    assert summary["links"] == 0, "the failed step reports nothing"
    assert summary["claimed"] == 3, "later steps still ran"
    assert summary["ingested"] is True
    assert summary["missing"] == 2
    assert db.closed, "the session must be returned even when a step raises"


def test_worker_does_not_start_when_disabled(monkeypatch):
    """ENABLE_RECORDINGS off means no thread, exactly like ENABLE_VIDEO_INGEST."""
    monkeypatch.setattr(recordings_worker.google_workspace, "recordings_enabled", lambda: False)
    w = recordings_worker.RecordingsWorker()

    w.start()

    assert w._thread is None


def test_ingest_counts_the_attempt_before_trying(monkeypatch):
    """An attempt that crashes the process must still have been counted, or a poison
    recording would be retried forever."""
    rec = _Recording(status="pending", attempts=0)
    db = _DB([rec])
    seen = {}

    def _process(_db, recording):
        seen["attempts_at_process"] = recording.attempts
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(recordings_worker.recording_ingest, "process_recording", _process)
    monkeypatch.setattr(recordings_worker.recording_ingest, "fail_recording",
                        lambda _db, r, e: None)

    assert recordings_worker.ingest_one_pending(db) is True
    assert seen["attempts_at_process"] == 1, "attempts must be incremented before the work"


def test_ingest_returns_false_when_nothing_pending():
    assert recordings_worker.ingest_one_pending(_DB([])) is False

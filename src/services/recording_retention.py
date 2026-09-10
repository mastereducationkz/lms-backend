"""Delete recordings that have outlived their retention window (spec §4.6).

| Content | Kept for |
|---|---|
| Drive original (robot's Drive) | 7 days after successful ingest |
| Lesson video (S3 HLS) | 12 months |
| Transcripts | indefinitely — ~50 KB against ~700 MB, and most of the durable value |

Drive is a landing zone, not storage: 10 TB/year of untouched originals would cost more
than the entire Workspace licence bill.

**Everything here defaults to dry run.** These functions permanently delete lesson
recordings — the only copy of a class that happened once. `dry_run=True` is the default on
every entry point, and the scheduler does not call any of them. Purging is a deliberate,
supervised act until someone has watched a dry run and agreed with what it proposes.
"""
import logging
from datetime import datetime, timedelta, timezone

from src.schemas.models import LessonRecording
from src.services import google_workspace, storage_service

logger = logging.getLogger(__name__)

DRIVE_ORIGINAL_DAYS = 7
S3_VIDEO_MONTHS = 12


def write_tombstone(recording, event, reason: str) -> str:
    """Leave a breadcrumb in the Shared Drive when a video is deleted.

    Deleting a lesson should never make it *untraceable*. A ~1 KB text file costs nothing
    against the ~700 MB it replaces, and it means someone browsing «Уроки — записи» a year
    later still finds a row for every lesson that was ever recorded, saying what happened
    to it and where the pieces went — instead of a silent gap they cannot distinguish from
    "this lesson was never recorded".

    The database row already records all of this, but the Shared Drive is where a human
    looks, and a database nobody queries is not traceability.
    """
    from src.services.recording_ingest import storage_prefix

    lines = [
        f"Lesson {event.id}: {event.title}",
        f"Scheduled     : {event.start_datetime}",
        f"Teacher id    : {event.teacher_id}",
        "",
        f"Video removed : {reason}",
        f"Removed at    : {datetime.now(timezone.utc).replace(tzinfo=None)} UTC",
        "",
        f"LMS record    : lesson_recordings.id={recording.id} (status={recording.status})",
        f"Watch in LMS  : /events/{event.id}/recording",
        f"S3 prefix     : {storage_prefix(event.id)}",
        f"HLS path      : {recording.hls_url or '(purged)'}",
        f"Drive original: {recording.drive_file_id or '(none)'}",
        f"Shared Drive  : {recording.shared_drive_file_id or '(none)'}",
        f"Conference    : {recording.conference_record or '(unknown)'}",
    ]
    body = "\n".join(lines).encode("utf-8")

    from googleapiclient.http import MediaInMemoryUpload

    drive = google_workspace.drive_client()
    created = drive.files().create(
        body={
            "name": f"lesson-{event.id}-{event.start_datetime:%Y%m%d}-REMOVED.txt",
            "parents": [google_workspace.RECORDINGS_SHARED_DRIVE_ID],
            "mimeType": "text/plain",
        },
        media_body=MediaInMemoryUpload(body, mimetype="text/plain"),
        supportsAllDrives=True,
        fields="id",
    ).execute()
    return created["id"]


def purge_drive_originals(db, dry_run: bool = True, limit: int = 100) -> dict:
    """Delete the robot's copy of recordings ingested more than 7 days ago.

    Two conditions, and both are about never destroying the last copy:

    * only ``status='ready'`` — a ``failed`` recording's Drive original is the *only*
      remaining copy of that lesson, and is exactly what a human needs to fix the failure;
    * only when ``shared_drive_file_id`` is set — that is the Shared Drive archive. If the
      archive step failed, the original is still the only copy outside S3, so it stays.
      A failure there costs storage; purging anyway could cost the lesson.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=DRIVE_ORIGINAL_DAYS)
    due = (
        db.query(LessonRecording)
        .filter(
            LessonRecording.status == "ready",
            LessonRecording.ingested_at.isnot(None),
            LessonRecording.ingested_at < cutoff,
            LessonRecording.drive_file_id.isnot(None),
            LessonRecording.drive_purged_at.is_(None),
            # No archive, no purge. See the docstring.
            LessonRecording.shared_drive_file_id.isnot(None),
        )
        .limit(limit)
        .all()
    )

    result = {"eligible": len(due), "deleted": 0, "errors": 0, "dry_run": dry_run}
    if dry_run:
        for r in due:
            logger.info("[dry-run] would delete Drive original %s (lesson %s, ingested %s)",
                        r.drive_file_id, r.event_id, r.ingested_at)
        return result

    drive = google_workspace.drive_client()
    for r in due:
        try:
            drive.files().delete(fileId=r.drive_file_id, supportsAllDrives=True).execute()
            r.drive_purged_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
            result["deleted"] += 1
        except Exception as e:
            db.rollback()
            result["errors"] += 1
            logger.error("failed to delete Drive original %s: %s", r.drive_file_id, e)
    return result


def purge_expired_videos(db, dry_run: bool = True, limit: int = 100) -> dict:
    """Delete lesson HLS from S3 after 12 months, and clear the row's hls_url.

    The recording row survives with ``status='ready'`` and ``hls_url=None``: payroll needs
    to keep knowing a lesson *was* recorded long after the video itself is gone.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30 * S3_VIDEO_MONTHS)
    due = (
        db.query(LessonRecording)
        .filter(
            LessonRecording.status == "ready",
            LessonRecording.hls_url.isnot(None),
            LessonRecording.ingested_at.isnot(None),
            LessonRecording.ingested_at < cutoff,
        )
        .limit(limit)
        .all()
    )

    result = {"eligible": len(due), "deleted": 0, "errors": 0, "dry_run": dry_run}
    if dry_run:
        for r in due:
            logger.info("[dry-run] would delete S3 HLS for lesson %s (%s)", r.event_id, r.hls_url)
        return result

    # Local import: recording_ingest pulls in the Google client tree, and retention is
    # imported by anything that wants the constants.
    from src.services.recording_ingest import storage_prefix

    for r in due:
        try:
            prefix = storage_prefix(r.event_id)
            # Breadcrumb first, delete second. If the tombstone fails we would rather
            # keep a video nobody can find than delete one nobody can trace.
            try:
                write_tombstone(r, r.event, f"S3 retention: {S3_VIDEO_MONTHS} months elapsed")
            except Exception as e:
                logger.error("lesson %s: tombstone failed, skipping purge: %s", r.event_id, e)
                result["errors"] += 1
                continue
            for key in storage_service.list_keys(prefix):
                storage_service.delete(key)
            r.hls_url = None
            db.commit()
            result["deleted"] += 1
        except Exception as e:
            db.rollback()
            result["errors"] += 1
            logger.error("failed to purge S3 HLS for lesson %s: %s", r.event_id, e)
    return result

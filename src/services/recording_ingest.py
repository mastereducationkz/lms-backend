"""Turn a claimed Meet recording into a streamable HLS video on S3.

Deliberately thin. The hard part — transcoding to an adaptive ladder, uploading the tree,
serving it behind a signed token — already exists in ``video_ingest`` and was proven
end-to-end against production S3 on 2026-09-10 (spec §13). This module only supplies a
different *source*: a Google Drive file instead of a YouTube URL. ``_transcode_hls`` and
``_upload_tree`` are reused untouched; duplicating them would mean two ladders to keep in
step and two places for a bug to hide.

The YouTube CDN outage in §12 does not apply here. That failure is specific to
``googlevideo.com`` media nodes; Drive downloads go to ``www.googleapis.com``, which is
reachable from this host (verified). ``MediaCdnUnreachable`` deliberately plays no part in
this path.
"""
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.services import google_workspace, meet_recordings, storage_service, video_ingest

logger = logging.getLogger(__name__)

# A lesson recording is far larger than a course video and the worker is single-threaded,
# so a stuck download must not wedge the queue behind it.
DOWNLOAD_TIMEOUT = 3600
MAX_ATTEMPTS = 3


def _download_drive_file(file_id: str, workdir: Path) -> Path:
    """Stream a Drive file to disk.

    Chunked rather than read-into-memory: lesson recordings run to hundreds of megabytes
    and the container is sharing 4 cores and limited RAM with the rest of the stack.
    """
    from googleapiclient.http import MediaIoBaseDownload

    dest = workdir / "source.mp4"
    drive = google_workspace.drive_client()
    request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)

    with open(dest, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _status, done = downloader.next_chunk()

    if dest.stat().st_size == 0:
        raise RuntimeError(f"drive file {file_id} downloaded as 0 bytes")
    return dest


def storage_prefix(event_id: int) -> str:
    """Where this lesson's HLS lives.

    Under ``videos/`` because that is the prefix ``storage_service.is_video`` recognises
    and the only one the signed-token route will serve — putting recordings anywhere else
    would make them silently unplayable.
    """
    return f"videos/recordings/{event_id}"


def process_recording(db, recording) -> None:
    """Drive file → HLS on S3 → ``status='ready'``. Raises on failure.

    The recording row is the unit of work; the caller owns retry and failure accounting.
    """
    if not recording.drive_file_id:
        raise RuntimeError(f"recording {recording.id} has no drive_file_id")

    workdir = Path(tempfile.mkdtemp(prefix=f"rec_{recording.event_id}_"))
    try:
        source = _download_drive_file(recording.drive_file_id, workdir)
        logger.info("recording %s: downloaded %s bytes", recording.id, source.stat().st_size)

        hls_dir = workdir / "hls"
        hls_dir.mkdir()
        video_ingest._transcode_hls(source, hls_dir)

        prefix = storage_prefix(recording.event_id)
        video_ingest._upload_tree(hls_dir, prefix)

        recording.hls_url = storage_service.stored_path(f"{prefix}/master.m3u8")
        recording.status = "ready"
        recording.error = None
        recording.ingested_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("recording %s: ready at %s", recording.id, recording.hls_url)

        # Archive into the Shared Drive (spec §4.3 step 5). Deliberately after the row is
        # committed as ready: the lesson is already watchable from S3, so a Drive hiccup
        # must not fail the ingest or make a student wait. It does leave
        # shared_drive_file_id NULL, which retention treats as "not safe to purge the
        # original yet" — so a failure here costs storage, never the recording.
        try:
            recording.shared_drive_file_id = meet_recordings.copy_to_shared_drive(
                recording.drive_file_id, recording.event
            )
            db.commit()
            logger.info("recording %s: archived to Shared Drive as %s",
                        recording.id, recording.shared_drive_file_id)
        except Exception as e:
            db.rollback()
            logger.error("recording %s: Shared Drive archive failed (video is still "
                         "playable; Drive original will not be purged): %s", recording.id, e)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def fail_recording(db, recording, exc: Exception) -> None:
    """Record a failure, and give up only after MAX_ATTEMPTS.

    A failed recording is never silently dropped: ``status='failed'`` is what the payroll
    view reads, so a lesson whose recording could not be ingested still surfaces to a
    human rather than looking like it was never recorded at all.
    """
    recording.error = str(exc)[:1900]
    recording.status = "failed" if recording.attempts >= MAX_ATTEMPTS else "pending"
    db.commit()
    logger.warning("recording %s failed (attempt %s): %s",
                   recording.id, recording.attempts, str(exc)[:400])

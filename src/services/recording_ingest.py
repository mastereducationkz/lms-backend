"""Turn a claimed Meet recording into a streamable HLS video on S3.

Deliberately thin. Uploading the tree and serving it behind a signed token already exist in
``video_ingest`` and were proven end-to-end against production S3 on 2026-09-10 (spec §13).
This module supplies a different *source* — a Google Drive file instead of a YouTube URL —
and a cheaper way to make it streamable: Meet's own H.264/AAC is repackaged into HLS rather
than re-encoded (:func:`package_hls`). ``_transcode_hls``'s ladder stays as the fallback for
a file that cannot be repackaged, reused untouched so there is still one ladder.

The YouTube CDN outage in §12 does not apply here. That failure is specific to
``googlevideo.com`` media nodes; Drive downloads go to ``www.googleapis.com``, which is
reachable from this host (verified). ``MediaCdnUnreachable`` deliberately plays no part in
this path.
"""
import json
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.exc import OperationalError

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


# A repackaged video can only be cut where Meet put a keyframe. If those are further apart
# than this, seeking would stall on huge segments, so the lesson is re-encoded instead.
MAX_SEGMENT_SECONDS = 15


def _probe_streams(src: Path) -> dict:
    """The first video and audio stream of ``src``; each value None if absent or unreadable."""
    info = {"video": None, "pix_fmt": None, "audio": None}
    try:
        out = video_ingest._run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt",
             "-of", "json", str(src)],
            timeout=120, capture=True,
        )
        streams = json.loads(out).get("streams", [])
    except Exception as e:
        logger.warning("ffprobe could not read %s: %s", src.name, e)
        return info
    for stream in streams:
        if stream.get("codec_type") == "video" and info["video"] is None:
            info["video"], info["pix_fmt"] = stream.get("codec_name"), stream.get("pix_fmt")
        elif stream.get("codec_type") == "audio" and info["audio"] is None:
            info["audio"] = stream.get("codec_name")
    return info


def _browser_playable(info: dict) -> bool:
    """H.264 in 8-bit 4:2:0 with AAC (or no) sound: what every browser decodes.

    The pixel format is not a formality. H.264 can also be 4:4:4 or 10-bit, which browsers
    refuse — a repackage of such a file would "succeed" and then never play.
    """
    return (
        info["video"] == "h264"
        and info["pix_fmt"] in ("yuv420p", "yuvj420p")
        and info["audio"] in ("aac", None)
    )


def _target_duration(playlist: Path) -> float:
    for line in playlist.read_text().splitlines():
        if line.startswith("#EXT-X-TARGETDURATION:"):
            return float(line.split(":", 1)[1])
    raise RuntimeError(f"{playlist.name} has no target duration")


def _repackage_hls(src: Path, out: Path, *, has_audio: bool) -> None:
    """Copy Meet's own H.264/AAC into HLS segments — no re-encoding, same layout as the ladder
    (``master.m3u8`` + ``v0.m3u8`` + ``v0_NNN.ts``), so the player and token route see no difference."""
    cmd = ["ffmpeg", "-y", "-i", str(src), "-map", "0:v:0"]
    if has_audio:
        cmd += ["-map", "0:a:0"]
    cmd += ["-c", "copy", "-bsf:v", "h264_mp4toannexb",
            "-f", "hls", "-hls_time", "6", "-hls_playlist_type", "vod",
            "-hls_flags", "independent_segments",
            "-hls_segment_filename", str(out / "v%v_%03d.ts"),
            "-master_pl_name", "master.m3u8",
            "-var_stream_map", "v:0,a:0" if has_audio else "v:0",
            str(out / "v%v.m3u8")]
    video_ingest._run(cmd, timeout=1800)
    if not (out / "master.m3u8").exists():
        raise RuntimeError("ffmpeg did not produce master.m3u8")


def package_hls(src: Path, out: Path) -> str:
    """Make the recording streamable. Returns ``"repackaged"`` or ``"re-encoded"``.

    Meet already records H.264 video with AAC sound — what every browser plays. Re-encoding
    it into three quality levels cost ~23 minutes of all four shared cores per lesson-hour
    (measured on production, 2026-09-10) and ~2.5 GB of storage, which at 45–66 lessons a day
    would never catch up. Copying the streams into HLS segments takes about a minute and
    stores roughly the original size.

    Anything else — another codec or pixel format, keyframes too far apart, a repackage that
    fails — falls back to the full re-encode, so an unusual file costs time, never the lesson.
    """
    info = _probe_streams(src)
    if _browser_playable(info):
        try:
            _repackage_hls(src, out, has_audio=info["audio"] is not None)
            longest = _target_duration(out / "v0.m3u8")
            if longest <= MAX_SEGMENT_SECONDS:
                return "repackaged"
            logger.warning("%s: keyframes up to %ss apart, re-encoding instead", src.name, longest)
        except Exception as e:
            logger.warning("%s: repackage failed, re-encoding instead: %s", src.name, e)
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir()
    else:
        logger.info("%s: video=%s/%s audio=%s, re-encoding", src.name,
                    info["video"], info["pix_fmt"], info["audio"])
    video_ingest._transcode_hls(src, out)
    return "re-encoded"


def _save(db, recording, **fields) -> None:
    """Write ``fields`` to the row, starting over once if the connection died meanwhile.

    The engine uses NullPool, so after a rollback the retry runs on a brand-new connection.
    """
    for attempt in (1, 2):
        for name, value in fields.items():
            setattr(recording, name, value)
        try:
            db.commit()
            return
        except OperationalError as e:
            db.rollback()
            if attempt == 2:
                raise
            logger.warning("recording %s: database connection dropped, saving again: %s",
                           getattr(recording, "id", "?"), str(e).splitlines()[0][:200])


def process_recording(db, recording) -> None:
    """Drive file → HLS on S3 → ``status='ready'``. Raises on failure.

    The recording row is the unit of work; the caller owns retry and failure accounting.
    """
    rec_id, event_id, drive_file_id = recording.id, recording.event_id, recording.drive_file_id
    if not drive_file_id:
        raise RuntimeError(f"recording {rec_id} has no drive_file_id")

    # Postgres and pgbouncer both kill a transaction left idle for 60 s, and reading the row
    # above opened one. What follows is minutes of download and upload with nothing to say to
    # the database: on 2026-09-10 the first live lesson downloaded, repackaged and uploaded
    # perfectly, then lost its "ready" to a connection closed four minutes earlier. End the
    # read here; every save below starts its own.
    db.commit()

    workdir = Path(tempfile.mkdtemp(prefix=f"rec_{event_id}_"))
    try:
        source = _download_drive_file(drive_file_id, workdir)
        logger.info("recording %s: downloaded %s bytes", rec_id, source.stat().st_size)

        hls_dir = workdir / "hls"
        hls_dir.mkdir()
        how = package_hls(source, hls_dir)
        logger.info("recording %s: %s for streaming", rec_id, how)

        prefix = storage_prefix(event_id)
        video_ingest._upload_tree(hls_dir, prefix)

        hls_url = storage_service.stored_path(f"{prefix}/master.m3u8")
        _save(db, recording, hls_url=hls_url, status="ready", error=None,
              ingested_at=datetime.now(timezone.utc))
        logger.info("recording %s: ready at %s", rec_id, hls_url)

        # Archive into the Shared Drive (spec §4.3 step 5). Deliberately after the row is
        # committed as ready: the lesson is already watchable from S3, so a Drive hiccup
        # must not fail the ingest or make a student wait. It does leave
        # shared_drive_file_id NULL, which retention treats as "not safe to purge the
        # original yet" — so a failure here costs storage, never the recording.
        try:
            shared_id = meet_recordings.copy_to_shared_drive(drive_file_id, recording.event)
            _save(db, recording, shared_drive_file_id=shared_id)
            logger.info("recording %s: archived to Shared Drive as %s", rec_id, shared_id)
        except Exception as e:
            db.rollback()
            logger.error("recording %s: Shared Drive archive failed (video is still "
                         "playable; Drive original will not be purged): %s", rec_id, e)
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

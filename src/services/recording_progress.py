"""Where a lesson's recording is on its way to the Watch button — for the pages that wait on it (2026-09-15).

«Processing» was all a page could say between a lesson ending and its video playing, for up to hours
while the ingest line drained. This reads the whole way:

  lesson_running      the lesson is on; its recording comes after it ends
  waiting_for_google  over, and Google Meet has not handed the recording over yet
  queued              the LMS has the file; it waits its turn — ``position`` of ``queue_length``
  processing          being made watchable now: downloading → packaging → preview → uploading, with the
                      phase's percent, an overall percent and — once a rate has been measured — the time
                      left in the phase
  retrying            an earlier try failed or was cut off; it is back in the line
  failed / removed    (ready needs no progress)

Nothing here is stored. The recording row, the line's order (the worker's own query) and the worker's
report in ``recordings_status`` are read together; ingest runs one recording at a time, so one report
covers the pipeline.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from src.schemas.models import LessonRecording, UserInDB
from src.services import recording_alerts, recording_ingest, recordings_status
from src.utils.utc_json import utc_z

PHASES = ("downloading", "packaging", "preview", "uploading")
# Each phase's share of the whole, from a 60-minute lesson on production (recording 48, 2026-09-15:
# 614 MB down, repackaged in 3 s, preview and upload ~170 s together, the upload the size of the
# download). A re-encode makes packaging far longer; its own phase percent still says how far it is.
WEIGHTS = {"downloading": 0.35, "packaging": 0.05, "preview": 0.10, "uploading": 0.50}
# A time left is only worth saying once the phase has run long enough to have a rate.
ETA_AFTER_SECONDS = 5
ETA_AFTER_FRACTION = 0.02

# Who reads why a recording failed. Students see the stage, never the error.
STAFF_ROLES = frozenset({"admin", "head_curator", "head_teacher", "teacher", "curator"})


def is_staff(user) -> bool:
    return getattr(user, "role", None) in STAFF_ROLES


def ingest_line(db) -> list:
    """Pending recording ids in the order the worker takes them (``recordings_worker.ingest_one_pending``)."""
    return [rid for (rid,) in (
        db.query(LessonRecording.id)
        .filter(LessonRecording.status == "pending",
                LessonRecording.drive_file_id.isnot(None),
                LessonRecording.attempts < recording_ingest.MAX_ATTEMPTS)
        .order_by(LessonRecording.created_at)
    )]


class Context:
    """What every recording's progress is read against — fetched once per request, not per card."""

    def __init__(self, db, now: Optional[datetime] = None):
        self.now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        self.status = recordings_status.raw(db)
        self.sync = recordings_status.snapshot(db, self.now)
        report = self.status.get("ingest") or None
        updated = recordings_status._parse(report.get("updated_at")) if report else None
        # A report no newer than this belongs to a worker that stopped mid-recording: it is back in line.
        fresh = updated is not None and self.now - updated <= recordings_status.PROGRESS_STALE_AFTER
        self.report = report if fresh else None
        current = self.report.get("recording_id") if self.report else None
        self.line = [rid for rid in ingest_line(db) if rid != current]

    def position(self, recording_id: int) -> Optional[int]:
        return self.line.index(recording_id) + 1 if recording_id in self.line else None


def _first_line(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return text.strip().splitlines()[0][:200] if text.strip() else None


def _progress(ctx: Context, stage: str, **fields) -> dict:
    return {
        "stage": stage, "phase": None, "phase_percent": None, "percent": None, "eta_seconds": None,
        "position": None, "queue_length": None, "held_for_disk": bool(ctx.status.get("held_for_disk")),
        "attempts": 0, "max_attempts": recording_ingest.MAX_ATTEMPTS, "error": None,
        "lesson_ended_at": None, "missing_after": None, "claimed_at": None, "updated_at": None,
        "sync": ctx.sync, **fields,
    }


def _phase_fields(report: dict, now: datetime) -> dict:
    """The phase under way: its percent, the overall percent, and the time left once there is a rate."""
    phase = report.get("phase")
    done, total = report.get("done"), report.get("total")
    fraction = None
    if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total > 0:
        fraction = min(max(done / total, 0.0), 1.0)
    before = sum(WEIGHTS[p] for p in PHASES[:PHASES.index(phase)]) if phase in PHASES else 0.0
    overall = before + WEIGHTS.get(phase, 0.0) * (fraction or 0.0)

    eta = None
    started = recordings_status._parse(report.get("phase_started_at"))
    updated = recordings_status._parse(report.get("updated_at"))
    if fraction is not None and ETA_AFTER_FRACTION <= fraction < 1 and started and updated:
        elapsed = (updated - started).total_seconds()
        if elapsed >= ETA_AFTER_SECONDS:
            left = (1 - fraction) * elapsed / fraction - (now - updated).total_seconds()
            eta = max(0, round(left))
    return {
        "phase": phase,
        "phase_percent": round(fraction * 100) if fraction is not None else None,
        # Never 100 before the row says ready: the last save and the archive still follow the upload.
        "percent": min(99, round(overall * 100)),
        "eta_seconds": eta,
        "updated_at": report.get("updated_at"),
    }


def of_recording(ctx: Context, recording, event=None, *, staff: bool = False) -> Optional[dict]:
    """The progress of a recording the LMS has found — None once it is ready to watch."""
    if recording.status == "ready" and recording.hls_url:
        return None
    common = {
        "attempts": recording.attempts or 0,
        "claimed_at": utc_z(recording.created_at) if recording.created_at else None,
        "error": _first_line(recording.error) if staff else None,
        "lesson_ended_at": utc_z(event.end_datetime) if event is not None and event.end_datetime else None,
    }
    if recording.status == "ready":
        return _progress(ctx, "removed", **common)
    if recording.status != "pending":
        return _progress(ctx, "failed" if recording.status == "failed" else recording.status, **common)
    if ctx.report and ctx.report.get("recording_id") == recording.id:
        return _progress(ctx, "processing", **{**common, **_phase_fields(ctx.report, ctx.now)})
    if not recording.drive_file_id:
        return _progress(ctx, "waiting_for_google", **common)
    return _progress(ctx, "retrying" if recording.attempts else "queued", **common,
                     position=ctx.position(recording.id), queue_length=len(ctx.line))


def without_recording(db, ctx: Context, event, *, staff: bool = False) -> tuple:
    """``("waiting", progress)`` while a recording can still come for ``event``; ``("missing", None)`` once not.

    Only lessons this pipeline records — a Meet link and a teacher with a Workspace account, the rule
    the missing-recording sweep uses. Until that sweep's grace runs out, «no recording» is not yet true.
    """
    if event.event_type != "class" or not event.meeting_url or not event.teacher_id:
        return "missing", None
    if not db.query(UserInDB.workspace_email).filter(UserInDB.id == event.teacher_id).scalar():
        return "missing", None
    end = event.end_datetime or event.start_datetime + timedelta(hours=1)
    if ctx.now < event.start_datetime:
        return "missing", None
    if ctx.now < end:
        return "waiting", _progress(ctx, "lesson_running", lesson_ended_at=utc_z(end))
    missing_after = end + timedelta(hours=recording_alerts.GRACE_HOURS)
    if ctx.now < missing_after:
        return "waiting", _progress(ctx, "waiting_for_google", lesson_ended_at=utc_z(end),
                                    missing_after=utc_z(missing_after) if staff else None)
    return "missing", None

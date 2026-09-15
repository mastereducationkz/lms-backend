"""What the recordings worker is doing — for the pages that wait on it (2026-09-15).

Who joined a lesson, its recording and its talk time reach the LMS only when the worker's tick gets
to them, and Google Meet decides when a call is handed over. A page that said just «Loading» read
as a slow LMS. With this it says what is really going on: checking Google Meet now, which step, how
far along, when the last check finished and when the next one starts.

One row in ``app_settings``, written by the worker in short transactions of its own — never inside a
step's session, whose Google calls must not hold a transaction open (pgbouncer cuts it at 60 s).
Status is a courtesy: when it cannot be saved, the work carries on.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.schemas.models import AppSetting
from src.utils.utc_json import utc_z

logger = logging.getLogger(__name__)

KEY = "recordings_worker"

# A check running longer than this is shown as slow, so no page says "checking now" for an hour and nothing more.
SLOW_AFTER = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _save(**fields) -> None:
    from src.config import SessionLocal

    db = SessionLocal()
    try:
        row = db.get(AppSetting, KEY)
        if row is None:
            row = AppSetting(key=KEY, value={})
            db.add(row)
        row.value = {**(row.value if isinstance(row.value, dict) else {}), **fields}
        row.updated_at = _now()
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("recordings status not saved: %s", e)
    finally:
        db.close()


def tick_started(poll_seconds: int) -> None:
    _save(started_at=utc_z(_now()), step=None, progress=None, poll_seconds=poll_seconds)


def step_started(step: str) -> None:
    _save(step=step, progress=None)


def attendance_progress(done: int, total: int) -> None:
    """Calls whose people are saved so far, of the calls this check has to save."""
    _save(progress={"done": done, "total": total})


def step_finished(step: str) -> None:
    fields: dict = {"progress": None}
    if step == "attendance":
        fields["attendance_at"] = utc_z(_now())
    _save(**fields)


def tick_finished(seconds: float) -> None:
    _save(finished_at=utc_z(_now()), step=None, progress=None, last_seconds=round(seconds))


# ── the recording being made watchable right now (2026-09-15) ───────────────────────────
# The ingest runs one recording at a time, so one entry says it all: which recording, which phase,
# how far into it. Written at most every PROGRESS_EVERY — a lesson is ~77 download chunks and ~600
# HLS files — except when the phase changes or completes.
PROGRESS_EVERY = timedelta(seconds=2)
# A report this old belongs to a worker that stopped mid-recording (every phase reports far more often).
PROGRESS_STALE_AFTER = timedelta(minutes=3)

_last_report: dict = {}


def ingest_progress(recording_id: int, event_id: int, phase: str,
                    done: Optional[float] = None, total: Optional[float] = None) -> None:
    now = _now()
    key = (recording_id, phase)
    same = _last_report.get("key") == key
    finished = done is not None and total is not None and done >= total
    if same and not finished and now - _last_report["at"] < PROGRESS_EVERY:
        return
    phase_started = _last_report["phase_started"] if same else now
    _last_report.update(key=key, at=now, phase_started=phase_started)
    _save(ingest={"recording_id": recording_id, "event_id": event_id, "phase": phase,
                  "done": done, "total": total,
                  "phase_started_at": utc_z(phase_started), "updated_at": utc_z(now)})


def ingest_finished() -> None:
    _last_report.clear()
    _save(ingest=None)


def ingest_held_for_disk(held: bool) -> None:
    """The line is held because the server's disk is nearly full — worth saying, never a silent wait."""
    _save(held_for_disk=held)


def raw(db) -> dict:
    """The row as the worker wrote it, for readers that need more than ``snapshot``."""
    row = db.get(AppSetting, KEY)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def snapshot(db, now: Optional[datetime] = None) -> Optional[dict]:
    """The worker's status as a page shows it, or None before it has ever run."""
    row = db.get(AppSetting, KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else None
    if not value:
        return None
    now = now or _now()
    started, finished = _parse(value.get("started_at")), _parse(value.get("finished_at"))
    # A restart mid-check leaves a start with no finish after it: still "running", and slow soon enough to say so.
    running = started is not None and (finished is None or finished < started)
    poll = value.get("poll_seconds")
    next_at = finished + timedelta(seconds=poll) if not running and finished and poll else None
    return {
        "running": running,
        "step": value.get("step") if running else None,
        "progress": value.get("progress") if running else None,
        "started_at": utc_z(started) if started else None,
        "finished_at": utc_z(finished) if finished else None,
        "attendance_at": utc_z(_parse(value.get("attendance_at"))) if value.get("attendance_at") else None,
        "next_at": utc_z(next_at) if next_at else None,
        "slow": bool(running and now - started > SLOW_AFTER),
    }

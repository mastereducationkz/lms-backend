"""Work the scheduler has started and not finished — so a deploy never costs a recording one of its tries (2026-09-15).

Ingest and transcription count an attempt *before* the work, so a recording that crashes the process (an
ffmpeg that eats the memory) cannot be retried for ever. The price was every deploy: ``docker compose up -d``
stopped the scheduler mid-recording, the cut-off try kept its count, and three unlucky deploys would leave a
recording pending with no attempts left — a row the ingest line skips and nothing ever retries.

So the scheduler now says goodbye properly. On SIGTERM (a deploy, a ``docker stop``) everything registered here
gets its attempt back before the process exits. An ungraceful end — SIGKILL, out of memory, the host going
down — keeps the attempt: exactly the protection the early count exists for. And work that has used up its
attempts without an answer is written off as failed, with why, so a person sees it and can retry it instead
of it waiting in line for ever.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# Said on a recording or transcript that ran out of attempts without ever failing on its own.
EXHAUSTED_REASON = ("Stopped before finishing on every attempt — the server was restarted or ran out of memory "
                    "while it was being worked on. Try again.")

_lock = threading.Lock()
_rows: dict = {}  # kind -> ids being worked on right now


def _model(kind: str):
    from src.schemas.models import LessonRecording, LessonTranscript

    return {"recording": LessonRecording, "transcript": LessonTranscript}[kind]


def begin(kind: str, row_id: int) -> None:
    with _lock:
        _rows.setdefault(kind, set()).add(row_id)


def end(kind: str, row_id: int) -> None:
    with _lock:
        _rows.get(kind, set()).discard(row_id)


def in_flight(kind: str) -> set:
    with _lock:
        return set(_rows.get(kind, ()))


def give_back_attempts(session_factory=None) -> dict:
    """Return the attempt of everything cut off by a graceful stop. Returns ``{kind: rows given back}``."""
    with _lock:
        snapshot = {kind: sorted(ids) for kind, ids in _rows.items() if ids}
        _rows.clear()
    if not snapshot:
        return {}
    if session_factory is None:
        from src.config import SessionLocal as session_factory

    given = {}
    db = session_factory()
    try:
        for kind, ids in snapshot.items():
            model = _model(kind)
            given[kind] = (db.query(model)
                           .filter(model.id.in_(ids), model.status == "pending", model.attempts > 0)
                           .update({model.attempts: model.attempts - 1}, synchronize_session=False))
        db.commit()
        logger.info("stopping: gave back the attempt of work cut off mid-way: %s (ids %s)", given, snapshot)
    except Exception as e:
        db.rollback()
        logger.error("stopping: could not give back the attempts of %s: %s", snapshot, e)
    finally:
        db.close()
    return given


def write_off_exhausted(db, kind: str, max_attempts: int) -> int:
    """Pending work with no attempts left and nobody on it: failed, with the reason — never silently stuck."""
    model = _model(kind)
    query = db.query(model).filter(model.status == "pending", model.attempts >= max_attempts)
    busy = in_flight(kind)
    if busy:
        query = query.filter(~model.id.in_(busy))
    rows = query.all()
    for row in rows:
        row.status = "failed"
        row.error = row.error or EXHAUSTED_REASON
    if rows:
        db.commit()
        logger.warning("%s rows %s used every attempt without an answer: marked failed", kind, [r.id for r in rows])
    return len(rows)

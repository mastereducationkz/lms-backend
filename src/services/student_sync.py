"""Cross-platform student-data sync — LMS publisher side (see SSO_SYNC_DESIGN.md).

LMS is the system-of-record for group membership (the rows physically live in the LMS DB,
even for CRM edits). This module (a) enqueues change snapshots into ``student_sync_outbox``
in the same transaction as the LMS write, and (b) drains the outbox by HTTP-pushing to the
target platforms (SAT/NUET now, IELTS later) idempotently.

Everything is gated by ``SYNC_ENABLED`` (off by default) so it is a strict no-op until enabled:
enqueue does nothing, and the drainer is not started. Transport is HTTP (the live X-API-Key
path LMS already uses for scores) — no message broker required.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _outbox_model():
    # Lazy import to avoid an import cycle (courses.models <-> services at load time).
    from src.courses.models import StudentSyncOutbox

    return StudentSyncOutbox

_TRUTHY = {"1", "true", "yes", "on"}
_MAX_ATTEMPTS = 8


def sync_enabled() -> bool:
    return os.getenv("SYNC_ENABLED", "").strip().lower() in _TRUTHY


def _sat_base_url() -> str:
    # Reuse the SAT/NUET api/lms base LMS already talks to for scores.
    return os.getenv("SAT_SYNC_URL", "https://api.mastereducation.kz/api/lms").rstrip("/")


def _sat_api_key() -> str:
    return os.getenv("MASTEREDU_API_KEY", "")


# --- payload builders --------------------------------------------------------

def group_snapshot(group) -> dict:
    """A full desired-state snapshot of an LMS group (idempotent, self-healing)."""
    return {
        "lms_group_id": group.id,
        "name": getattr(group, "name", None),
        "program_type": getattr(group, "program_type", None),
        "is_active": bool(getattr(group, "is_active", True)),
    }


# --- enqueue (runs inside the caller's transaction) --------------------------

def enqueue_group_upserted(db: Session, group):
    """Enqueue a ``group.upserted`` snapshot. No-op (returns None) when sync is off.

    Adds the row to the session but does NOT commit — the caller's transaction owns it, so
    the event is durable iff the group write commits (atomic). Never raises into the caller.
    """
    if not sync_enabled():
        return None
    try:
        event_id = str(uuid.uuid4())
        payload = {
            "event_id": event_id,
            "event_type": "group.upserted",
            "source": "lms",
            "group": group_snapshot(group),
        }
        row = _outbox_model()(event_id=event_id, event_type="group.upserted", payload=payload)
        db.add(row)
        return row
    except Exception as exc:  # never break the group write because of sync bookkeeping
        logger.warning("enqueue_group_upserted failed (non-fatal): %s", exc)
        return None


# --- drainer (runs in the scheduler container) -------------------------------

_ROUTES = {
    # event_type -> (relative path on the SAT api/lms base)
    "group.upserted": "/groups",
}


def _deliver(row, *, timeout: float = 15.0) -> tuple[bool, str]:
    """POST one outbox row to the target. Returns (ok, detail)."""
    path = _ROUTES.get(row.event_type)
    if path is None:
        return False, f"no route for event_type {row.event_type}"
    url = f"{_sat_base_url()}{path}"
    try:
        resp = httpx.post(
            url,
            json=row.payload,
            headers={"X-API-Key": _sat_api_key(), "Content-Type": "application/json"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return False, f"transport error: {exc}"
    if resp.status_code in (200, 201, 204):
        return True, "ok"
    # 503 = target sync feature disabled; retry later. 4xx (except 503) = permanent-ish.
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def run_drain_loop(stop_event=None, poll_seconds: int = 15) -> None:
    """Background loop for the scheduler container: drain the outbox every ``poll_seconds``.

    No-op unless ``SYNC_ENABLED``. Opens its own short-lived session per pass and never
    raises out of the loop, so a transient DB/target failure can't kill the scheduler.
    """
    import time as _time

    from src.config import SessionLocal

    logger.info("student-sync drainer loop started (poll=%ss, enabled=%s)", poll_seconds, sync_enabled())
    while stop_event is None or not stop_event.is_set():
        try:
            if sync_enabled():
                db = SessionLocal()
                try:
                    result = drain_outbox(db)
                    if result["published"] or result["failed"]:
                        logger.info("student-sync drain: %s", result)
                finally:
                    db.close()
        except Exception as exc:  # pragma: no cover - loop must never die
            logger.warning("student-sync drain pass failed (non-fatal): %s", exc)
        _time.sleep(poll_seconds)


def drain_outbox(db: Session, *, batch: int = 50) -> dict:
    """Publish pending outbox rows. Returns {published, failed, retried}. Safe to call repeatedly."""
    Model = _outbox_model()
    now = datetime.now(timezone.utc)
    rows = (
        db.query(Model)
        .filter(
            Model.status == "pending",
            (Model.next_attempt_at.is_(None)) | (Model.next_attempt_at <= now),
        )
        .order_by(Model.id.asc())
        .limit(batch)
        .all()
    )
    published = failed = retried = 0
    for row in rows:
        ok, detail = _deliver(row)
        if ok:
            row.status = "done"
            row.published_at = datetime.now(timezone.utc)
            row.last_error = None
            published += 1
        else:
            row.attempts = (row.attempts or 0) + 1
            row.last_error = detail
            if row.attempts >= _MAX_ATTEMPTS:
                row.status = "failed"  # surfaced for an operator; not retried
                failed += 1
            else:
                backoff = min(3600, 30 * (2 ** (row.attempts - 1)))
                row.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=backoff)
                retried += 1
        db.commit()
    return {"published": published, "failed": failed, "retried": retried}

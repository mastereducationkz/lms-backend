"""Nightly platform job (Platform Integration Pack §2.5), run in the scheduler container:

1. re-resolve events stored with ``error='unresolved'`` (the student appeared in the LMS later);
2. reconcile the last 7 days from IELTS's ``batch-scores-by-date`` for every student in an
   active IELTS group, so a lost event never leaves a hole (platforms stay the source of truth);
3. prune ``platform_events`` older than 400 days.

Gated by ``PLATFORM_EVENTS_INGEST_ENABLED`` like the ingest endpoint — nothing runs while the
pack is switched off.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.integrations.models import PlatformEvent, PlatformResult, PlatformWeeklySet
from src.integrations.projection import (
    STATUS_RANK, ProjectionDataError, UnhandledEventType, apply_event,
)
from src.integrations.resolver import resolve_user_id

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
ALMATY = timezone(timedelta(hours=5))
NIGHTLY_AT = (3, 30)  # local Asia/Almaty
RECONCILE_DAYS = 7
RERESOLVE_DAYS = 30
RETENTION_DAYS = 400

# batch-scores-by-date item fields per module (camelCase, additive on the IELTS side).
_MODULE_FIELDS = {
    "listening": ("listeningAttemptId", "listeningBand", "listeningTestName", "listeningResultUrl", "listeningStatus"),
    "reading": ("readingAttemptId", "readingBand", "readingTestName", "readingResultUrl", "readingStatus"),
    "writing": ("writingSessionId", "writingBand", "writingTestName", "writingResultUrl", "writingStatus"),
    "speaking": ("speakingAttemptId", "speakingBand", "speakingTestName", "speakingResultUrl", "speakingStatus"),
}
_STATUS_FROM_PLATFORM = {"completed": "completed", "in_progress": "started"}

FetchFn = Callable[[list[str], str], Optional[dict]]


def enabled() -> bool:
    return os.getenv("PLATFORM_EVENTS_INGEST_ENABLED", "").strip().lower() in _TRUTHY


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- who ------------------------------------------------------------------------------

def ielts_track_emails(db: Session) -> list[str]:
    """Distinct lowercased emails of active students in active IELTS groups."""
    from src.auth.models import UserInDB
    from src.courses.models import Group, GroupStudent

    rows = (
        db.query(func.lower(func.trim(UserInDB.email)))
        .join(GroupStudent, GroupStudent.student_id == UserInDB.id)
        .join(Group, Group.id == GroupStudent.group_id)
        .filter(
            Group.program_type == "ielts",
            or_(Group.is_active.is_(True), Group.is_active.is_(None)),
            UserInDB.is_active.is_(True),
            UserInDB.email.isnot(None),
        )
        .distinct()
        .all()
    )
    return sorted(email for (email,) in rows if email and "@" in email)


# --- reconciliation --------------------------------------------------------------------

def _default_fetch(emails: list[str], date_str: str) -> Optional[dict]:
    from src.services.ielts_service import IELTSService

    return asyncio.run(IELTSService.fetch_batch_scores_by_date(emails, date_str))


def reconcile_ielts(db: Session, *, days: int = RECONCILE_DAYS, today: Optional[date] = None,
                    fetch: Optional[FetchFn] = None) -> dict:
    emails = ielts_track_emails(db)
    out = {"students": len(emails), "days": 0, "weekly_sets": 0, "results": 0, "errors": 0}
    if not emails:
        return out
    fetch = fetch or _default_fetch
    today = today or datetime.now(ALMATY).date()
    seen_sets: set[int] = set()
    for offset in range(days):
        date_str = (today - timedelta(days=offset)).isoformat()
        try:
            payload = fetch(emails, date_str) or {}
        except Exception as exc:  # noqa: BLE001 - one bad day must not stop the others
            out["errors"] += 1
            logger.warning("platform reconcile: IELTS fetch for %s failed: %s", date_str, exc)
            continue
        out["days"] += 1
        set_id = payload.get("weeklySetId")
        if set_id is not None:
            if set_id in seen_sets:
                continue  # same weekly set as an earlier day: identical answer
            seen_sets.add(set_id)
            _upsert_weekly_set_from_payload(db, payload)
            out["weekly_sets"] += 1
        for item in payload.get("results") or []:
            out["results"] += _upsert_item(db, item, set_id)
        db.commit()
    return out


def _upsert_weekly_set_from_payload(db: Session, payload: dict) -> None:
    set_id = int(payload["weeklySetId"])
    row = (
        db.query(PlatformWeeklySet)
        .filter(PlatformWeeklySet.platform == "ielts", PlatformWeeklySet.weekly_set_id == set_id)
        .first()
    )
    if row is None:
        row = PlatformWeeklySet(platform="ielts", weekly_set_id=set_id, track="ielts", is_active=True)
        db.add(row)
    if payload.get("weeklySetTitle"):
        row.title = str(payload["weeklySetTitle"])
    for key, attr in (("weeklySetDateFrom", "date_from"), ("weeklySetDateTo", "date_to")):
        value = payload.get(key)
        if value:
            try:
                setattr(row, attr, date.fromisoformat(str(value)[:10]))
            except ValueError:
                logger.warning("platform reconcile: bad %s=%r for set %s", key, value, set_id)
    db.flush()


def _upsert_item(db: Session, item: dict, set_id) -> int:
    email = (item.get("email") or "").strip().lower()
    user_id = resolve_user_id(db, zitadel_subject=None, email=email) if email else None
    touched = 0
    for module, (ref_key, band_key, title_key, url_key, status_key) in _MODULE_FIELDS.items():
        ref = item.get(ref_key)
        if ref in (None, ""):
            continue
        band = item.get(band_key)
        status = "scored" if band is not None else _STATUS_FROM_PLATFORM.get(item.get(status_key))
        if status is None:
            continue
        ref = str(ref)
        row = (
            db.query(PlatformResult)
            .filter(PlatformResult.platform == "ielts", PlatformResult.module == module,
                    PlatformResult.attempt_ref == ref)
            .first()
        )
        if row is None:
            row = PlatformResult(platform="ielts", track="ielts", module=module, attempt_ref=ref, status=status)
            db.add(row)
        if row.user_id is None and user_id is not None:
            row.user_id = user_id
        if set_id is not None:
            row.weekly_set_id = int(set_id)
        if item.get(title_key):
            row.test_title = str(item[title_key])
        if item.get(url_key):
            row.result_url = str(item[url_key])
        if STATUS_RANK[status] >= STATUS_RANK.get(row.status, 0):
            row.status = status
        if band is not None and row.band is None:
            row.band = float(band)  # never overwrite an event-sourced band
        touched += 1
    db.flush()
    return touched


# --- re-resolution + prune -----------------------------------------------------------

def re_resolve_unresolved(db: Session, *, days: int = RERESOLVE_DAYS, now: Optional[datetime] = None) -> int:
    now = now or _utcnow()
    events = (
        db.query(PlatformEvent)
        .filter(PlatformEvent.error == "unresolved", PlatformEvent.received_at >= now - timedelta(days=days))
        .order_by(PlatformEvent.occurred_at.asc(), PlatformEvent.id.asc())
        .all()
    )
    fixed = 0
    for event in events:
        user_id = resolve_user_id(db, zitadel_subject=event.zitadel_subject, email=event.email)
        if user_id is None:
            continue
        event.user_id = user_id
        event.error = None
        try:
            apply_event(db, event)  # idempotent: fills user_id on the projected rows
            event.processed_at = event.processed_at or now
        except UnhandledEventType:
            event.error = "unhandled_event_type"
        except ProjectionDataError as exc:
            event.error = f"projection: {exc}"
        fixed += 1
    db.commit()
    return fixed


def prune_events(db: Session, *, days: int = RETENTION_DAYS, now: Optional[datetime] = None) -> int:
    now = now or _utcnow()
    deleted = (
        db.query(PlatformEvent)
        .filter(PlatformEvent.received_at < now - timedelta(days=days))
        .delete(synchronize_session=False)
    )
    db.commit()
    return int(deleted or 0)


def run_nightly(db: Session) -> dict:
    return {
        "re_resolved": re_resolve_unresolved(db),
        "reconcile": reconcile_ielts(db),
        "pruned": prune_events(db),
    }


# --- scheduling ------------------------------------------------------------------------

def nightly_due(now_utc: datetime, last_run_date: Optional[date]) -> bool:
    """Once per Almaty day, at or after 03:30 local."""
    local = now_utc.replace(tzinfo=timezone.utc).astimezone(ALMATY)
    if (local.hour, local.minute) < NIGHTLY_AT:
        return False
    return last_run_date != local.date()


class PlatformNightlyScheduler:
    """Daemon thread: checks every minute, runs the nightly job once per day when enabled."""

    def __init__(self, check_interval: int = 60):
        self.check_interval = check_interval
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.last_run_date: Optional[date] = None

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="platform-nightly")
        self.thread.start()
        logger.info("platform nightly scheduler started (03:30 Asia/Almaty, enabled=%s)", enabled())

    def stop(self) -> None:
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)

    def _run(self) -> None:
        while self.running:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - never die on a tick
                logger.error("platform nightly tick failed: %s", exc, exc_info=True)
            time.sleep(self.check_interval)

    def tick(self) -> None:
        now = _utcnow()
        if not enabled() or not nightly_due(now, self.last_run_date):
            return
        from src.config import SessionLocal

        db = SessionLocal()
        try:
            result = run_nightly(db)
            logger.info("platform nightly: %s", result)
        finally:
            db.close()
        self.last_run_date = now.replace(tzinfo=timezone.utc).astimezone(ALMATY).date()

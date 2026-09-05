"""Weekly sets as calendar events (lead decision 2026-09-05).

A published weekly set is one ``weekly_test`` calendar event per (platform, set), attached to the
target groups (same rule as before: active, non-special groups of the set's track that have not
opted out) and linking to the set page on the platform. Web and mobile open that link through the
handoff mint, so the student lands signed in. Homework rows are no longer created for weekly
tests — a "NOT SUBMITTED / SUBMIT" row for a test taken on another platform confused students.

Idempotent by ``platform_test_events`` (platform, weekly_set_id) → event: ``updated`` recomputes
title, window, link and groups; ``unpublished`` deactivates the event (groups kept so a republish
reattaches nothing); sets without a window, or already past, never get a new event.
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from src.integrations import platform_assignments as pa
from src.integrations.models import PlatformTestEvent, PlatformWeeklySet

log = logging.getLogger(__name__)

EVENT_TYPE = "weekly_test"
_DEFAULT_TRACK_URLS = {
    "ielts": "https://ielts.mastereducation.kz",
    "sat": "https://sat.mastereducation.kz",
    "nuet": "https://nuet.mastereducation.kz",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def track_base_url(track: str) -> Optional[str]:
    """Host of a track's student app (``<TRACK>_PLATFORM_URL`` overrides the default)."""
    track = (track or "").lower()
    if track not in _DEFAULT_TRACK_URLS:
        return None
    return (os.getenv(f"{track.upper()}_PLATFORM_URL", "").strip() or _DEFAULT_TRACK_URLS[track]).rstrip("/")


def set_url(ws: PlatformWeeklySet) -> Optional[str]:
    """Absolute URL of the set page — the calendar event's link."""
    base = track_base_url(pa.track_of(ws))
    if base is None:
        return None
    path = getattr(ws, "set_path", None) or pa.default_set_path(ws) or "/"
    if not path.startswith("/") or path.startswith("//"):
        path = "/"
    return f"{base}{path}"


def author_id(db: Session) -> Optional[int]:
    """The user auto-created events are filed under: ``PLATFORM_EVENTS_AUTHOR_ID`` or the first
    active admin. None (no admin at all) means nothing is created."""
    from src.auth.models import UserInDB

    raw = os.getenv("PLATFORM_EVENTS_AUTHOR_ID", "").strip()
    if raw.isdigit():
        return int(raw)
    admin = (
        db.query(UserInDB)
        .filter(UserInDB.role == "admin", UserInDB.is_active.is_(True))
        .order_by(UserInDB.id.asc())
        .first()
    )
    return admin.id if admin else None


def build_description(ws: PlatformWeeklySet) -> str:
    label = pa.PLATFORM_LABEL.get(pa.track_of(ws), pa.track_of(ws).upper())
    parts = ", ".join(m["module"].capitalize() for m in pa.build_content(ws)["modules"]) or "all parts"
    return (f"Weekly test on the {label} platform: {parts}. "
            "Open the set from this event; your results appear on the dashboard automatically.")


def _window(ws: PlatformWeeklySet) -> tuple[datetime, datetime]:
    end = ws.date_to
    start = ws.date_from or (end - timedelta(days=1))
    return start, end


def _apply(event, ws: PlatformWeeklySet) -> None:
    event.title = pa.build_title(ws)
    event.description = build_description(ws)
    event.start_datetime, event.end_datetime = _window(ws)
    event.is_online = True
    event.meeting_url = set_url(ws)
    event.is_active = True


def sync_weekly_set_event(db: Session, ws: PlatformWeeklySet, *, now: Optional[datetime] = None) -> dict:
    """Create/update the set's calendar event and its group attachments; deactivate when the set
    is unpublished or has no window. Idempotent."""
    from src.events.models import Event, EventGroup

    out = {"created": 0, "updated": 0, "deactivated": 0, "groups_added": 0, "groups_removed": 0}
    if not pa.enabled():
        return out
    now = now or _utcnow()
    live = bool(ws.is_active) and pa.has_window(ws)
    targets = {g.id for g in pa.target_groups(db, pa.track_of(ws))} if live else set()

    link = (
        db.query(PlatformTestEvent)
        .filter(PlatformTestEvent.platform == ws.platform, PlatformTestEvent.weekly_set_id == ws.weekly_set_id)
        .first()
    )
    event = db.query(Event).filter(Event.id == link.event_id).first() if link else None

    if event is None:
        if not live or pa.is_past(ws, now) or not targets:
            return out
        author = author_id(db)
        if author is None:
            log.warning("platform_calendar: no admin user to file the weekly test event under; skipped")
            return out
        event = Event(event_type=EVENT_TYPE, created_by=author, is_recurring=False, title="",
                      start_datetime=now, end_datetime=now)
        _apply(event, ws)
        db.add(event)
        db.flush()
        db.add(PlatformTestEvent(event_id=event.id, platform=ws.platform, weekly_set_id=ws.weekly_set_id))
        out["created"] = 1
    elif live:
        _apply(event, ws)
        out["updated"] = 1
    else:
        if event.is_active:
            event.is_active = False
            out["deactivated"] = 1
        db.commit()
        return out  # groups kept: a republish reactivates the same event

    existing = {eg.group_id: eg for eg in db.query(EventGroup).filter(EventGroup.event_id == event.id).all()}
    for group_id in sorted(targets - set(existing)):
        db.add(EventGroup(event_id=event.id, group_id=group_id))
        out["groups_added"] += 1
    for group_id, eg in existing.items():
        if group_id not in targets:
            db.delete(eg)
            out["groups_removed"] += 1
    db.commit()
    return out


def sync_all_active(db: Session, platform: Optional[str] = None, *, now: Optional[datetime] = None) -> dict:
    """Run ``sync_weekly_set_event`` for every active set (nightly job, enable-time CLI)."""
    query = db.query(PlatformWeeklySet).filter(PlatformWeeklySet.is_active.is_(True))
    if platform:
        query = query.filter(PlatformWeeklySet.platform == platform)
    out = {"sets": 0, "created": 0, "updated": 0, "deactivated": 0, "groups_added": 0, "groups_removed": 0}
    for ws in query.order_by(PlatformWeeklySet.weekly_set_id.asc()).all():
        result = sync_weekly_set_event(db, ws, now=now)
        out["sets"] += 1
        for key, value in result.items():
            out[key] += value
    return out


def main() -> None:  # pragma: no cover - operator CLI
    from src.config import SessionLocal

    parser = argparse.ArgumentParser(description="Sync weekly-test calendar events for active sets")
    parser.add_argument("--platform", default=None, help="ielts | sat (default: all)")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        print(sync_all_active(db, args.platform))
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    main()

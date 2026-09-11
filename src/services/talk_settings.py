"""The talk-time switch an admin flips inside the LMS (owner, 2026-09-11).

Two switches in one setting. ``enabled`` is talk time itself: while on, Meet transcribes every
LMS lesson room (everyone in the call sees Meet's notice), and lessons show who spoke for how
long. ``transcripts`` is the paid half — Deepgram turning each recording into readable words,
which is what the searchable transcript and the question counts need. Off stops new work and
hides the panels; what was already saved stays, and comes back when switched on again.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func

from src.schemas.models import AppSetting, LessonTranscript, UserInDB
from src.utils.utc_json import utc_z

KEY = "talk_time"
DEFAULTS = {"enabled": False, "transcripts": True, "enabled_at": None}

# Deepgram Nova-3 multilingual, pre-recorded: $0.0052 a minute (2026-09). An estimate for the
# admin's eye, not an invoice.
USD_PER_AUDIO_HOUR = 0.0052 * 60

# Who may see the switch; only admins may flip it.
READERS = frozenset({"admin", "head_curator", "head_teacher"})
WRITERS = frozenset({"admin"})


def deepgram_key() -> Optional[str]:
    value = os.getenv("DEEPGRAM_API_KEY")
    return value.strip() if value and value.strip() else None


def _row(db) -> Optional[AppSetting]:
    return db.get(AppSetting, KEY)


def current(db) -> dict:
    row = _row(db)
    return {**DEFAULTS, **(row.value if row and isinstance(row.value, dict) else {})}


def enabled(db) -> bool:
    return bool(current(db)["enabled"])


def transcripts_enabled(db) -> bool:
    """Deepgram runs only while talk time is on, its own switch is on, and there is a key."""
    value = current(db)
    return bool(value["enabled"] and value["transcripts"] and deepgram_key())


def update(db, user, *, enabled: Optional[bool] = None, transcripts: Optional[bool] = None) -> dict:
    value = current(db)
    if enabled is not None:
        if enabled and not value["enabled"]:
            value["enabled_at"] = utc_z(datetime.now(timezone.utc).replace(tzinfo=None))
        value["enabled"] = bool(enabled)
    if transcripts is not None:
        value["transcripts"] = bool(transcripts)
    row = _row(db)
    if row is None:
        row = AppSetting(key=KEY)
        db.add(row)
    row.value = value
    row.updated_by = getattr(user, "id", None)
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return value


def usage(db, now: Optional[datetime] = None) -> dict:
    """This calendar month's Deepgram work: lessons, audio hours, an estimated cost."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    lessons, seconds = (db.query(func.count(LessonTranscript.id), func.coalesce(func.sum(LessonTranscript.audio_seconds), 0.0))
                        .filter(LessonTranscript.status == "ready", LessonTranscript.completed_at >= first)
                        .one())
    hours = float(seconds or 0) / 3600
    return {"month": first.strftime("%Y-%m"), "lessons": int(lessons or 0), "audio_hours": round(hours, 1),
            "estimated_usd": round(hours * USD_PER_AUDIO_HOUR, 2)}


def last_error(db) -> Optional[str]:
    row = (db.query(LessonTranscript.error)
           .filter(LessonTranscript.error.isnot(None))
           .order_by(LessonTranscript.id.desc()).first())
    return row[0][:300] if row else None


def describe(db) -> dict:
    """The switch as the settings popover reads it."""
    value = current(db)
    row = _row(db)
    by = db.get(UserInDB, row.updated_by) if row and row.updated_by else None
    return {
        "enabled": bool(value["enabled"]),
        "transcripts": bool(value["transcripts"]),
        "enabled_at": value.get("enabled_at"),
        "updated_by": by.name if by else None,
        "deepgram_configured": deepgram_key() is not None,
        "usage": usage(db),
        "last_error": last_error(db),
    }

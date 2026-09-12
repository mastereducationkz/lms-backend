"""The switch behind the bot that answers in a group's Telegram chat (owner, 2026-09-12).

Off until an admin turns it on, and then only for the chats of the recording pilot: the same
teachers whose lessons already get an LMS Meet room. ``scope="all"`` opens it to every linked
chat when the pilot has earned it.

The switch lives where talk time's does — one row in ``app_settings`` — because both answer the
same question for an admin: is this thing speaking to students right now?
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import exists

from src.schemas.models import AppSetting, Event, EventGroup, Group, UserInDB
from src.utils.utc_json import utc_z

KEY = "group_bot"
SCOPE_PILOT, SCOPE_ALL = "pilot", "all"
DEFAULTS = {"enabled": False, "scope": SCOPE_PILOT, "enabled_at": None}

# Who may see the switch; only admins may flip it — the same gate talk time uses.
READERS = frozenset({"admin", "head_curator", "head_teacher"})
WRITERS = frozenset({"admin"})


def _row(db) -> Optional[AppSetting]:
    return db.get(AppSetting, KEY)


def current(db) -> dict:
    row = _row(db)
    return {**DEFAULTS, **(row.value if row and isinstance(row.value, dict) else {})}


def enabled(db) -> bool:
    return bool(current(db)["enabled"])


def in_pilot(db, group: Group) -> bool:
    """A pilot group: its own teacher has a Workspace account, or the teacher of one of its
    lessons does — the same test that decides whether a lesson gets an LMS Meet room."""
    if group.teacher_id is not None:
        owner = db.get(UserInDB, group.teacher_id)
        if owner is not None and owner.workspace_email:
            return True
    return bool(db.query(exists().where(
        (EventGroup.group_id == group.id)
        & (Event.id == EventGroup.event_id)
        & (UserInDB.id == Event.teacher_id)
        & (UserInDB.workspace_email.isnot(None))
    )).scalar())


def enabled_for(db, group: Group) -> bool:
    """Whether the bot answers in this group's chat at all."""
    value = current(db)
    if not value["enabled"]:
        return False
    return value["scope"] == SCOPE_ALL or in_pilot(db, group)


def update(db, user, *, enabled: Optional[bool] = None, scope: Optional[str] = None) -> dict:
    value = current(db)
    if enabled is not None:
        if enabled and not value["enabled"]:
            value["enabled_at"] = utc_z(datetime.now(timezone.utc).replace(tzinfo=None))
        value["enabled"] = bool(enabled)
    if scope is not None:
        if scope not in (SCOPE_PILOT, SCOPE_ALL):
            raise ValueError("scope must be 'pilot' or 'all'")
        value["scope"] = scope
    row = _row(db)
    if row is None:
        row = AppSetting(key=KEY)
        db.add(row)
    row.value = value
    row.updated_by = getattr(user, "id", None)
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return value


def describe(db) -> dict:
    """The switch as a settings panel reads it."""
    from src.services import group_bot

    value = current(db)
    row = _row(db)
    by = db.get(UserInDB, row.updated_by) if row and row.updated_by else None
    return {
        "enabled": bool(value["enabled"]),
        "scope": value["scope"],
        "enabled_at": value.get("enabled_at"),
        "updated_by": by.name if by else None,
        "model_configured": group_bot.model_key() is not None,
        "questions_this_week": group_bot.recent_count(db),
    }

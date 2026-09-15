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
DEFAULTS = {"enabled": False, "scope": SCOPE_PILOT, "enabled_at": None, "test_chats": {},
            # v3 (owner, 2026-09-15): the automatic hello waits for the owner's approval of its
            # neutral one-to-one text; the digest is on unless switched off, globally or per group.
            "auto_hello_enabled": False, "digest_enabled": True, "digest_off_groups": []}

_MISSING = object()

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


def test_chat_group(db, support_group_id: int) -> Optional[int]:
    """The LMS group a staff test chat answers about, if it is one.

    ``test_chats`` maps a Support chat id to a real group's id, so the whole bot can be tried
    end-to-end in a chat with no students («IT отдел», 2026-09-15) without linking that chat —
    a link is one chat per group and the group already has its students' chat. A test chat
    never notifies the group's curator.
    """
    mapping = current(db).get("test_chats")
    value = mapping.get(str(support_group_id)) if isinstance(mapping, dict) else None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def is_live(db, group: Group, now: Optional[datetime] = None, *, directory=_MISSING) -> bool:
    """Whether the bot may speak in this group's chat on its own — hello, pinned timetable,
    timetable notices, digest (owner, 2026-09-15).

    Stricter than :func:`in_pilot`, which only decides whether questions are answered: the chat is
    linked (or is a staff test chat), the group runs (active, not over, a class still ahead), and
    its **regular** teacher's Workspace account is connected and not suspended in the uploaded
    directory. A group whose only connected teacher is a substitute is not live — its own lessons
    get no Meet room — and no directory uploaded means nobody is live, not everybody.
    """
    from src.announcements.models import TelegramGroupLink
    from src.services import workspace_directory

    if group is None or not group.is_active or group.is_over:
        return False
    linked = db.query(TelegramGroupLink.id).filter(TelegramGroupLink.lms_group_id == group.id).first()
    if linked is None:
        mapping = current(db).get("test_chats")
        if not (isinstance(mapping, dict) and str(group.id) in {str(v) for v in mapping.values()}):
            return False
    teacher = db.get(UserInDB, group.teacher_id) if group.teacher_id else None
    email = (teacher.workspace_email or "").strip().lower() if teacher is not None else ""
    if not email:
        return False
    accounts = workspace_directory.accounts_by_email(db) if directory is _MISSING else directory
    account = (accounts or {}).get(email)
    if not account or account.get("suspended"):
        return False
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return db.query(Event.id).join(EventGroup, EventGroup.event_id == Event.id).filter(
        EventGroup.group_id == group.id, Event.is_active.is_(True), Event.event_type == "class",
        Event.end_datetime > now).first() is not None


def auto_hello_enabled(db) -> bool:
    return bool(current(db).get("auto_hello_enabled"))


def digest_enabled_for(db, group_id: int) -> bool:
    value = current(db)
    off = value.get("digest_off_groups") or []
    return bool(value.get("digest_enabled", True)) and int(group_id) not in {int(g) for g in off}


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

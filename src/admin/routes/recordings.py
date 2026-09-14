"""Admin surface for connecting teachers to the recordings pipeline.

The whole switch is ``users.workspace_email``: set it and the scheduler's recordings worker
gives the teacher's upcoming lessons LMS Meet rooms, records them, and sends Telegram
invitations to linked group chats; clear it and no new rooms are made (existing links stay).

These routes replace the one-off production scripts that onboarded the eleven pilot
teachers. They never *create* Google accounts — that happens in the Admin Console from the
CSV ``/export.csv`` produces — they only point the pipeline at an account that exists.
"""
import csv
import io
import logging
import secrets
import string
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.announcements.models import TelegramGroupLink
from src.config import get_db
from src.schemas.models import Event, EventGroup, Group, UserInDB
from src.services import teacher_onboarding
from src.services.operational_groups import event_has_operational_group_clause
from src.utils.permissions import require_role

logger = logging.getLogger(__name__)

router = APIRouter()

READERS = sorted(teacher_onboarding.READERS)
WRITERS = sorted(teacher_onboarding.WRITERS)

# How far ahead the list counts "upcoming" lessons. Matches the window an admin plans a
# rollout around; rooms themselves appear only inside the worker's 3-day horizon.
LIST_WINDOW_DAYS = 30


class TeacherGroupOut(BaseModel):
    id: int
    name: str
    telegram_linked: bool
    lessons: int


class TeacherOut(BaseModel):
    id: int
    name: str
    email: str
    role: str
    is_active: bool
    workspace_email: Optional[str]
    suggested_workspace_email: Optional[str]
    upcoming_lessons: int
    rooms_ready: int
    groups: List[TeacherGroupOut]


class ConnectBody(BaseModel):
    # null disconnects the teacher: no new Meet rooms, no invitations, no bot.
    workspace_email: Optional[str] = None


def _upcoming_lessons(db: Session, now: datetime) -> list:
    """(teacher_id, event_id, meeting_url, group_id) for class lessons in the list window."""
    until = now + timedelta(days=LIST_WINDOW_DAYS)
    return (
        db.query(Event.teacher_id, Event.id, Event.meeting_url, EventGroup.group_id)
        .outerjoin(EventGroup, EventGroup.event_id == Event.id)
        .filter(
            Event.is_active.is_(True),
            Event.event_type == "class",
            Event.teacher_id.isnot(None),
            Event.start_datetime > now,
            Event.start_datetime < until,
            event_has_operational_group_clause(),
        )
        .all()
    )


def _teacher_rows(db: Session) -> List[TeacherOut]:
    now = datetime.utcnow()
    rows = _upcoming_lessons(db, now)

    by_teacher: dict = {}
    for teacher_id, event_id, meeting_url, group_id in rows:
        entry = by_teacher.setdefault(teacher_id, {"lessons": set(), "rooms": set(), "groups": {}})
        entry["lessons"].add(event_id)
        if meeting_url and meeting_url.startswith("https://meet.google.com/"):
            entry["rooms"].add(event_id)
        if group_id:
            entry["groups"].setdefault(group_id, set()).add(event_id)

    linked_groups = {
        row[0]
        for row in db.query(TelegramGroupLink.lms_group_id)
        .filter(TelegramGroupLink.lms_group_id.in_(
            {gid for e in by_teacher.values() for gid in e["groups"]} or {-1}
        ))
        .all()
    }
    group_names = dict(
        db.query(Group.id, Group.name)
        .filter(Group.id.in_({gid for e in by_teacher.values() for gid in e["groups"]} or {-1}))
        .all()
    )

    teachers = (
        db.query(UserInDB)
        .filter(
            UserInDB.role.in_(sorted(teacher_onboarding.TEACHER_ROLES)),
            UserInDB.is_active.is_(True),
        )
        .order_by(func.lower(UserInDB.name))
        .all()
    )
    taken = {u.workspace_email for u in
             db.query(UserInDB.workspace_email).filter(UserInDB.workspace_email.isnot(None)).all()}

    out = []
    for teacher in teachers:
        entry = by_teacher.get(teacher.id, {"lessons": set(), "rooms": set(), "groups": {}})
        # Onboarded teachers stay listed even with no upcoming lessons — an admin must be
        # able to find them to disconnect.
        if teacher.workspace_email is None and not entry["lessons"]:
            continue
        groups = [
            TeacherGroupOut(id=gid, name=group_names.get(gid, f"#{gid}"),
                            telegram_linked=gid in linked_groups, lessons=len(ids))
            for gid, ids in sorted(entry["groups"].items(),
                                   key=lambda kv: (-len(kv[1]), kv[0]))
        ]
        out.append(TeacherOut(
            id=teacher.id,
            name=teacher.name,
            email=teacher.email,
            role=teacher.role,
            is_active=bool(teacher.is_active),
            workspace_email=teacher.workspace_email,
            suggested_workspace_email=(
                None if teacher.workspace_email else
                teacher_onboarding.suggest_workspace_email(
                    teacher.name, taken,
                    group_names=[group_names.get(gid, "") for gid in entry["groups"]],
                )
            ),
            upcoming_lessons=len(entry["lessons"]),
            rooms_ready=len(entry["rooms"]),
            groups=groups,
        ))
    out.sort(key=lambda t: (t.workspace_email is None, -t.upcoming_lessons, (t.name or "").lower()))
    return out


@router.get("/teachers", response_model=List[TeacherOut])
def list_teachers(db: Session = Depends(get_db),
                  current_user: UserInDB = Depends(require_role(READERS))):
    """Every teacher who is connected or has lessons coming: the rollout dashboard."""
    return _teacher_rows(db)


@router.get("/teachers/{user_id}", response_model=TeacherOut)
def get_teacher(user_id: int, db: Session = Depends(get_db),
                current_user: UserInDB = Depends(require_role(READERS))):
    """One teacher's row — the Manage Users dialog prefills its field from this."""
    for row in _teacher_rows(db):
        if row.id == user_id:
            return row
    raise HTTPException(status_code=404, detail="Teacher not found or has no upcoming lessons")


@router.put("/teachers/{user_id}", response_model=TeacherOut)
def connect_teacher(user_id: int, body: ConnectBody, db: Session = Depends(get_db),
                    current_user: UserInDB = Depends(require_role(WRITERS))):
    """Connect (set workspace_email) or disconnect (null) a teacher. Admin only."""
    teacher = db.get(UserInDB, user_id)
    if teacher is None:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        teacher_onboarding.set_workspace_email(db, current_user, teacher, body.workspace_email)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    for row in _teacher_rows(db):
        if row.id == user_id:
            return row
    # A connected teacher with no upcoming lessons isn't in the list — answer with what was set.
    return TeacherOut(
        id=teacher.id, name=teacher.name, email=teacher.email, role=teacher.role,
        is_active=bool(teacher.is_active), workspace_email=teacher.workspace_email,
        suggested_workspace_email=None, upcoming_lessons=0, rooms_ready=0, groups=[],
    )


_GOOGLE_ADMIN_HEADER = [
    "First Name [Required]", "Last Name [Required]", "Email Address [Required]",
    "Password [Required]", "Org Unit Path [Required]",
    "Change a User's Password at Next Sign-in [Upload Only]",
]


def _split_name(name: str, given: str) -> tuple:
    """(first, last) for the Admin-Console CSV — ``given`` is the email's local name so the
    display name always agrees with the address that was actually suggested."""
    words = teacher_onboarding._latin_words(name)
    if not words:
        return (name or "Teacher", "-")
    rest = [w for i, w in enumerate(words) if w != given or i != words.index(given)]
    first = given.capitalize()
    last = " ".join(w.capitalize() for w in rest) or first
    return first, last


@router.get("/export.csv")
def export_workspace_import(org_unit: str = Query("/", description="Google Admin org unit path"),
                            db: Session = Depends(get_db),
                            current_user: UserInDB = Depends(require_role(WRITERS))):
    """Google Admin bulk-import rows for every active teacher not yet connected.

    Passwords are single-use: the import flags each account to force a change at first
    sign-in, so the file can be shared with whoever runs the import and then discarded.
    """
    pending = (
        db.query(UserInDB)
        .filter(
            UserInDB.role.in_(sorted(teacher_onboarding.TEACHER_ROLES)),
            UserInDB.is_active.is_(True),
            UserInDB.workspace_email.is_(None),
        )
        .order_by(func.lower(UserInDB.name))
        .all()
    )
    taken = {u.workspace_email for u in
             db.query(UserInDB.workspace_email).filter(UserInDB.workspace_email.isnot(None)).all()}

    # Group names disambiguate the given name for the CSV just as they do for the list:
    # "August 3 SAT - Киясбек" tells us Қиясбек is the given name, not Мирас.
    now = datetime.utcnow()
    group_names = dict(db.query(Group.id, Group.name).all())
    names_by_teacher: dict = {}
    for teacher_id, _event_id, _url, group_id in _upcoming_lessons(db, now):
        if group_id:
            names_by_teacher.setdefault(teacher_id, []).append(group_names.get(group_id, ""))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_GOOGLE_ADMIN_HEADER)
    claimed = set(taken)
    for teacher in pending:
        suggested = teacher_onboarding.suggest_workspace_email(
            teacher.name, taken | claimed,
            group_names=names_by_teacher.get(teacher.id, []),
        )
        if not suggested:
            continue
        claimed.add(suggested)
        first, last = _split_name(teacher.name or "", suggested.split("@")[0].split(".")[0])
        password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
        writer.writerow([first, last, suggested, password, org_unit or "/", "TRUE"])

    content = buf.getvalue()
    logger.info("recordings: admin %s exported workspace import CSV (%s rows)",
                current_user.id, len(pending))
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="workspace-import.csv"'},
    )

"""Admin surface for connecting teachers to the recordings pipeline.

The whole switch is ``users.workspace_email``: set it and the scheduler's recordings worker
gives the teacher's upcoming lessons LMS Meet rooms, records them, and sends Telegram
invitations to linked group chats; clear it and no new rooms are made (existing links stay).

These routes replace the one-off production scripts that onboarded the eleven pilot teachers.
They never *create* Google accounts. The flow is: upload Google's users list (so the LMS knows
which accounts exist) → review English names and addresses → download the import for the new
accounts → create them in the Admin Console → upload the users list again → connect.
"""
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.announcements.models import TelegramGroupLink
from src.config import get_db
from src.schemas.models import Event, EventGroup, Group, UserInDB
from src.services import teacher_onboarding, workspace_directory, workspace_names
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


class AccountOut(BaseModel):
    """What the uploaded Workspace users list says about the teacher's (proposed) address."""
    status: str  # "exists" | "missing" | "unknown" (no list uploaded, or no address)
    signed_in: Optional[bool] = None
    suspended: bool = False
    org_unit: Optional[str] = None


class TeacherOut(BaseModel):
    id: int
    name: str
    official_full_name: Optional[str]
    email: str
    role: str
    is_active: bool
    workspace_email: Optional[str]
    suggested_workspace_email: Optional[str]
    first_name: str
    last_name: str
    similar_accounts: List[str]
    skipped: bool
    account: AccountOut
    upcoming_lessons: int
    rooms_ready: int
    groups: List[TeacherGroupOut]


class ConnectBody(BaseModel):
    # null disconnects the teacher: no new Meet rooms, no invitations, no bot.
    workspace_email: Optional[str] = None


class SkipBody(BaseModel):
    skipped: bool


class DirectoryAccountOut(BaseModel):
    email: str
    first_name: str
    last_name: str
    org_unit: Optional[str]
    suspended: bool
    signed_in: Optional[bool]


class DirectoryOut(BaseModel):
    uploaded_at: Optional[str]
    uploaded_by: Optional[str]
    count: int
    accounts: List[DirectoryAccountOut]


class DirectoryUpload(BaseModel):
    csv_text: str = Field(..., max_length=workspace_directory.MAX_CSV_CHARS)


class ImportRowIn(BaseModel):
    user_id: int
    workspace_email: str
    first_name: str
    last_name: str


class ImportBody(BaseModel):
    org_unit: str = teacher_onboarding.DEFAULT_ORG_UNIT
    rows: List[ImportRowIn] = Field(..., max_length=500)


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


def _account(directory, address: Optional[str]) -> AccountOut:
    if directory is None or not address:
        return AccountOut(status="unknown")
    account = directory.get(address)
    if account is None:
        return AccountOut(status="missing")
    return AccountOut(status="exists", signed_in=account.get("signed_in"),
                      suspended=bool(account.get("suspended")), org_unit=account.get("org_unit"))


def _teacher_rows(db: Session, include_id: Optional[int] = None) -> List[TeacherOut]:
    """Connected teachers, teachers with lessons coming, and ``include_id`` whatever its state."""
    by_teacher: dict = {}
    for teacher_id, event_id, meeting_url, group_id in _upcoming_lessons(db, datetime.utcnow()):
        entry = by_teacher.setdefault(teacher_id, {"lessons": set(), "rooms": set(), "groups": {}})
        entry["lessons"].add(event_id)
        if meeting_url and meeting_url.startswith("https://meet.google.com/"):
            entry["rooms"].add(event_id)
        if group_id:
            entry["groups"].setdefault(group_id, set()).add(event_id)

    group_ids = {gid for e in by_teacher.values() for gid in e["groups"]} or {-1}
    linked_groups = {row[0] for row in db.query(TelegramGroupLink.lms_group_id)
                     .filter(TelegramGroupLink.lms_group_id.in_(group_ids)).all()}
    group_names = dict(db.query(Group.id, Group.name).filter(Group.id.in_(group_ids)).all())

    teachers = [
        t for t in db.query(UserInDB)
        .filter(UserInDB.role.in_(sorted(teacher_onboarding.TEACHER_ROLES)))
        .order_by(func.lower(UserInDB.name)).all()
        if t.id == include_id or (t.is_active and (t.workspace_email or t.id in by_teacher))
    ]
    taken = teacher_onboarding.connected_addresses(db)
    directory = workspace_directory.accounts_by_email(db)
    skipped = teacher_onboarding.skipped_ids(db)

    def lessons(t):
        return len(by_teacher.get(t.id, {}).get("lessons", ()))

    # Claim addresses busiest teacher first, so the plain given-name address goes to the teacher
    # the rollout reaches first, and the page and the import can never disagree about it.
    proposals, claimed = {}, set()
    for t in sorted((t for t in teachers if not t.workspace_email),
                    key=lambda t: (t.id in skipped, -lessons(t), t.id)):
        proposal = teacher_onboarding.propose(t, taken=taken | claimed, directory=directory)
        if proposal.address:
            claimed.add(proposal.address)
        proposals[t.id] = proposal

    out = []
    for t in teachers:
        entry = by_teacher.get(t.id, {"lessons": set(), "rooms": set(), "groups": {}})
        if t.workspace_email:
            first, last = workspace_names.english_name(t.name, t.official_full_name)
            suggested, similar = None, []
        else:
            p = proposals[t.id]
            first, last, suggested, similar = p.first_name, p.last_name, p.address, p.similar
        out.append(TeacherOut(
            id=t.id, name=t.name, official_full_name=t.official_full_name, email=t.email,
            role=t.role, is_active=bool(t.is_active), workspace_email=t.workspace_email,
            suggested_workspace_email=suggested, first_name=first, last_name=last,
            similar_accounts=similar, skipped=t.id in skipped,
            account=_account(directory, t.workspace_email or suggested),
            upcoming_lessons=len(entry["lessons"]), rooms_ready=len(entry["rooms"]),
            groups=[
                TeacherGroupOut(id=gid, name=group_names.get(gid, f"#{gid}"),
                                telegram_linked=gid in linked_groups, lessons=len(ids))
                for gid, ids in sorted(entry["groups"].items(), key=lambda kv: (-len(kv[1]), kv[0]))
            ],
        ))
    # Launch order: pending first (busiest first), then connected, then skipped.
    out.sort(key=lambda r: (r.skipped, r.workspace_email is not None, -r.upcoming_lessons, r.name.lower()))
    return out


def _row(db: Session, user_id: int) -> TeacherOut:
    for row in _teacher_rows(db, include_id=user_id):
        if row.id == user_id:
            return row
    raise HTTPException(status_code=404, detail="Teacher not found")


@router.get("/teachers", response_model=List[TeacherOut])
def list_teachers(db: Session = Depends(get_db),
                  current_user: UserInDB = Depends(require_role(READERS))):
    """Every teacher who is connected or has lessons coming: the rollout dashboard."""
    return _teacher_rows(db)


@router.get("/teachers/{user_id}", response_model=TeacherOut)
def get_teacher(user_id: int, db: Session = Depends(get_db),
                current_user: UserInDB = Depends(require_role(READERS))):
    """One teacher's row, lessons or not — the Manage Users dialog prefills from this."""
    return _row(db, user_id)


@router.put("/teachers/{user_id}", response_model=TeacherOut)
def connect_teacher(user_id: int, body: ConnectBody, db: Session = Depends(get_db),
                    current_user: UserInDB = Depends(require_role(WRITERS))):
    """Connect (an address that exists in Workspace) or disconnect (null) a teacher."""
    teacher = db.get(UserInDB, user_id)
    if teacher is None:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        teacher_onboarding.set_workspace_email(db, current_user, teacher, body.workspace_email)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _row(db, user_id)


@router.put("/teachers/{user_id}/skip", response_model=TeacherOut)
def skip_teacher(user_id: int, body: SkipBody, db: Session = Depends(get_db),
                 current_user: UserInDB = Depends(require_role(WRITERS))):
    """Leave a teacher out of the rollout (or bring them back). Changes nothing else."""
    teacher = db.get(UserInDB, user_id)
    if teacher is None:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        teacher_onboarding.set_skipped(db, current_user, teacher, body.skipped)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _row(db, user_id)


@router.get("/directory", response_model=DirectoryOut)
def get_directory(db: Session = Depends(get_db),
                  current_user: UserInDB = Depends(require_role(READERS))):
    """The Workspace users list last uploaded, with when and by whom."""
    return workspace_directory.describe(db)


@router.post("/directory", response_model=DirectoryOut)
def upload_directory(body: DirectoryUpload, db: Session = Depends(get_db),
                     current_user: UserInDB = Depends(require_role(WRITERS))):
    """Replace the users list with Google Admin's "Download users" CSV."""
    try:
        accounts = workspace_directory.parse_users_csv(body.csv_text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    workspace_directory.store(db, current_user, accounts)
    logger.info("recordings onboarding: admin %s uploaded the Workspace users list (%s accounts)",
                current_user.id, len(accounts))
    return workspace_directory.describe(db)


@router.post("/export.csv")
def export_workspace_import(body: ImportBody, db: Session = Depends(get_db),
                            current_user: UserInDB = Depends(require_role(WRITERS))):
    """Google Admin bulk-upload rows for exactly the teachers and names the admin reviewed."""
    rows = [teacher_onboarding.ImportRow(**r.model_dump()) for r in body.rows]
    try:
        content = teacher_onboarding.build_import_csv(db, current_user, rows, body.org_unit)
    except teacher_onboarding.ImportRefused as e:
        raise HTTPException(status_code=400, detail=" | ".join(e.problems))
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="workspace-import.csv"'},
    )

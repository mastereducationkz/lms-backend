"""Internal curator API consumed by the CRM curator workspace (service-key auth).

The CRM is the canonical *interface* for onboarding; the LMS remains the canonical *store*.
This router is the seam. It exposes the onboarding domain and the curator↔student↔group
projection the CRM needs to compose its financial data around, and nothing else.

Two invariants make the seam safe:

**The LMS re-derives the actor's role from its own database.** The CRM sends only an actor
id (``X-Lms-Actor-Id``); the role, and therefore what the actor may touch, comes from the
``users`` row read here. A stale CRM token, or a CRM bug that mislabels a curator as a head,
cannot widen access — which is exactly the "authorization must use current database roles"
requirement, enforced at the layer that owns the roles.

**CRM staff without an LMS identity are a separate, explicit actor kind.** Managers and
admins may open the head workspace for support. They have no ``users`` row, so they arrive as
``X-Crm-Staff-Role`` and are recorded in the history as CRM staff rather than being silently
attributed to a curator.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from src.config import get_db
from src.curator.email_policy import SUPPRESSION_REASON, is_curator
from src.curator.notifications import (
    notify_once,
    send_curator_email,
    TYPE_ONBOARDING_OVERDUE,
    TYPE_UNASSIGNED_GROUP,
    crm_onboarding_url,
    dispatch_emails,
    head_curators,
    notify_heads,
    notify_responsibility_ended,
    notify_student_assigned,
)
from src.curator.onboarding_core import (
    BOARD_STATUSES,
    ONBOARDING_THRESHOLDS,
    SETTABLE_STATUSES,
    OnboardingActor,
    OnboardingNotFound,
    OnboardingPermissionError,
    add_note,
    curator_student_ids,
    get_card,
    is_overdue,
    load_board,
    reconcile_onboarding,
    reconcile_student,
    serialize_card,
    set_next_action,
    set_status,
    status_counts,
    telegram_link,
)
from src.routes.crm_internal import _require_crm_internal_key
from src.schemas.models import (
    AssignmentZeroSubmission,
    Course,
    CourseGroupAccess,
    CuratorOnboarding,
    Group,
    GroupStudent,
    UserInDB,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(_require_crm_internal_key)])

CURATOR_ROLES = ("curator", "head_curator")
#: Roles that may act through this API at all.
ACTOR_ROLES = ("curator", "head_curator", "admin")
#: CRM staff roles granted head-curator-equivalent oversight (read + intervene, never
#: financial mutation — that is enforced on the CRM side, which owns the money).
CRM_STAFF_OVERSIGHT_ROLES = ("admin", "manager")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- actor resolution ---------------------------------------------------------------------


def resolve_actor(
    db: Session = Depends(get_db),
    x_lms_actor_id: Annotated[Optional[str], Header(alias="X-Lms-Actor-Id")] = None,
    x_crm_staff_role: Annotated[Optional[str], Header(alias="X-Crm-Staff-Role")] = None,
) -> OnboardingActor:
    """Build the acting identity from the LMS database, not from what the caller claims."""
    if x_lms_actor_id:
        try:
            actor_id = int(x_lms_actor_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid X-Lms-Actor-Id") from None
        user = db.query(UserInDB).filter(UserInDB.id == actor_id).first()
        if user is None:
            raise HTTPException(status_code=404, detail="Actor not found in LMS")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Actor is inactive")
        if user.role not in ACTOR_ROLES:
            raise HTTPException(status_code=403, detail="Actor is not a curator")
        return OnboardingActor.from_user(user)

    staff_role = (x_crm_staff_role or "").strip().lower()
    if staff_role in CRM_STAFF_OVERSIGHT_ROLES:
        # No LMS identity: recorded as CRM staff oversight, with head-equivalent reach.
        return OnboardingActor(user_id=None, name=f"CRM {staff_role}", role="admin")

    raise HTTPException(
        status_code=400, detail="Missing X-Lms-Actor-Id or X-Crm-Staff-Role"
    )


def _scope_for(db: Session, actor: OnboardingActor) -> Optional[list[int]]:
    """Curator ids the actor may see. ``None`` means organisation-wide."""
    if actor.is_head:
        return None
    return [int(actor.user_id)] if actor.user_id is not None else []


# --- directory ----------------------------------------------------------------------------


class CuratorDirectoryItem(BaseModel):
    lms_user_id: int
    email: Optional[str] = None
    name: Optional[str] = None
    official_full_name: Optional[str] = None
    role: str
    is_active: bool
    central_auth_user_id: Optional[str] = None
    active_group_count: int = 0
    student_count: int = 0


@router.get("/directory", response_model=list[CuratorDirectoryItem])
def curator_directory(
    include_inactive: bool = Query(True),
    db: Session = Depends(get_db),
) -> list[CuratorDirectoryItem]:
    """Every LMS curator / head curator, with workload. Drives identity migration.

    Inactive curators are included by default: the migration must link them too, or a
    curator who is reactivated tomorrow silently has no CRM identity.
    """
    q = db.query(UserInDB).filter(UserInDB.role.in_(CURATOR_ROLES))
    if not include_inactive:
        q = q.filter(UserInDB.is_active == True)  # noqa: E712
    curators = q.order_by(UserInDB.id).all()
    ids = [c.id for c in curators]
    if not ids:
        return []

    group_counts = dict(
        db.query(Group.curator_id, func.count(Group.id))
        .filter(
            Group.curator_id.in_(ids),
            Group.is_active == True,  # noqa: E712
            or_(Group.is_over == False, Group.is_over.is_(None)),  # noqa: E712
        )
        .group_by(Group.curator_id)
        .all()
    )
    student_counts = dict(
        db.query(Group.curator_id, func.count(func.distinct(GroupStudent.student_id)))
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .filter(
            Group.curator_id.in_(ids),
            Group.is_active == True,  # noqa: E712
            or_(Group.is_over == False, Group.is_over.is_(None)),  # noqa: E712
        )
        .group_by(Group.curator_id)
        .all()
    )
    return [
        CuratorDirectoryItem(
            lms_user_id=c.id,
            email=c.email,
            name=c.name,
            official_full_name=getattr(c, "official_full_name", None),
            role=c.role,
            is_active=bool(c.is_active),
            central_auth_user_id=getattr(c, "central_auth_user_id", None),
            active_group_count=int(group_counts.get(c.id, 0) or 0),
            student_count=int(student_counts.get(c.id, 0) or 0),
        )
        for c in curators
    ]


@router.get("/thresholds")
def thresholds() -> dict[str, int]:
    """Domain constants, served rather than duplicated in each frontend."""
    return ONBOARDING_THRESHOLDS


# --- scope --------------------------------------------------------------------------------


class ScopeResponse(BaseModel):
    curator_ids: list[int]
    student_ids: list[int]
    group_ids: list[int]
    is_head: bool


@router.get("/scope", response_model=ScopeResponse)
def get_scope(
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> ScopeResponse:
    """What this actor may see: the authorization primitive the CRM filters every list by."""
    scope = _scope_for(db, actor)
    if scope is None:
        curator_rows = db.query(UserInDB.id).filter(UserInDB.role.in_(CURATOR_ROLES)).all()
        curator_ids = [int(r[0]) for r in curator_rows]
        group_rows = (
            db.query(Group.id)
            .filter(
                Group.is_active == True,  # noqa: E712
                or_(Group.is_over == False, Group.is_over.is_(None)),  # noqa: E712
            )
            .all()
        )
        student_rows = (
            db.query(GroupStudent.student_id)
            .join(Group, Group.id == GroupStudent.group_id)
            .filter(
                Group.is_active == True,  # noqa: E712
                or_(Group.is_over == False, Group.is_over.is_(None)),  # noqa: E712
            )
            .distinct()
            .all()
        )
        return ScopeResponse(
            curator_ids=curator_ids,
            student_ids=[int(r[0]) for r in student_rows],
            group_ids=[int(r[0]) for r in group_rows],
            is_head=True,
        )

    group_rows = (
        db.query(Group.id)
        .filter(
            Group.curator_id.in_(scope),
            Group.is_active == True,  # noqa: E712
            or_(Group.is_over == False, Group.is_over.is_(None)),  # noqa: E712
        )
        .all()
    )
    return ScopeResponse(
        curator_ids=scope,
        student_ids=sorted(curator_student_ids(db, scope)),
        group_ids=[int(r[0]) for r in group_rows],
        is_head=False,
    )


# --- onboarding board ---------------------------------------------------------------------


def _contact_map(db: Session, student_ids: list[int]) -> dict[int, AssignmentZeroSubmission]:
    if not student_ids:
        return {}
    return {
        az.user_id: az
        for az in db.query(AssignmentZeroSubmission)
        .filter(AssignmentZeroSubmission.user_id.in_(student_ids))
        .all()
    }


def _decorate(card: dict[str, Any], az: Optional[AssignmentZeroSubmission]) -> dict[str, Any]:
    card["telegram_id"] = az.telegram_id if az else None
    card["telegram_link"] = telegram_link(az.telegram_id) if az else None
    card["phone_number"] = az.phone_number if az else None
    card["parent_phone_number"] = az.parent_phone_number if az else None
    card["parent_name"] = getattr(az, "parent_name", None) if az else None
    return card


@router.get("/onboarding")
def list_onboarding(
    curator_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None, description="Comma-separated statuses"),
    include_closed: bool = Query(False),
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> dict[str, Any]:
    scope = _scope_for(db, actor)
    if curator_id is not None:
        if scope is not None and int(curator_id) not in scope:
            # Asking about someone else's board is not an error the caller gets to
            # distinguish from an empty one.
            raise HTTPException(status_code=403, detail="Доступ запрещён")
        scope = [int(curator_id)]

    statuses = [s.strip() for s in status.split(",")] if status else None
    if statuses:
        invalid = [s for s in statuses if s not in (*BOARD_STATUSES, "cancelled")]
        if invalid:
            raise HTTPException(status_code=400, detail=f"Недопустимый статус: {invalid[0]}")

    rows = load_board(db, curator_ids=scope, statuses=statuses, include_closed=include_closed)
    az_map = _contact_map(db, [r.student_id for r in rows])
    now = _utcnow()
    return {
        "cards": [
            _decorate(serialize_card(r, now), az_map.get(r.student_id)) for r in rows
        ],
        "thresholds": ONBOARDING_THRESHOLDS,
        "is_head": actor.is_head,
    }


@router.get("/onboarding/counts")
def onboarding_counts(
    curator_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> dict[str, Any]:
    scope = _scope_for(db, actor)
    if curator_id is not None:
        if scope is not None and int(curator_id) not in scope:
            raise HTTPException(status_code=403, detail="Доступ запрещён")
        scope = [int(curator_id)]
    return status_counts(db, curator_ids=scope)


@router.get("/onboarding/per-curator")
def onboarding_per_curator(
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> list[dict[str, Any]]:
    """Per-curator onboarding rollup for the head dashboard. Head/admin only."""
    if not actor.is_head:
        raise HTTPException(status_code=403, detail="Требуется роль старшего куратора")

    curators = (
        db.query(UserInDB)
        .filter(UserInDB.role.in_(CURATOR_ROLES), UserInDB.is_active == True)  # noqa: E712
        .order_by(UserInDB.name)
        .all()
    )
    rows = load_board(db)
    now = _utcnow()
    by_curator: dict[int, dict[str, int]] = {}
    for row in rows:
        bucket = by_curator.setdefault(
            int(row.curator_id), {"new": 0, "in_progress": 0, "done": 0, "overdue": 0}
        )
        if row.status in bucket:
            bucket[row.status] += 1
        if is_overdue(row, now):
            bucket["overdue"] += 1

    group_counts = dict(
        db.query(Group.curator_id, func.count(Group.id))
        .filter(
            Group.curator_id.isnot(None),
            Group.is_active == True,  # noqa: E712
            or_(Group.is_over == False, Group.is_over.is_(None)),  # noqa: E712
        )
        .group_by(Group.curator_id)
        .all()
    )
    student_counts = dict(
        db.query(Group.curator_id, func.count(func.distinct(GroupStudent.student_id)))
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .filter(
            Group.curator_id.isnot(None),
            Group.is_active == True,  # noqa: E712
            or_(Group.is_over == False, Group.is_over.is_(None)),  # noqa: E712
        )
        .group_by(Group.curator_id)
        .all()
    )

    out = []
    for c in curators:
        counts = by_curator.get(c.id, {"new": 0, "in_progress": 0, "done": 0, "overdue": 0})
        total = counts["new"] + counts["in_progress"] + counts["done"]
        out.append(
            {
                "curator_id": c.id,
                "curator_name": (getattr(c, "official_full_name", None) or c.name or ""),
                "email": c.email,
                "role": c.role,
                "student_count": int(student_counts.get(c.id, 0) or 0),
                "group_count": int(group_counts.get(c.id, 0) or 0),
                "onboarding": counts,
                "completion_rate": round(counts["done"] / total, 4) if total else None,
            }
        )
    return out


@router.get("/onboarding/{onboarding_id}")
def get_onboarding_card(
    onboarding_id: int,
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> dict[str, Any]:
    try:
        row = get_card(db, onboarding_id, actor)
    except OnboardingNotFound:
        raise HTTPException(status_code=404, detail="Карточка не найдена") from None
    db.refresh(row)
    az = (
        db.query(AssignmentZeroSubmission)
        .filter(AssignmentZeroSubmission.user_id == row.student_id)
        .first()
    )
    return _decorate(
        serialize_card(row, include_notes=True, include_history=True), az
    )


class StatusBody(BaseModel):
    status: str


@router.patch("/onboarding/{onboarding_id}")
def patch_onboarding(
    onboarding_id: int,
    body: StatusBody,
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> dict[str, Any]:
    if body.status not in SETTABLE_STATUSES:
        raise HTTPException(status_code=400, detail="Недопустимый статус")
    try:
        row = get_card(db, onboarding_id, actor)
        row = set_status(db, row, body.status, actor)
    except OnboardingNotFound:
        raise HTTPException(status_code=404, detail="Карточка не найдена") from None
    except OnboardingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_card(row)


class NextActionBody(BaseModel):
    next_action_at: Optional[date] = None
    note: Optional[str] = Field(None, max_length=500)


@router.put("/onboarding/{onboarding_id}/next-action")
def put_next_action(
    onboarding_id: int,
    body: NextActionBody,
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> dict[str, Any]:
    try:
        row = get_card(db, onboarding_id, actor)
        row = set_next_action(db, row, body.next_action_at, body.note, actor)
    except OnboardingNotFound:
        raise HTTPException(status_code=404, detail="Карточка не найдена") from None
    except OnboardingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return serialize_card(row)


class NoteBody(BaseModel):
    body: str = Field(..., min_length=1, max_length=5000)


@router.post("/onboarding/{onboarding_id}/notes")
def post_note(
    onboarding_id: int,
    body: NoteBody,
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> dict[str, Any]:
    try:
        row = get_card(db, onboarding_id, actor)
        note = add_note(db, row, body.body, actor)
    except OnboardingNotFound:
        raise HTTPException(status_code=404, detail="Карточка не найдена") from None
    except OnboardingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": note.id,
        "body": note.body,
        "author_id": note.author_id,
        "author_name": note.author_name,
        "author_role": note.author_role,
        "created_at": note.created_at.isoformat() if note.created_at else None,
    }


# --- groups & academic projection ---------------------------------------------------------


class GroupItem(BaseModel):
    group_id: int
    name: str
    curator_id: Optional[int] = None
    curator_name: Optional[str] = None
    teacher_id: Optional[int] = None
    teacher_name: Optional[str] = None
    program_type: Optional[str] = None
    group_type: Optional[str] = None
    is_active: bool = True
    is_over: bool = False
    student_count: int = 0
    course_titles: list[str] = Field(default_factory=list)
    is_mine: bool = False


def _course_titles_by_group(db: Session, group_ids: list[int]) -> dict[int, list[str]]:
    if not group_ids:
        return {}
    rows = (
        db.query(CourseGroupAccess.group_id, Course.title)
        .join(Course, Course.id == CourseGroupAccess.course_id)
        .filter(
            CourseGroupAccess.group_id.in_(group_ids),
            CourseGroupAccess.is_active == True,  # noqa: E712
        )
        .all()
    )
    out: dict[int, list[str]] = {}
    for gid, title in rows:
        label = (title or "").strip()
        if not label:
            continue
        bucket = out.setdefault(int(gid), [])
        if label not in bucket:
            bucket.append(label)
    return out


@router.get("/groups", response_model=list[GroupItem])
def list_groups(
    mine_only: bool = Query(False),
    include_completed: bool = Query(False),
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> list[GroupItem]:
    """Groups for the workspace.

    A regular curator gets *their* groups in full detail when ``mine_only``; without it they
    also get the catalogue needed to pick a destination for an add/transfer — name and
    program only, flagged ``is_mine=False``, because choosing a destination must not become
    a way to read another curator's roster.
    """
    scope = _scope_for(db, actor)
    q = db.query(Group).filter(Group.is_active == True)  # noqa: E712
    if not include_completed:
        q = q.filter(or_(Group.is_over == False, Group.is_over.is_(None)))  # noqa: E712
    if mine_only:
        if scope is None:
            q = q.filter(Group.curator_id.isnot(None))
        elif not scope:
            return []
        else:
            q = q.filter(Group.curator_id.in_(scope))
    groups = q.order_by(Group.name).all()
    if not groups:
        return []

    group_ids = [g.id for g in groups]
    mine = set(scope) if scope else set()
    counts = dict(
        db.query(GroupStudent.group_id, func.count(GroupStudent.id))
        .filter(GroupStudent.group_id.in_(group_ids))
        .group_by(GroupStudent.group_id)
        .all()
    )
    user_ids = {g.curator_id for g in groups if g.curator_id} | {
        g.teacher_id for g in groups if g.teacher_id
    }
    names = {
        u.id: (getattr(u, "official_full_name", None) or u.name or "")
        for u in db.query(UserInDB).filter(UserInDB.id.in_(list(user_ids))).all()
    } if user_ids else {}
    titles = _course_titles_by_group(db, group_ids)

    out: list[GroupItem] = []
    for g in groups:
        is_mine = scope is None or (g.curator_id is not None and int(g.curator_id) in mine)
        out.append(
            GroupItem(
                group_id=g.id,
                name=(g.name or "").strip() or f"Группа #{g.id}",
                curator_id=g.curator_id,
                curator_name=names.get(g.curator_id) if g.curator_id else None,
                # Another curator's roster stays hidden: only the label needed to pick it.
                teacher_id=g.teacher_id if is_mine else None,
                teacher_name=names.get(g.teacher_id) if (is_mine and g.teacher_id) else None,
                program_type=g.program_type,
                group_type=g.group_type,
                is_active=bool(g.is_active),
                is_over=bool(g.is_over),
                student_count=int(counts.get(g.id, 0) or 0) if is_mine else 0,
                course_titles=titles.get(g.id, []),
                is_mine=is_mine,
            )
        )
    return out


class AcademicRequest(BaseModel):
    lms_student_ids: list[int] = Field(default_factory=list)


class NotifyItem(BaseModel):
    """One notification the CRM has decided to send."""

    curator_id: int
    title: str
    content: str
    notification_type: str = "curator_health"
    related_id: Optional[int] = None
    #: Whether an email should also go out. The CRM applies the curator's preferences and the
    #: "critical is not optional" rule before setting this; the LMS just delivers.
    email: bool = False
    email_subject: Optional[str] = None
    email_lines: list[str] = Field(default_factory=list)
    link: Optional[str] = None
    #: Suppresses a repeat of the same (user, type, related_id) inside this many hours.
    within_hours: int = 24


class NotifyRequest(BaseModel):
    items: list[NotifyItem] = Field(default_factory=list)


@router.post("/notify")
def notify_curators(
    body: NotifyRequest,
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> dict[str, Any]:
    """Deliver CRM-composed notifications through the LMS's existing channels.

    Reuses ``notify_once`` and Resend rather than giving the CRM a second notification stack:
    curators already watch the LMS bell, and a school with two independent notification
    systems is a school where half the alerts are missed.

    The CRM decides *what* to send — it owns the rules, the deadlines and the preferences.
    The LMS decides *how*, and applies its own duplicate window as a second line of defence
    on top of the CRM's idempotency keys.

    Email failure never fails the request: an unsent email must not roll back the business
    change that triggered it, and the CRM will try again on its next pass.
    """
    users = {
        u.id: u
        for u in db.query(UserInDB)
        .filter(UserInDB.id.in_([i.curator_id for i in body.items] or [0]))
        .all()
    }
    created = emailed = suppressed = 0
    for item in body.items:
        user = users.get(item.curator_id)
        if user is None:
            continue
        if notify_once(
            db,
            user_id=item.curator_id,
            title=item.title,
            content=item.content,
            notification_type=item.notification_type,
            related_id=item.related_id,
            within_hours=item.within_hours,
        ) is not None:
            created += 1
        # Defence in depth. The CRM clears this flag before calling, but that check lives in
        # another repository behind an HTTP boundary and can be redeployed independently —
        # an older build, a replayed request or any future direct caller would otherwise get
        # through. The in-app notification above is unaffected.
        if item.email and is_curator(db, item.curator_id):
            suppressed += 1
            continue
        if item.email and send_curator_email(
            user.email,
            item.email_subject or item.title,
            item.email_lines or [item.content],
            link=item.link,
        ):
            emailed += 1
    db.commit()
    return {
        "in_app": created,
        "emails": emailed,
        "suppressed": suppressed,
        "suppression_reason": SUPPRESSION_REASON if suppressed else None,
        "requested": len(body.items),
    }


class FreezeTaskItem(BaseModel):
    """One freeze's curator task, as the CRM sees it."""

    crm_freeze_period_id: int
    curator_id: int
    lms_student_id: Optional[int] = None
    #: Almaty civil date the return is planned for; it is the task's deadline.
    due_date: Optional[str] = None
    title: str = "Возвращение из заморозки"
    body: str = ""
    #: ``upsert`` while the freeze is running, ``resumed`` / ``cancelled`` when it ends.
    action: str = "upsert"


class FreezeTaskRequest(BaseModel):
    items: list[FreezeTaskItem] = Field(default_factory=list)


@router.post("/curator-tasks/freeze-return")
def push_freeze_return_tasks(
    body: FreezeTaskRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Create or update the single curator task a freeze produces.

    Idempotent on ``freeze_return:{crm_freeze_period_id}``, which the database enforces —
    the caller is a scheduler that can run in several workers at once and may retry after a
    network failure, so "create unless it exists" decided by the caller would be a race.

    Replaying the whole batch is therefore safe and is the intended recovery: the CRM does
    not have to remember which items it managed to deliver.
    """
    from src.curator.freeze_tasks import close_freeze_return_task, upsert_freeze_return_task

    results: list[dict[str, Any]] = []
    for item in body.items:
        try:
            if item.action in ("resumed", "cancelled"):
                outcome = close_freeze_return_task(
                    db, freeze_period_id=item.crm_freeze_period_id, outcome=item.action
                )
            else:
                due = None
                if item.due_date:
                    try:
                        due = datetime.fromisoformat(item.due_date)
                    except ValueError:
                        raise HTTPException(status_code=400, detail="Invalid due_date")
                outcome = upsert_freeze_return_task(
                    db,
                    freeze_period_id=item.crm_freeze_period_id,
                    curator_id=item.curator_id,
                    student_id=item.lms_student_id,
                    due_date=due,
                    title=item.title,
                    body=item.body,
                )
            results.append({"crm_freeze_period_id": item.crm_freeze_period_id, **outcome})
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 — one bad item must not lose the rest of the batch
            logger.exception("freeze task upsert failed for %s", item.crm_freeze_period_id)
            results.append(
                {"crm_freeze_period_id": item.crm_freeze_period_id, "action": "error"}
            )
    db.commit()
    return {"results": results, "requested": len(body.items)}


class FreezeStateItem(BaseModel):
    lms_student_id: int
    #: The frozen group. Absent — which is what a CRM build predating scoped freezes
    #: sends — means student-wide, i.e. every group, exactly as it always has.
    group_id: Optional[int] = None
    #: Snapshots the CRM takes at freeze time: the membership they were read from is
    #: deleted moments later, so nothing is left to join to. Staff labels; a student
    #: is never shown them.
    scope_group_name: Optional[str] = None
    scope_product: Optional[str] = None
    status: str = "active"
    freeze_start: Optional[str] = None
    planned_resume_date: Optional[str] = None
    actual_resume_date: Optional[str] = None
    reason_code: Optional[str] = None
    responsible_curator_id: Optional[int] = None
    crm_freeze_period_id: Optional[int] = None
    revision: int = 0
    note: Optional[str] = None


class FreezeStateRequest(BaseModel):
    items: list[FreezeStateItem] = Field(default_factory=list)


@router.post("/students/freeze-state")
def push_freeze_state(
    body: FreezeStateRequest,
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> dict[str, Any]:
    """Receive the CRM's freeze decisions. Idempotent, order-tolerant, batched.

    The CRM is canonical for the freeze lifecycle; this only records what it decided so the
    LMS can display and count without asking. ``revision`` makes a replayed or reordered
    delivery converge instead of flapping, which matters because the outbox promises
    at-least-once and nothing more.

    Note what it does not do: freeze never touches LMS access. Platform access is an
    independent policy with its own rules and audit, and conflating the two would silently
    lock a paying student out of the course they can still study.
    """
    from src.curator.freeze_mirror import upsert_freeze_state

    results = [upsert_freeze_state(db, item.model_dump()) for item in body.items]
    db.commit()
    return {
        "applied": sum(1 for r in results if r.get("applied")),
        "skipped": sum(1 for r in results if not r.get("applied")),
        "results": results,
    }


class HealthFactsRequest(BaseModel):
    lms_student_ids: list[int] = Field(default_factory=list)
    window_days: int = 30
    activity_days: int = 7
    homework_window_days: int = 7
    progress_days: int = 7
    #: Supplied by the CRM, which owns the freeze lifecycle. Two shapes are accepted —
    #: see :func:`_normalize_frozen_windows`. Typed loosely because a union that still
    #: round-trips the legacy shape is not expressible as an annotation worth reading.
    frozen_windows: dict[str, Any] = Field(default_factory=dict)


def _normalize_frozen_windows(
    raw: Optional[dict[str, Any]]
) -> dict[int, dict[int, list[tuple[Optional[str], Optional[str]]]]]:
    """One internal shape out of the two the CRM may send.

    * ``{student_id: [[start, end], ...]}`` — the legacy shape, meaning **every group**. It is
      what a CRM build predating scoped freezes sends, and reading it any other way would
      silently stop suppressing anything for those payloads halfway through the rollout.
    * ``{student_id: {group_id: [[start, end], ...]}}`` — one entry per frozen enrollment, so
      a SAT freeze leaves the student's IELTS denominators exactly as they were.

    Both collapse to the scoped shape, with group ``0`` carrying the student-wide meaning.
    """
    from src.curator.freeze_mirror import GROUP_WIDE

    out: dict[int, dict[int, list[tuple[Optional[str], Optional[str]]]]] = {}
    for sid, value in (raw or {}).items():
        if not str(sid).lstrip("-").isdigit():
            continue
        per_group = value if isinstance(value, dict) else {GROUP_WIDE: value}
        scoped: dict[int, list[tuple[Optional[str], Optional[str]]]] = {}
        for gid, spans in (per_group or {}).items():
            if not str(gid).lstrip("-").isdigit():
                continue
            scoped[int(gid)] = [
                (span[0] if span else None, span[1] if span and len(span) > 1 else None)
                for span in (spans or [])
            ]
        if scoped:
            out[int(sid)] = scoped
    return out


@router.post("/students/health-facts")
def students_health_facts(
    body: HealthFactsRequest,
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> dict[str, Any]:
    """Counts and dates the CRM's health rules read. No thresholds, no severity.

    The split is the point: the LMS knows who attended and who submitted, the CRM decides
    what that means and whose problem it is. Putting a threshold here would mean changing a
    setting required an LMS deploy, and would give the two services two opinions about when
    a student is at risk.

    Unscoped by curator on purpose — unlike ``/students/academic``, which projects per
    curator for display. This feeds an engine that must evaluate a student *as a whole* to
    decide severity; the per-curator projection happens in the CRM when a case is shown.
    Access is already limited to the service key.
    """
    from src.curator.health_facts import health_facts

    return health_facts(
        db,
        body.lms_student_ids,
        window_days=body.window_days,
        activity_days=body.activity_days,
        homework_window_days=body.homework_window_days,
        progress_days=body.progress_days,
        frozen_windows=_normalize_frozen_windows(body.frozen_windows),
    )


@router.post("/students/academic")
def students_academic(
    body: AcademicRequest,
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> dict[str, Any]:
    """Academic projection for a batch of students, scoped to what the actor may see.

    The *same* student yields a different projection per curator: each sees only the groups
    they own. That is the "shared student" rule, implemented once, here.
    """
    ids = [int(i) for i in (body.lms_student_ids or [])][:5000]
    if not ids:
        return {"students": {}}

    scope = _scope_for(db, actor)
    if scope is not None and not scope:
        return {"students": {}}

    gq = (
        db.query(
            GroupStudent.student_id,
            Group.id,
            Group.name,
            Group.program_type,
            Group.curator_id,
            GroupStudent.created_at,
            Group.is_active,
            Group.is_over,
        )
        .join(Group, Group.id == GroupStudent.group_id)
        .filter(GroupStudent.student_id.in_(ids))
    )
    rows = gq.all()

    users = {
        u.id: u for u in db.query(UserInDB).filter(UserInDB.id.in_(ids)).all()
    }

    out: dict[str, Any] = {}
    for sid in ids:
        out[str(sid)] = {
            "lms_student_id": sid,
            "groups": [],
            "products": [],
            "product_count": 0,
            "learning_start_date": None,
            "last_activity_date": None,
            "is_active": None,
        }
    for sid, gid, gname, program, curator_id, joined, g_active, g_over in rows:
        entry = out.get(str(int(sid)))
        if entry is None:
            continue
        active_group = bool(g_active) and not bool(g_over)
        # Learning start spans every membership ever recorded, including groups that have
        # since completed — it is when the student started studying, not when they joined
        # the group they are in today.
        joined_date = joined.date() if isinstance(joined, datetime) else joined
        if joined_date is not None:
            current = entry["learning_start_date"]
            iso = joined_date.isoformat()
            if current is None or iso < current:
                entry["learning_start_date"] = iso
        if not active_group:
            continue
        visible = scope is None or (curator_id is not None and int(curator_id) in scope)
        if visible:
            entry["groups"].append(
                {
                    "group_id": int(gid),
                    "name": (gname or "").strip() or f"Группа #{gid}",
                    "program_type": program,
                    "curator_id": curator_id,
                    "joined_at": joined_date.isoformat() if joined_date else None,
                }
            )
        # Products are commercial information and stay visible even when the group behind
        # them does not — a curator may learn the student also studies IELTS, but not with
        # whom or in which group.
        product = (program or "").strip()
        if product and product not in entry["products"]:
            entry["products"].append(product)

    for sid in ids:
        entry = out[str(sid)]
        entry["product_count"] = len(entry["products"])
        user = users.get(sid)
        if user is not None:
            entry["last_activity_date"] = (
                user.last_activity_date.isoformat() if user.last_activity_date else None
            )
            entry["is_active"] = bool(user.is_active)
            entry["name"] = getattr(user, "official_full_name", None) or user.name
            entry["email"] = user.email
    return {"students": out}


# --- reconciliation & notifications -------------------------------------------------------


@router.post("/reconcile")
def post_reconcile(
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> dict[str, int]:
    if not actor.is_head:
        raise HTTPException(status_code=403, detail="Требуется роль старшего куратора")
    return reconcile_onboarding(db, actor)


class ReconcileStudentBody(BaseModel):
    lms_student_id: int
    notify: bool = True
    source_group_name: Optional[str] = None
    destination_group_name: Optional[str] = None


@router.post("/reconcile-student")
def post_reconcile_student(
    body: ReconcileStudentBody,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> dict[str, Any]:
    """Re-derive one student's cards after a membership change, and notify.

    Called by the CRM immediately after an add/transfer so the board is correct without
    waiting for the hourly sweep. Notification failures never affect the result: emails go
    out on a background task after the reconcile has already committed.
    """
    sid = int(body.lms_student_id)
    before_open = {
        int(r.curator_id): r
        for r in db.query(CuratorOnboarding)
        .filter(
            CuratorOnboarding.student_id == sid,
            CuratorOnboarding.ended_at.is_(None),
        )
        .all()
    }
    result = reconcile_student(db, sid, actor)

    after_rows = (
        db.query(CuratorOnboarding)
        .filter(
            CuratorOnboarding.student_id == sid,
            CuratorOnboarding.ended_at.is_(None),
        )
        .all()
    )
    after_open = {int(r.curator_id): r for r in after_rows}

    emails: list[tuple[str, str, list[str], str]] = []
    if body.notify:
        student = db.query(UserInDB).filter(UserInDB.id == sid).first()
        gained = set(after_open) - set(before_open)
        lost = set(before_open) - set(after_open)
        if student is not None and (gained or lost):
            curators = {
                u.id: u
                for u in db.query(UserInDB)
                .filter(UserInDB.id.in_(list(gained | lost)))
                .all()
            }
            for cid in gained:
                curator = curators.get(cid)
                if curator is not None:
                    emails += notify_student_assigned(
                        db,
                        curator,
                        student,
                        body.destination_group_name,
                        after_open[cid].id,
                        reason="transfer" if lost else "assigned",
                    )
            for cid in lost:
                curator = curators.get(cid)
                if curator is not None:
                    emails += notify_responsibility_ended(
                        db, curator, student, body.source_group_name
                    )
            db.commit()

    if emails:
        background_tasks.add_task(dispatch_emails, emails, "curator_transfer")
    return {**result, "notified": len(emails)}


class UnassignedAlertBody(BaseModel):
    group_ids: list[int] = Field(default_factory=list)
    reason: Optional[str] = None


@router.post("/alerts/unassigned-groups")
def alert_unassigned_groups(
    body: UnassignedAlertBody,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> dict[str, int]:
    """Escalate groups left without a curator to every head curator."""
    heads = head_curators(db)
    if not heads or not body.group_ids:
        return {"notified": 0}
    names = [
        (g.name or f"Группа #{g.id}")
        for g in db.query(Group).filter(Group.id.in_(body.group_ids[:50])).all()
    ]
    content = "Группы без куратора: " + ", ".join(names[:20])
    if body.reason:
        content += f". Причина: {body.reason}"
    emails = notify_heads(
        db,
        heads,
        "Группы без куратора",
        content,
        TYPE_UNASSIGNED_GROUP,
        related_id=body.group_ids[0] if body.group_ids else None,
        link=crm_onboarding_url(),
    )
    db.commit()
    if emails:
        background_tasks.add_task(dispatch_emails, emails, "unassigned_groups")
    return {"notified": len(emails)}


@router.post("/alerts/overdue-sweep")
def overdue_sweep(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> dict[str, int]:
    """Notify curators about their overdue cards, deduplicated to once per card per day."""
    if not actor.is_head:
        raise HTTPException(status_code=403, detail="Требуется роль старшего куратора")
    rows = load_board(db)
    now = _utcnow()
    overdue = [r for r in rows if is_overdue(r, now)]
    if not overdue:
        return {"cards": 0, "notified": 0}

    curators = {
        u.id: u
        for u in db.query(UserInDB)
        .filter(UserInDB.id.in_([r.curator_id for r in overdue]))
        .all()
    }
    from src.curator.notifications import notify_once

    emails: list[tuple[str, str, list[str], str]] = []
    for row in overdue:
        curator = curators.get(int(row.curator_id))
        if curator is None or not curator.is_active:
            continue
        student_name = (
            (row.student.official_full_name or row.student.name) if row.student else "студент"
        )
        title = "Просроченный онбординг"
        content = f"{student_name}: карточка в статусе «{row.status}» просрочена."
        if notify_once(
            db, curator.id, title, content, TYPE_ONBOARDING_OVERDUE, related_id=row.id
        ) is not None:
            emails.append(
                ((curator.email or ""), title, [content], crm_onboarding_url(row.id))
            )
    heads = head_curators(db)
    if heads and overdue:
        emails += notify_heads(
            db,
            heads,
            "Просроченные онбординги",
            f"Просроченных карточек: {len(overdue)}.",
            TYPE_ONBOARDING_OVERDUE,
            related_id=None,
            link=crm_onboarding_url(),
        )
    db.commit()
    if emails:
        background_tasks.add_task(dispatch_emails, emails, "overdue_sweep")
    return {"cards": len(overdue), "notified": len(emails)}


# --- group curator assignment (head only) -------------------------------------------------


class AssignCuratorBody(BaseModel):
    curator_id: Optional[int] = None


@router.put("/groups/{group_id}/curator")
def assign_group_curator(
    group_id: int,
    body: AssignCuratorBody,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: OnboardingActor = Depends(resolve_actor),
) -> dict[str, Any]:
    """Assign/reassign a group's curator, then re-derive the affected onboarding cycles.

    Head curators may move any group between curators; that is assignment management, not
    structural editing of the group, which stays out of their reach.
    """
    if not actor.is_head:
        raise HTTPException(status_code=403, detail="Требуется роль старшего куратора")
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Группа не найдена")

    if body.curator_id is not None:
        curator = (
            db.query(UserInDB)
            .filter(UserInDB.id == body.curator_id, UserInDB.role.in_(CURATOR_ROLES))
            .first()
        )
        if curator is None:
            raise HTTPException(status_code=400, detail="Куратор не найден")
        if not curator.is_active:
            raise HTTPException(status_code=400, detail="Куратор неактивен")

    previous = group.curator_id
    group.curator_id = body.curator_id
    db.commit()

    student_ids = [
        int(r[0])
        for r in db.query(GroupStudent.student_id)
        .filter(GroupStudent.group_id == group_id)
        .all()
    ]
    for sid in student_ids:
        reconcile_student(db, sid, actor, commit=False)
    db.commit()

    if body.curator_id is None and student_ids:
        heads = head_curators(db)
        emails = notify_heads(
            db,
            heads,
            "Группа осталась без куратора",
            f"«{group.name or group_id}»: куратор снят, студентов: {len(student_ids)}.",
            TYPE_UNASSIGNED_GROUP,
            related_id=group_id,
            link=crm_onboarding_url(),
        )
        db.commit()
        if emails:
            background_tasks.add_task(dispatch_emails, emails, "curator_removed")

    return {
        "group_id": group_id,
        "previous_curator_id": previous,
        "curator_id": body.curator_id,
        "students_reconciled": len(student_ids),
    }

"""Canonical curator-onboarding domain service.

Every writer goes through here — the legacy LMS kanban route, the CRM workspace (via
``/internal/crm/curator/*``) and the background reconciler — so there is exactly one place
that knows what an onboarding cycle is and when it may change. The CRM composes its own
financial data around what this module returns; it never re-implements the rules.

Two things this module owns that nothing else may duplicate:

**The cycle invariant.** At most one *open* row (``ended_at IS NULL``) per (curator,
student). Closed rows are history and are never revived — a student returning to a previous
curator gets a new row with the next ``cycle_no``.

**The thresholds.** "Overdue" is a business rule, not a UI opinion, so the numbers live in
:data:`ONBOARDING_THRESHOLDS` and are served to the frontends over the API rather than being
re-typed into a component.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from src.schemas.models import (
    CuratorOnboarding,
    CuratorOnboardingEvent,
    CuratorOnboardingNote,
    Group,
    GroupStudent,
    UserInDB,
)

logger = logging.getLogger(__name__)

# --- statuses -----------------------------------------------------------------------------

STATUS_NEW = "new"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

#: Statuses that mean "someone still has to do something".
ACTIVE_STATUSES: tuple[str, ...] = (STATUS_NEW, STATUS_IN_PROGRESS)
#: Columns of the board. ``cancelled`` is history and is hidden from the active board.
BOARD_STATUSES: tuple[str, ...] = (STATUS_NEW, STATUS_IN_PROGRESS, STATUS_DONE)
#: What a human may set a card to.
SETTABLE_STATUSES: tuple[str, ...] = (STATUS_NEW, STATUS_IN_PROGRESS, STATUS_DONE)

# --- end reasons --------------------------------------------------------------------------

END_RELATIONSHIP_ENDED = "relationship_ended"
END_TRANSFERRED_OUT = "transferred_out"
END_CURATOR_DEACTIVATED = "curator_deactivated"
END_LEGACY_CANCELLED = "legacy_cancelled"
END_MANUAL = "manual"

# --- thresholds ---------------------------------------------------------------------------
#
# Centralised on purpose: these five numbers decide what the dashboards count, what the
# board badges, and what the overdue digest emails. Scattering them through UI components is
# how "overdue" starts meaning three different things on three screens.

#: A card sitting in ``new`` for longer than this many calendar days is overdue.
NEW_OVERDUE_DAYS = 2
#: A card in ``in_progress`` with no status movement for this many days is overdue.
IN_PROGRESS_STALE_DAYS = 5
#: No LMS activity for this many days raises the inactivity warning.
LMS_INACTIVITY_DAYS = 7
#: A product whose expected end falls within this many days is "ending soon".
PRODUCT_ENDING_SOON_DAYS = 15
#: A student studying at least this many distinct active products is flagged as multi-product.
MULTI_PRODUCT_MIN = 2

ONBOARDING_THRESHOLDS: dict[str, int] = {
    "new_overdue_days": NEW_OVERDUE_DAYS,
    "in_progress_stale_days": IN_PROGRESS_STALE_DAYS,
    "lms_inactivity_days": LMS_INACTIVITY_DAYS,
    "product_ending_soon_days": PRODUCT_ENDING_SOON_DAYS,
    "multi_product_min": MULTI_PRODUCT_MIN,
}


def _utcnow() -> datetime:
    """Naive UTC, matching the columns (which are all ``DateTime`` without timezone)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Compare-safe: rows written before/after the tz-aware default are mixed in the wild."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


# --- actor --------------------------------------------------------------------------------


@dataclass(frozen=True)
class OnboardingActor:
    """Who is making a change, in terms this module can record and authorise.

    Deliberately not a ``UserInDB``: the CRM calls in over the internal API with an identity
    it resolved itself, and the legacy LMS route calls in with a session user. Both collapse
    to (id, name, role), which is all the domain needs.
    """

    user_id: Optional[int]
    name: str = ""
    role: str = ""

    @property
    def is_head(self) -> bool:
        return self.role in ("head_curator", "admin")

    @classmethod
    def from_user(cls, user: UserInDB) -> "OnboardingActor":
        return cls(
            user_id=getattr(user, "id", None),
            name=(getattr(user, "official_full_name", None) or getattr(user, "name", "") or ""),
            role=(getattr(user, "role", "") or ""),
        )

    @classmethod
    def system(cls, name: str = "система") -> "OnboardingActor":
        return cls(user_id=None, name=name, role="system")


class OnboardingPermissionError(PermissionError):
    """The actor may not touch this card. Mapped to 403 by the route layer."""


class OnboardingNotFound(LookupError):
    """No such card, or none the actor is allowed to know exists. Mapped to 404."""


# --- history ------------------------------------------------------------------------------


def record_event(
    db: Session,
    onboarding: CuratorOnboarding,
    actor: OnboardingActor,
    action: str,
    before: Optional[dict[str, Any]] = None,
    after: Optional[dict[str, Any]] = None,
) -> CuratorOnboardingEvent:
    """Append one history row. Caller commits — the event and the change it describes must
    land in the same transaction, or the log can disagree with the data."""
    event = CuratorOnboardingEvent(
        onboarding_id=onboarding.id,
        actor_id=actor.user_id,
        actor_name=actor.name or None,
        actor_role=actor.role or None,
        action=action,
        before=before,
        after=after,
        created_at=_utcnow(),
    )
    db.add(event)
    return event


# --- cycle management ---------------------------------------------------------------------


def open_cycle(
    db: Session,
    curator_id: int,
    student_id: int,
    group_id: Optional[int],
    actor: Optional[OnboardingActor] = None,
) -> Optional[CuratorOnboarding]:
    """Start a fresh onboarding cycle, or return None if one is already open.

    Idempotent and concurrency-safe: the open-cycle uniqueness is enforced by a partial
    unique index, so two racing reconcilers cannot both create a card — the loser catches
    IntegrityError and finds the winner's row. Callers must be able to tolerate a
    ``SAVEPOINT`` here, which every caller in this repo can (they all run inside a session,
    not a raw connection).
    """
    actor = actor or OnboardingActor.system()
    existing = active_cycle(db, curator_id, student_id)
    if existing is not None:
        return None

    next_no = (
        db.query(func.coalesce(func.max(CuratorOnboarding.cycle_no), 0))
        .filter(
            CuratorOnboarding.curator_id == curator_id,
            CuratorOnboarding.student_id == student_id,
        )
        .scalar()
        or 0
    ) + 1

    now = _utcnow()
    row = CuratorOnboarding(
        curator_id=curator_id,
        student_id=student_id,
        group_id=group_id,
        status=STATUS_NEW,
        cycle_no=next_no,
        created_at=now,
        updated_at=now,
        status_changed_at=now,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        # Another writer opened the cycle between our check and our insert. Theirs is as
        # good as ours; returning None means "nothing new here", which is the truth.
        logger.info(
            "onboarding cycle already opened concurrently curator=%s student=%s",
            curator_id,
            student_id,
        )
        return None

    record_event(
        db,
        row,
        actor,
        "cycle.opened",
        after={"cycle_no": next_no, "group_id": group_id, "status": STATUS_NEW},
    )
    return row


def close_cycle(
    db: Session,
    row: CuratorOnboarding,
    reason: str = END_RELATIONSHIP_ENDED,
    actor: Optional[OnboardingActor] = None,
) -> bool:
    """Close an open cycle. Returns False when it was already closed (idempotent).

    A cycle that reached ``done`` keeps that status — the onboarding genuinely finished and
    the history must say so. One still in flight becomes ``cancelled``, which is what the
    original reconciler did and what every existing report expects to see.
    """
    if row.ended_at is not None:
        return False
    actor = actor or OnboardingActor.system()
    before = {"status": row.status, "ended_at": None}
    now = _utcnow()
    row.ended_at = now
    row.end_reason = reason
    if row.status in ACTIVE_STATUSES:
        row.status = STATUS_CANCELLED
        row.status_changed_at = now
    row.updated_at = now
    record_event(
        db,
        row,
        actor,
        "cycle.closed",
        before=before,
        after={"status": row.status, "ended_at": now.isoformat(), "end_reason": reason},
    )
    return True


def active_cycle(db: Session, curator_id: int, student_id: int) -> Optional[CuratorOnboarding]:
    """The one open cycle for a pair, if any."""
    return (
        db.query(CuratorOnboarding)
        .filter(
            CuratorOnboarding.curator_id == curator_id,
            CuratorOnboarding.student_id == student_id,
            CuratorOnboarding.ended_at.is_(None),
        )
        .order_by(CuratorOnboarding.cycle_no.desc())
        .first()
    )


# --- reconciler ---------------------------------------------------------------------------


def compute_active_pairs(db: Session) -> dict[tuple[int, int], int]:
    """``{(curator_id, student_id): group_id}`` for live curator↔student relationships.

    Live means: an active, not-over group that has a curator, containing an active student.
    ``is_over`` is honoured because a completed group is no longer anyone's responsibility —
    the original query checked only ``is_active``, so curators kept cards for cohorts that
    had finished months ago.

    When a student is in several groups owned by the same curator the most recently joined
    one wins as the card's *display* group; the relationship itself is the same either way.
    """
    rows = (
        db.query(
            Group.curator_id,
            GroupStudent.student_id,
            GroupStudent.group_id,
            GroupStudent.created_at,
        )
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .join(UserInDB, UserInDB.id == GroupStudent.student_id)
        .filter(
            Group.is_active == True,  # noqa: E712 - SQLAlchemy needs the comparison
            or_(Group.is_over == False, Group.is_over.is_(None)),  # noqa: E712
            Group.curator_id.isnot(None),
            UserInDB.is_active == True,  # noqa: E712
        )
        .all()
    )
    pairs: dict[tuple[int, int], int] = {}
    seen_created: dict[tuple[int, int], datetime] = {}
    for curator_id, student_id, group_id, created_at in rows:
        key = (int(curator_id), int(student_id))
        ts = _as_naive_utc(created_at) or datetime.min
        if key not in pairs or ts > seen_created[key]:
            pairs[key] = group_id
            seen_created[key] = ts
    return pairs


def reconcile_onboarding(db: Session, actor: Optional[OnboardingActor] = None) -> dict[str, int]:
    """Bring cards in line with live relationships. Idempotent; safe to run concurrently.

    * relationship appears and no cycle is open → open one (``new``)
    * relationship persists → refresh the display group only
    * relationship disappears → close the open cycle
    * relationship reappears later → a *new* cycle, never a revived historical row
    """
    actor = actor or OnboardingActor.system("реконсилятор")
    active = compute_active_pairs(db)

    open_rows: dict[tuple[int, int], CuratorOnboarding] = {}
    for row in db.query(CuratorOnboarding).filter(CuratorOnboarding.ended_at.is_(None)).all():
        open_rows[(int(row.curator_id), int(row.student_id))] = row

    created = closed = regrouped = 0

    for key, group_id in active.items():
        row = open_rows.get(key)
        if row is None:
            if open_cycle(db, key[0], key[1], group_id, actor) is not None:
                created += 1
        elif row.group_id != group_id:
            row.group_id = group_id  # keep the displayed group fresh
            regrouped += 1

    for key, row in open_rows.items():
        if key not in active:
            if close_cycle(db, row, END_RELATIONSHIP_ENDED, actor):
                closed += 1

    db.commit()
    return {"created": created, "closed": closed, "regrouped": regrouped}


def reconcile_student(
    db: Session,
    student_id: int,
    actor: Optional[OnboardingActor] = None,
    commit: bool = True,
) -> dict[str, int]:
    """Reconcile one student's cards only — the cheap path after a group add/transfer.

    Same rules as the full sweep, restricted to a single student so a transfer does not have
    to wait for (or pay for) the hourly organisation-wide pass.
    """
    actor = actor or OnboardingActor.system()
    all_pairs = compute_active_pairs(db)
    active = {k: v for k, v in all_pairs.items() if k[1] == int(student_id)}

    open_rows = {
        (int(r.curator_id), int(r.student_id)): r
        for r in db.query(CuratorOnboarding)
        .filter(
            CuratorOnboarding.student_id == int(student_id),
            CuratorOnboarding.ended_at.is_(None),
        )
        .all()
    }

    created = closed = 0
    for key, group_id in active.items():
        if key not in open_rows:
            if open_cycle(db, key[0], key[1], group_id, actor) is not None:
                created += 1
        elif open_rows[key].group_id != group_id:
            open_rows[key].group_id = group_id

    for key, row in open_rows.items():
        if key not in active:
            if close_cycle(db, row, END_TRANSFERRED_OUT, actor):
                closed += 1

    if commit:
        db.commit()
    return {"created": created, "closed": closed}


# --- overdue ------------------------------------------------------------------------------


def is_overdue(row: CuratorOnboarding, now: Optional[datetime] = None) -> bool:
    """Has this card breached its threshold? ``done``/closed cards never are."""
    if row.ended_at is not None or row.status not in ACTIVE_STATUSES:
        return False
    now = now or _utcnow()
    if row.status == STATUS_NEW:
        anchor = _as_naive_utc(row.created_at)
        limit = NEW_OVERDUE_DAYS
    else:
        anchor = _as_naive_utc(row.status_changed_at) or _as_naive_utc(row.updated_at)
        limit = IN_PROGRESS_STALE_DAYS
    if anchor is None:
        return False
    return (now - anchor) > timedelta(days=limit)


def onboarding_age_days(row: CuratorOnboarding, now: Optional[datetime] = None) -> int:
    now = now or _utcnow()
    created = _as_naive_utc(row.created_at)
    if created is None:
        return 0
    return max(0, (now - created).days)


# --- mutations ----------------------------------------------------------------------------


def _assert_may_edit(row: CuratorOnboarding, actor: OnboardingActor) -> None:
    """A card is editable by the curator who owns it, or by any head/admin.

    Head-curator edits are *interventions*: they are attributed to the head in the history
    but leave ``curator_id`` alone, because oversight is not a transfer of ownership.
    """
    if actor.is_head:
        return
    if actor.user_id is not None and int(row.curator_id) == int(actor.user_id):
        return
    raise OnboardingPermissionError("Карточка принадлежит другому куратору")


def get_card(db: Session, onboarding_id: int, actor: OnboardingActor) -> CuratorOnboarding:
    row = (
        db.query(CuratorOnboarding)
        .filter(CuratorOnboarding.id == onboarding_id)
        .first()
    )
    if row is None:
        raise OnboardingNotFound("Карточка не найдена")
    try:
        _assert_may_edit(row, actor)
    except OnboardingPermissionError:
        # Do not leak that a card with this id exists but belongs to someone else.
        raise OnboardingNotFound("Карточка не найдена") from None
    return row


def set_status(
    db: Session,
    row: CuratorOnboarding,
    status: str,
    actor: OnboardingActor,
    commit: bool = True,
) -> CuratorOnboarding:
    if status not in SETTABLE_STATUSES:
        raise ValueError(f"Недопустимый статус: {status}")
    _assert_may_edit(row, actor)
    if row.ended_at is not None:
        raise OnboardingPermissionError("Цикл закрыт — изменение статуса невозможно")

    before = {"status": row.status}
    now = _utcnow()
    row.status = status
    row.status_changed_at = now
    row.updated_at = now
    if status == STATUS_DONE:
        row.completed_at = now
        row.completed_by = actor.user_id
    else:
        row.completed_at = None
        row.completed_by = None

    intervention = (
        actor.is_head and actor.user_id is not None and int(actor.user_id) != int(row.curator_id)
    )
    record_event(
        db,
        row,
        actor,
        "intervention" if intervention else "status.changed",
        before=before,
        after={"status": status},
    )
    if commit:
        db.commit()
        db.refresh(row)
    return row


def set_next_action(
    db: Session,
    row: CuratorOnboarding,
    next_action_at: Optional[date],
    note: Optional[str],
    actor: OnboardingActor,
    commit: bool = True,
) -> CuratorOnboarding:
    _assert_may_edit(row, actor)
    before = {
        "next_action_at": row.next_action_at.isoformat() if row.next_action_at else None,
        "next_action_note": row.next_action_note,
    }
    row.next_action_at = next_action_at
    row.next_action_note = (note or "").strip()[:500] or None
    row.updated_at = _utcnow()
    record_event(
        db,
        row,
        actor,
        "next_action.set",
        before=before,
        after={
            "next_action_at": next_action_at.isoformat() if next_action_at else None,
            "next_action_note": row.next_action_note,
        },
    )
    if commit:
        db.commit()
        db.refresh(row)
    return row


def add_note(
    db: Session,
    row: CuratorOnboarding,
    body: str,
    actor: OnboardingActor,
    commit: bool = True,
) -> CuratorOnboardingNote:
    _assert_may_edit(row, actor)
    text = (body or "").strip()
    if not text:
        raise ValueError("Заметка не может быть пустой")
    note = CuratorOnboardingNote(
        onboarding_id=row.id,
        author_id=actor.user_id,
        author_name=actor.name or None,
        author_role=actor.role or None,
        body=text,
        created_at=_utcnow(),
    )
    db.add(note)
    row.updated_at = _utcnow()
    record_event(db, row, actor, "note.added", after={"length": len(text)})
    if commit:
        db.commit()
        db.refresh(note)
    return note


# --- reads --------------------------------------------------------------------------------


def board_query(
    db: Session,
    curator_ids: Optional[Sequence[int]] = None,
    statuses: Optional[Sequence[str]] = None,
    include_closed: bool = False,
    include_baseline: bool = False,
):
    """Base query for the board, with the launch-baseline rule applied.

    The launch backfill seeded every pre-existing pair as ``done`` with no human actioner so
    the board would start clean. Those synthetic rows must stay hidden (they are not
    achievements anyone made) while genuinely completed cards — which always have
    ``completed_by`` — still show in Завершено.
    """
    q = db.query(CuratorOnboarding)
    if not include_closed:
        q = q.filter(CuratorOnboarding.ended_at.is_(None))
    q = q.filter(CuratorOnboarding.status.in_(list(statuses or BOARD_STATUSES)))
    if not include_baseline:
        q = q.filter(
            ~(
                (CuratorOnboarding.status == STATUS_DONE)
                & (CuratorOnboarding.completed_by.is_(None))
            )
        )
    if curator_ids is not None:
        ids = [int(c) for c in curator_ids]
        if not ids:
            # An empty explicit scope means "nothing", not "everything".
            return q.filter(CuratorOnboarding.id == -1)
        q = q.filter(CuratorOnboarding.curator_id.in_(ids))
    return q


def load_board(
    db: Session,
    curator_ids: Optional[Sequence[int]] = None,
    statuses: Optional[Sequence[str]] = None,
    student_ids: Optional[Sequence[int]] = None,
    include_closed: bool = False,
) -> list[CuratorOnboarding]:
    q = board_query(db, curator_ids=curator_ids, statuses=statuses, include_closed=include_closed)
    if student_ids is not None:
        ids = [int(s) for s in student_ids]
        if not ids:
            return []
        q = q.filter(CuratorOnboarding.student_id.in_(ids))
    return (
        q.options(
            joinedload(CuratorOnboarding.student),
            joinedload(CuratorOnboarding.group),
            joinedload(CuratorOnboarding.curator),
        )
        .order_by(CuratorOnboarding.created_at.desc(), CuratorOnboarding.id.desc())
        .all()
    )


def status_counts(
    db: Session, curator_ids: Optional[Sequence[int]] = None
) -> dict[str, int]:
    """Per-status card counts for the dashboards, plus the derived ``overdue``."""
    rows = board_query(db, curator_ids=curator_ids).all()
    now = _utcnow()
    out = {s: 0 for s in BOARD_STATUSES}
    out["overdue"] = 0
    for row in rows:
        if row.status in out:
            out[row.status] += 1
        if is_overdue(row, now):
            out["overdue"] += 1
    return out


def curator_student_ids(db: Session, curator_ids: Sequence[int]) -> set[int]:
    """Students visible to these curators: everyone in their active, non-completed groups.

    This is the authorization primitive for the whole curator workspace — the CRM asks this
    question before it will show a student card, and the answer must not depend on whether an
    onboarding row happens to exist.
    """
    ids = [int(c) for c in curator_ids]
    if not ids:
        return set()
    rows = (
        db.query(GroupStudent.student_id)
        .join(Group, Group.id == GroupStudent.group_id)
        .filter(
            Group.curator_id.in_(ids),
            Group.is_active == True,  # noqa: E712
            or_(Group.is_over == False, Group.is_over.is_(None)),  # noqa: E712
        )
        .distinct()
        .all()
    )
    return {int(r[0]) for r in rows}


def serialize_card(
    row: CuratorOnboarding,
    now: Optional[datetime] = None,
    include_notes: bool = False,
    include_history: bool = False,
) -> dict[str, Any]:
    """Wire shape shared by the LMS kanban and the CRM workspace."""
    now = now or _utcnow()
    student = row.student
    data: dict[str, Any] = {
        "id": row.id,
        "cycle_no": row.cycle_no or 1,
        "student_id": row.student_id,
        "student_name": (
            (student.official_full_name or student.name) if student else ""
        ),
        "student_email": (getattr(student, "email", None) if student else None),
        "group_id": row.group_id,
        "group_name": row.group.name if row.group else None,
        "curator_id": row.curator_id,
        "curator_name": (row.curator.name if row.curator else None),
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "status_changed_at": (
            row.status_changed_at.isoformat() if row.status_changed_at else None
        ),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "completed_by": row.completed_by,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "end_reason": row.end_reason,
        "next_action_at": row.next_action_at.isoformat() if row.next_action_at else None,
        "next_action_note": row.next_action_note,
        "age_days": onboarding_age_days(row, now),
        "is_overdue": is_overdue(row, now),
        "last_activity_date": (
            student.last_activity_date.isoformat()
            if student is not None and getattr(student, "last_activity_date", None)
            else None
        ),
    }
    if include_notes:
        data["notes"] = [
            {
                "id": n.id,
                "body": n.body,
                "author_id": n.author_id,
                "author_name": n.author_name,
                "author_role": n.author_role,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in (row.notes or [])
        ]
    if include_history:
        data["history"] = [
            {
                "id": e.id,
                "action": e.action,
                "actor_id": e.actor_id,
                "actor_name": e.actor_name,
                "actor_role": e.actor_role,
                "before": e.before,
                "after": e.after,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in (row.events or [])
        ]
    return data


def telegram_link(tg: Optional[str]) -> Optional[str]:
    """A t.me URL for a handle, or None for a numeric chat id / empty value."""
    if not tg:
        return None
    handle = tg.strip().lstrip("@")
    if not handle or handle.isdigit():
        return None
    return f"https://t.me/{handle}"

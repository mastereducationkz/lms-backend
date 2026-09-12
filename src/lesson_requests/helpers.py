"""Shared helpers for lesson request routes and CRM internal API."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.schemas.models import (
    UserInDB,
    Group,
    LessonSchedule,
    Event,
    EventGroup,
    LessonRequest,
    LessonRequestSchema,
    Notification,
    GroupStudent,
)
from src.services.event_service import EventService
from src.services.attendance_service import AttendanceService
from src.services.email_service import send_lesson_change_curator_notification
from src.lesson_requests.services import (
    ADD_REPLACEMENT,
    CANCEL_ONLY,
    resolve_head_teachers_for_group,
)

logger = logging.getLogger(__name__)

#: The business runs on Asia/Almaty, a fixed UTC+5; events are stored as naive UTC. Same
#: constant the schedule generator uses (``leaderboard.generate_schedule``).
KZ_OFFSET = timedelta(hours=5)

#: How far past the anchor ``append_replacement_lesson`` looks for a free slot before it
#: gives up. A group meets at least weekly, so eight weeks means the schedule is broken.
_REPLACEMENT_SEARCH_WEEKS = 8

#: Everything from ": Lesson" to the end of an auto-generated title. Titles without it are
#: custom and are never renumbered — the same rule as the CRM's ``retitle_group_lessons``.
_LESSON_SUFFIX = re.compile(r": Lesson\b.*$")


#: Said to a teacher when an approved request and the live schedule disagree. Staff get the
#: same flag rendered as a red warning with the ids; a teacher gets a sentence.
_NOT_APPLIED_NOTE = (
    "Замена одобрена, но в расписании урок пока закреплён за другим педагогом. "
    "Администратор уведомлён."
)

def taught_marks_count(db: Session, event_id: Optional[int]) -> int:
    """How many real attendance marks this lesson already carries.

    Classified through :mod:`src.services.attendance_status`, which is the single source of
    truth for what "marked" means and says in so many words not to re-inline its tuples. It
    matters here beyond tidiness: the marked set includes legacy and imported spellings
    (``missed``, ``presented``, ``1``/``0``) that a hand-written ``present/late/absent`` list
    would miss, and a missed mark is a lesson this guard would wave through.
    """
    if not event_id:
        return 0
    from sqlalchemy import func

    from src.schemas.models import Attendance
    from src.services.attendance_status import marked_statuses_for_sql

    return (
        db.query(Attendance)
        .filter(
            Attendance.event_id == event_id,
            func.lower(func.coalesce(Attendance.status, "")).in_(marked_statuses_for_sql()),
        )
        .count()
    )


def assert_reschedule_is_not_rewriting_history(
    db: Session, event_id: Optional[int]
) -> None:
    """Refuse to move a lesson that has already been taught and marked.

    A reschedule rewrites ``events.start_datetime`` in place, and attendance rows point at
    the *event*, not at a date — which is deliberate, so that moving next Monday's lesson to
    Friday keeps its history attached. Applied to a lesson that already happened, the same
    mechanism carries a teacher's marks forward onto a date the class was never in the room.

    Production did exactly this: two lessons of «June 9 SAT - Ayanat» taught and marked on 27
    and 29 July were rescheduled on 22 August to 1 and 3 September, and twenty students'
    marks moved with them. The cards then showed attendance filled in for lessons that had
    not happened yet, which is unreadable as anything but a bug — and it was not a marking
    bug at all. Five groups carried the same damage.

    Past-but-unmarked lessons stay reschedulable on purpose: «мы не провели понедельник,
    перенесём на пятницу» is a real and common request, and refusing it would break a
    working flow to fix a different problem. The line is the mark, not the date.
    """
    marks = taught_marks_count(db, event_id)
    if marks == 0:
        return
    event = db.query(Event).filter(Event.id == event_id).first()
    if event is None:
        return
    started = event.start_datetime
    if started is not None and started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if started is None or started >= datetime.now(timezone.utc):
        return
    raise HTTPException(
        status_code=400,
        detail=(
            f"Урок от {_format_dt(event.start_datetime)} уже проведён: по нему выставлено "
            f"отметок — {marks}. Перенос сдвинул бы эти отметки на новую дату, как будто "
            "занятие прошло тогда. Если урок нужно провести ещё раз — добавьте новый урок; "
            "если отметки поставлены по ошибке — сначала снимите их."
        ),
    )
_NO_LESSON_NOTE = (
    "Заявка одобрена, но урок не найден в расписании. Обратитесь к администратору."
)


def _parse_teacher_ids(raw) -> Optional[list]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, list) else None


def enrich_requests(lrs: Sequence[LessonRequest], db: Session) -> list[LessonRequestSchema]:
    """Serialise a page of requests in a fixed number of queries.

    The per-row version issued one ``SELECT`` per user it needed to name — requester,
    proposed substitute, confirmed substitute, and one per candidate id — so a page of fifty
    requests cost a few hundred round trips. Everything is prefetched here instead.

    It also answers the question the page could not previously ask: *did the approval
    actually take effect?* A request records a decision; the Event records what will happen.
    They can disagree — schedule regeneration used to overwrite approved substitutions — and
    a page rendering only the request cannot show that. So the live lesson is read too, and
    the two are compared.
    """
    from src.services.lesson_teacher import (
        APPROVED_STATUS,
        SUBSTITUTION_REQUEST_TYPE,
        approved_substitute_id,
    )
    from src.services.attendance_status import marked_statuses_for_sql
    from src.schemas.models import Attendance
    from sqlalchemy import func

    if not lrs:
        return []

    # ── one round trip per entity kind ────────────────────────────────────────────────
    candidate_ids: set[int] = set()
    for lr in lrs:
        for value in (lr.requester_id, lr.substitute_teacher_id,
                      lr.confirmed_teacher_id, lr.resolved_by):
            if value:
                candidate_ids.add(int(value))
        for tid in (_parse_teacher_ids(lr.substitute_teacher_ids) or []):
            if isinstance(tid, int):
                candidate_ids.add(tid)

    group_ids = {int(lr.group_id) for lr in lrs if lr.group_id}
    event_ids = {int(lr.event_id) for lr in lrs if lr.event_id}
    # Replacement lessons (an approved cancel resolved as «добавить урок в конец курса») ride
    # along in the same event query — one round trip, not two.
    replacement_ids = {int(lr.replacement_event_id) for lr in lrs if lr.replacement_event_id}

    groups = {
        g.id: g for g in db.query(Group).filter(Group.id.in_(group_ids)).all()
    } if group_ids else {}
    events = {
        e.id: e
        for e in db.query(Event).filter(Event.id.in_(event_ids | replacement_ids)).all()
    } if (event_ids or replacement_ids) else {}

    # Group owners and live lesson teachers also need naming.
    for group in groups.values():
        if group.teacher_id:
            candidate_ids.add(int(group.teacher_id))
    for event in events.values():
        if event.teacher_id:
            candidate_ids.add(int(event.teacher_id))

    users = {
        u.id: u
        for u in db.query(UserInDB).filter(UserInDB.id.in_(sorted(candidate_ids))).all()
    } if candidate_ids else {}

    marked_event_ids: set[int] = set()
    if event_ids:
        marked_event_ids = {
            int(row[0])
            for row in db.query(Attendance.event_id)
            .filter(
                Attendance.event_id.in_(sorted(event_ids)),
                func.lower(func.coalesce(Attendance.status, "")).in_(marked_statuses_for_sql()),
            )
            .distinct()
            .all()
        }

    def name_of(user_id) -> Optional[str]:
        user = users.get(int(user_id)) if user_id else None
        return user.name if user else None

    out: list[LessonRequestSchema] = []
    for lr in lrs:
        group = groups.get(int(lr.group_id)) if lr.group_id else None
        event = events.get(int(lr.event_id)) if lr.event_id else None
        replacement = (
            events.get(int(lr.replacement_event_id)) if lr.replacement_event_id else None
        )
        resolver = users.get(int(lr.resolved_by)) if lr.resolved_by else None

        teacher_ids_list = _parse_teacher_ids(lr.substitute_teacher_ids)
        teacher_names_list = (
            [name_of(tid) or "Unknown" for tid in teacher_ids_list]
            if teacher_ids_list is not None
            else None
        )

        # The register is owed by whoever is actually assigned to the lesson, falling back to
        # the group's regular teacher only when the lesson never recorded one.
        attendance_owner_id = None
        if event is not None:
            attendance_owner_id = event.teacher_id or (group.teacher_id if group else None)

        # Does the schedule agree with the decision? Only meaningful once approved.
        is_applied: Optional[bool] = None
        consistency_note: Optional[str] = None
        if lr.status == APPROVED_STATUS and lr.request_type == SUBSTITUTION_REQUEST_TYPE:
            approved_id = approved_substitute_id(lr)
            if event is None:
                is_applied, consistency_note = False, _NO_LESSON_NOTE
            elif approved_id is None:
                is_applied, consistency_note = False, _NO_LESSON_NOTE
            else:
                is_applied = event.teacher_id == approved_id
                if not is_applied:
                    consistency_note = _NOT_APPLIED_NOTE

        out.append(LessonRequestSchema(
            id=lr.id,
            request_type=lr.request_type,
            status=lr.status,
            requester_id=lr.requester_id,
            requester_name=name_of(lr.requester_id),
            lesson_schedule_id=lr.lesson_schedule_id,
            event_id=lr.event_id,
            group_id=lr.group_id,
            group_name=group.name if group else None,
            original_datetime=lr.original_datetime,
            substitute_teacher_id=lr.substitute_teacher_id,
            substitute_teacher_name=name_of(lr.substitute_teacher_id),
            substitute_teacher_ids=teacher_ids_list,
            substitute_teacher_names=teacher_names_list,
            confirmed_teacher_id=lr.confirmed_teacher_id,
            confirmed_teacher_name=name_of(lr.confirmed_teacher_id),
            new_datetime=lr.new_datetime,
            reason=lr.reason,
            admin_comment=lr.admin_comment,
            created_at=lr.created_at,
            resolved_at=lr.resolved_at,
            resolved_by=lr.resolved_by,
            resolver_name=resolver.name if resolver else None,
            resolver_role=resolver.role if resolver else None,
            lesson_title=event.title if event else None,
            current_event_teacher_id=event.teacher_id if event else None,
            current_event_teacher_name=name_of(event.teacher_id) if event else None,
            group_teacher_id=group.teacher_id if group else None,
            group_teacher_name=name_of(group.teacher_id) if group else None,
            attendance_owner_id=attendance_owner_id,
            attendance_owner_name=name_of(attendance_owner_id),
            attendance_marked=(int(lr.event_id) in marked_event_ids) if lr.event_id else None,
            is_applied=is_applied,
            consistency_note=consistency_note,
            lesson_is_active=event.is_active if event else None,
            cancel_resolution=lr.cancel_resolution,
            replacement_event_id=lr.replacement_event_id,
            replacement_lesson_title=replacement.title if replacement else None,
            replacement_datetime=replacement.start_datetime if replacement else None,
        ))
    return out


def enrich_request(lr: LessonRequest, db: Session) -> LessonRequestSchema:
    """Single-request convenience wrapper. Callers rendering a list must use
    :func:`enrich_requests`, which is the same work without the round trips."""
    return enrich_requests([lr], db)[0]


def apply_substitution(db: Session, lr: LessonRequest, resolver_id: int) -> None:
    """Pin the approved substitute to the exact occurrence, and say so in the audit trail.

    Approval is one atomic act: resolve (materialising the lesson if it only existed as a
    schedule row), assign, and record. It runs inside the caller's transaction, so a
    substitution that is approved is also applied, or neither happened.

    ``groups.teacher_id`` is deliberately not touched. The regular teacher still owns the
    group; they simply are not teaching this one lesson. See
    :mod:`src.services.lesson_teacher` for why those two facts have to stay separate.
    """
    from src.services.lesson_teacher import approved_substitute_id, record_lesson_teacher_audit

    event_id = lr.event_id
    if not event_id and lr.lesson_schedule_id:
        event_id = EventService.materialize_lesson_schedule(db, lr.lesson_schedule_id, user_id=resolver_id)
        lr.event_id = event_id
        db.add(lr)

    new_teacher_id = approved_substitute_id(lr)

    if not event_id:
        # Approving something with no lesson to point at leaves the request "approved" and
        # the schedule unchanged — the exact silent divergence `/lesson-requests` now shows
        # as a consistency warning. Loud in the log so it is not discovered via payroll.
        logger.error(
            "substitution request %s approved but no event could be resolved "
            "(lesson_schedule_id=%s)",
            lr.id, lr.lesson_schedule_id,
        )
        return

    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        logger.error(
            "substitution request %s references event %s, which does not exist", lr.id, event_id
        )
        return
    if not new_teacher_id:
        logger.error("substitution request %s approved without naming a substitute", lr.id)
        return

    before_teacher_id = event.teacher_id
    if before_teacher_id == new_teacher_id:
        # Idempotent: re-approving, or re-running the repair, must not churn the audit trail.
        return

    event.teacher_id = new_teacher_id
    record_lesson_teacher_audit(
        db,
        event_id=event.id,
        before_teacher_id=before_teacher_id,
        after_teacher_id=new_teacher_id,
        action="lesson.substitution.applied",
        actor_id=resolver_id,
        requester_id=lr.requester_id,
        lesson_request_id=lr.id,
        reason=lr.reason,
    )
    db.flush()

    from src.services import telegram_lesson_notices

    telegram_lesson_notices.queue_for_request(
        db, lr, event, telegram_lesson_notices.SUBSTITUTED,
        old_start=event.start_datetime, old_teacher_id=before_teacher_id, new_teacher_id=new_teacher_id,
    )


def apply_reschedule(db: Session, lr: LessonRequest, resolver_id: int) -> None:
    event_id = lr.event_id
    if not event_id and lr.lesson_schedule_id:
        event_id = EventService.materialize_lesson_schedule(db, lr.lesson_schedule_id, user_id=resolver_id)
        lr.event_id = event_id
        db.add(lr)

    # Checked again here, not only when the request was filed: a request may sit pending for
    # days, and the lesson it names can be taught and marked in the meantime. The approval is
    # the moment the move actually happens, so it is the moment that has to be sure.
    assert_reschedule_is_not_rewriting_history(db, event_id)

    if event_id and lr.new_datetime:
        event = db.query(Event).filter(Event.id == event_id).first()
        if event:
            old_start = event.start_datetime
            duration = event.end_datetime - event.start_datetime
            event.start_datetime = lr.new_datetime
            event.end_datetime = lr.new_datetime + duration
            db.flush()

            from src.services import telegram_lesson_notices

            telegram_lesson_notices.queue_for_request(
                db, lr, event, telegram_lesson_notices.RESCHEDULED,
                old_start=old_start, new_start=event.start_datetime,
            )

    if lr.lesson_schedule_id and lr.new_datetime:
        schedule = db.query(LessonSchedule).filter(LessonSchedule.id == lr.lesson_schedule_id).first()
        if schedule:
            schedule.scheduled_at = lr.new_datetime
            db.flush()


def _naive_utc(dt: datetime) -> datetime:
    """Events are stored as naive UTC; an in-memory value may still carry a tzinfo."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _active_class_events(db: Session, group_id: int) -> list[Event]:
    return (
        db.query(Event)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .filter(
            EventGroup.group_id == group_id,
            Event.event_type == "class",
            Event.is_active == True,
        )
        .all()
    )


def _regular_slots(
    group: Group, active_events: Sequence[Event], cancelled_event: Event
) -> list[tuple[int, time]]:
    """The group's weekly slots as ``(weekday, time)`` in Almaty, Monday = 0.

    From ``schedule_config.schedule_items`` when the group has a generated schedule. A group
    whose lessons were placed by hand has none, so the slots are read off its active lessons
    instead; a group with no other lessons at all inherits the cancelled lesson's own slot.
    """
    slots: set[tuple[int, time]] = set()

    config = group.schedule_config if isinstance(group.schedule_config, dict) else {}
    for item in config.get("schedule_items") or []:
        if not isinstance(item, dict):
            continue
        try:
            weekday = int(item.get("day_of_week"))
            at = datetime.strptime(str(item.get("time_of_day")), "%H:%M").time()
        except (TypeError, ValueError):
            continue
        if 0 <= weekday <= 6:
            slots.add((weekday, at))

    if not slots:
        for source in (active_events, [cancelled_event]):
            for event in source:
                local = _naive_utc(event.start_datetime) + KZ_OFFSET
                slots.add((local.weekday(), local.time().replace(second=0, microsecond=0)))
            if slots:
                break

    return sorted(slots)


def _decrement_planned_lessons(group: Group) -> None:
    """One lesson fewer in the plan, so the course can still finish.

    ``is_over`` is «past >= planned and no future lesson», with planned read from
    ``schedule_config.lessons_count``. A course planned at 48 with one lesson cancelled and
    not replaced has 47 lessons, so it would never become «Завершена» — which also starves
    the CRM's «Завершил» queue. JSONB change detection needs a new object, not a mutation.
    """
    config = group.schedule_config
    if not isinstance(config, dict):
        return
    lessons_count = config.get("lessons_count")
    if isinstance(lessons_count, bool) or not isinstance(lessons_count, int):
        return
    if lessons_count > 1:
        group.schedule_config = {**config, "lessons_count": lessons_count - 1}


def append_replacement_lesson(
    db: Session, group: Group, cancelled_event: Event, *, created_by: int
) -> Event:
    """Append one class lesson after the group's last scheduled one, on its regular slot.

    The cancelled lesson is already inactive by the time this runs, so the anchor is the
    latest ACTIVE lesson. The replacement is the earliest regular slot strictly after the
    anchor, after now, AND after the cancelled lesson itself — a cancelled lesson in a
    finished course is replaced in the future, not back-dated, and a cancelled LAST lesson
    is not re-created on the very instant just cancelled (its slot is free again and would
    otherwise be the earliest candidate, silently undoing the cancellation). A slot the
    group already has an active event on is skipped.

    The lesson is the group's regular teacher's: a substitute pinned to the cancelled
    occurrence does not follow it. ``schedule_config.lessons_count`` is left alone — the
    plan is intact, one lesson simply moved to the end. Titled ``"{group}: Lesson"`` here;
    :func:`renumber_lesson_titles` gives it its number.

    Raises ``HTTPException(400)`` when no free slot exists within eight weeks, so the
    approval fails loudly and the head teacher can choose «только отменить» instead.
    """
    now_utc = datetime.utcnow()
    active = _active_class_events(db, group.id)
    slots = _regular_slots(group, active, cancelled_event)

    cancelled_start = _naive_utc(cancelled_event.start_datetime)
    anchor = max((_naive_utc(e.start_datetime) for e in active), default=cancelled_start)
    # The cancelled instant belongs in the lower bound whether or not it is the anchor: the
    # row is already deactivated, so it is neither in ``active`` nor in ``occupied`` and its
    # slot would read as the earliest free one when the cancelled lesson was the last.
    floor = max(anchor, now_utc, cancelled_start)

    occupied = {
        _naive_utc(row[0])
        for row in db.query(Event.start_datetime)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .filter(EventGroup.group_id == group.id, Event.is_active == True)
        .all()
    }

    start_local_date = (floor + KZ_OFFSET).date()
    candidate: Optional[datetime] = None
    for day_offset in range(_REPLACEMENT_SEARCH_WEEKS * 7 + 1):
        local_date = start_local_date + timedelta(days=day_offset)
        for weekday, at in slots:
            if weekday != local_date.weekday():
                continue
            instant = datetime.combine(local_date, at) - KZ_OFFSET
            if instant <= floor or instant in occupied:
                continue
            candidate = instant
            break
        if candidate is not None:
            break

    if candidate is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Не удалось найти свободный слот для дополнительного урока группы "
                f"«{group.name}» в ближайшие {_REPLACEMENT_SEARCH_WEEKS} недель. "
                "Проверьте расписание группы или выберите «только отменить»."
            ),
        )

    duration = _naive_utc(cancelled_event.end_datetime) - _naive_utc(cancelled_event.start_datetime)
    if duration <= timedelta(0):
        duration = timedelta(minutes=60)

    replacement = Event(
        title=f"{group.name}: Lesson",
        description=(
            "Replacement for the cancelled lesson on "
            f"{_format_dt(cancelled_event.start_datetime)}"
        ),
        event_type="class",
        start_datetime=candidate,
        end_datetime=candidate + duration,
        location=cancelled_event.location,
        is_online=cancelled_event.is_online,
        meeting_url=cancelled_event.meeting_url,
        created_by=created_by,
        teacher_id=group.teacher_id,
        is_active=True,
        is_recurring=False,
        max_participants=50,
    )
    db.add(replacement)
    db.flush()
    db.add(EventGroup(event_id=replacement.id, group_id=group.id))
    db.flush()
    logger.info(
        "replacement lesson event_id=%s appended to group_id=%s at %s for cancelled event_id=%s",
        replacement.id, group.id, candidate.isoformat(), cancelled_event.id,
    )
    return replacement


def renumber_lesson_titles(db: Session, group: Group) -> None:
    """``"{group.name}: Lesson {i}"`` over the group's active class lessons, by date.

    The same numbering ``reconcile_group_schedule`` produces, restricted — like the CRM's
    ``retitle_group_lessons`` — to titles that already carry ``": Lesson"``. A custom title
    is somebody's decision and is left alone.
    """
    ordered = sorted(
        _active_class_events(db, group.id),
        key=lambda e: (_naive_utc(e.start_datetime), e.id),
    )
    for index, event in enumerate(ordered, start=1):
        if not event.title or not _LESSON_SUFFIX.search(event.title):
            continue
        target = f"{group.name}: Lesson {index}"
        if event.title != target:
            event.title = target
    db.flush()


def apply_cancel(db: Session, lr: LessonRequest, resolver_id: int) -> None:
    """Deactivate the lesson, mark it «cancelled» for every student, then resolve.

    ``lr.cancel_resolution`` decides what happens to the course afterwards:

    * ``cancel_only`` (the default, and the only behaviour there used to be): the lesson is
      gone as if it never happened, and the plan shrinks by one so the group can still
      finish — see :func:`_decrement_planned_lessons` for why that matters.
    * ``add_replacement``: the plan is intact and one lesson is appended after the group's
      last scheduled one — see :func:`append_replacement_lesson`.

    Either way the surviving lessons are renumbered and the group's ``is_over`` is
    recomputed, all inside the caller's transaction: nothing here commits.

    Applying the same cancel twice must not change the course twice, so everything past the
    deactivation is skipped when the lesson was already inactive.
    """
    from src.services.group_completion_service import sync_groups_over_status

    event_id = lr.event_id
    if not event_id and lr.lesson_schedule_id:
        event_id = EventService.materialize_lesson_schedule(db, lr.lesson_schedule_id, user_id=resolver_id)
        lr.event_id = event_id
        db.add(lr)

    if not event_id:
        return

    event = db.query(Event).filter(Event.id == event_id).first()
    # Whether THIS approval is the one that took the lesson off the schedule. Deactivating an
    # already-inactive event is a no-op, but appending a lesson or shrinking the plan is not.
    was_active = bool(event and event.is_active)
    if was_active:
        event.is_active = False
        db.flush()

        from src.services import telegram_lesson_notices

        telegram_lesson_notices.queue_for_request(
            db, lr, event, telegram_lesson_notices.CANCELLED, old_start=event.start_datetime,
        )

    # Mark every group student's attendance for this lesson as "cancelled"
    # so it surfaces in Attendance / leaderboard grids and is excluded from
    # attendance-rate scoring (a cancelled lesson must not count as absent).
    student_ids = [
        row[0]
        for row in db.query(GroupStudent.student_id).filter(
            GroupStudent.group_id == lr.group_id
        ).all()
    ]
    for sid in student_ids:
        AttendanceService.upsert_for_event(
            db,
            event_id=event_id,
            user_id=sid,
            status="cancelled",
            score=0,
            flush=False,
        )
    if student_ids:
        db.flush()

    resolution = lr.cancel_resolution or CANCEL_ONLY
    group = db.query(Group).filter(Group.id == lr.group_id).first()

    if event is None:
        if resolution == ADD_REPLACEMENT:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Отменяемый урок не найден в расписании, поэтому добавить урок в конец "
                    "курса нельзя. Выберите «только отменить»."
                ),
            )
        logger.error("cancel request %s references event %s, which does not exist", lr.id, event_id)
        return
    if group is None:
        logger.error("cancel request %s references group %s, which does not exist", lr.id, lr.group_id)
        return

    if not was_active:
        # The lesson was already off the schedule: a second cancel request for the same
        # occurrence (the create-side guard only stops the SAME requester filing twice, so a
        # substitute and the regular teacher can both file one), an approval of a request
        # left pending while the event was deactivated another way, or two approvers racing.
        # Marking it cancelled again is harmless; a second replacement lesson or a second
        # decrement of the plan is not, so the resolution stops here.
        logger.warning(
            "cancel request %s: event %s is already inactive — leaving the group's plan alone",
            lr.id, event_id,
        )
        return

    if resolution == ADD_REPLACEMENT:
        replacement = append_replacement_lesson(db, group, event, created_by=resolver_id)
        lr.replacement_event_id = replacement.id
        db.add(lr)
    else:
        _decrement_planned_lessons(group)

    renumber_lesson_titles(db, group)
    db.flush()
    sync_groups_over_status(db, [group.id], commit=False)


def apply_approved_request(db: Session, lr: LessonRequest, resolver_id: int) -> None:
    if lr.request_type == "substitution":
        apply_substitution(db, lr, resolver_id)
    elif lr.request_type == "reschedule":
        apply_reschedule(db, lr, resolver_id)
    elif lr.request_type == "cancel":
        apply_cancel(db, lr, resolver_id)


def _format_dt(dt: datetime | None) -> str:
    if not dt:
        return "TBD"
    # Stored datetimes are naive UTC; display in Asia/Almaty (UTC+5).
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("Asia/Almaty")).strftime("%d %b %Y %H:%M")


def notify_approvers_of_request(db: Session, lr: LessonRequest, requester: UserInDB) -> None:
    group = db.query(Group).filter(Group.id == lr.group_id).first()
    group_name = group.name if group else "Unknown Group"
    content = (
        f"{requester.name} requested a {lr.request_type} for {group_name} "
        f"on {_format_dt(lr.original_datetime)}"
    )

    head_teachers = resolve_head_teachers_for_group(db, lr.group_id)
    notified_ids: set[int] = set()

    for head in head_teachers:
        db.add(
            Notification(
                user_id=head.id,
                title="New Lesson Request",
                content=content,
                notification_type="lesson_request",
                related_id=lr.id,
            )
        )
        notified_ids.add(head.id)

    if not head_teachers:
        admins = db.query(UserInDB).filter(UserInDB.role == "admin", UserInDB.is_active == True).all()
        for admin in admins:
            if admin.id not in notified_ids:
                db.add(
                    Notification(
                        user_id=admin.id,
                        title="New Lesson Request (no head teacher)",
                        content=content,
                        notification_type="lesson_request",
                        related_id=lr.id,
                    )
                )
    db.commit()


def notify_approvers_of_confirmation(db: Session, lr: LessonRequest, teacher: UserInDB) -> None:
    group = db.query(Group).filter(Group.id == lr.group_id).first()
    group_name = group.name if group else "Unknown Group"
    content = f"{teacher.name} confirmed substitution for {group_name}. Ready for approval."

    head_teachers = resolve_head_teachers_for_group(db, lr.group_id)
    if head_teachers:
        for head in head_teachers:
            db.add(
                Notification(
                    user_id=head.id,
                    title="Substitution Confirmed",
                    content=content,
                    notification_type="lesson_request",
                    related_id=lr.id,
                )
            )
    else:
        admins = db.query(UserInDB).filter(UserInDB.role == "admin", UserInDB.is_active == True).all()
        for admin in admins:
            db.add(
                Notification(
                    user_id=admin.id,
                    title="Substitution Confirmed",
                    content=content,
                    notification_type="lesson_request",
                    related_id=lr.id,
                )
            )
    db.commit()


def notify_resolution(db: Session, lr: LessonRequest, approved: bool) -> None:
    status_word = "approved" if approved else "rejected"
    group = db.query(Group).filter(Group.id == lr.group_id).first()
    group_name = group.name if group else "Unknown Group"
    requester = db.query(UserInDB).filter(UserInDB.id == lr.requester_id).first()
    pending_curator_email: dict | None = None
    # The lesson appended under «добавить урок в конец курса», if the cancel was resolved so.
    replacement = (
        db.query(Event).filter(Event.id == lr.replacement_event_id).first()
        if approved and lr.request_type == "cancel" and lr.replacement_event_id
        else None
    )

    db.add(
        Notification(
            user_id=lr.requester_id,
            title=f"Lesson Request {status_word.title()}",
            content=f"Your {lr.request_type} request for {group_name} has been {status_word}.",
            notification_type="lesson_request",
            related_id=lr.id,
        )
    )

    if approved:
        student_ids = db.query(GroupStudent.student_id).filter(
            GroupStudent.group_id == lr.group_id
        ).all()

        if lr.request_type == "substitution":
            sub_teacher_id = lr.confirmed_teacher_id or lr.substitute_teacher_id
            sub_teacher = (
                db.query(UserInDB).filter(UserInDB.id == sub_teacher_id).first()
                if sub_teacher_id
                else None
            )
            sub_name = sub_teacher.name if sub_teacher else "another teacher"
            msg = (
                f"Your class in {group_name} on {_format_dt(lr.original_datetime)} "
                f"will be taught by {sub_name}."
            )
        elif lr.request_type == "cancel":
            msg = (
                f"Your class in {group_name} on {_format_dt(lr.original_datetime)} "
                f"has been cancelled."
            )
            if replacement is not None:
                msg += (
                    " A replacement lesson has been added on "
                    f"{_format_dt(replacement.start_datetime)}."
                )
        else:
            msg = (
                f"Your class in {group_name} has been rescheduled from "
                f"{_format_dt(lr.original_datetime)} to {_format_dt(lr.new_datetime)}."
            )

        for (sid,) in student_ids:
            db.add(
                Notification(
                    user_id=sid,
                    title="Lesson Change",
                    content=msg,
                    notification_type="lesson_request",
                    related_id=lr.id,
                )
            )

        if group and group.curator_id:
            curator = db.query(UserInDB).filter(UserInDB.id == group.curator_id).first()
            if curator and curator.email:
                sub_teacher_id = lr.confirmed_teacher_id or lr.substitute_teacher_id
                sub_teacher = (
                    db.query(UserInDB).filter(UserInDB.id == sub_teacher_id).first()
                    if sub_teacher_id
                    else None
                )
                # Read everything the email needs now, while the objects are loaded, but
                # send after the commit — see below.
                pending_curator_email = dict(
                    curator_email=curator.email,
                    curator_name=curator.name or curator.email,
                    group_name=group_name,
                    request_type=lr.request_type,
                    original_datetime=_format_dt(lr.original_datetime),
                    # For a reschedule: where the lesson moved. For a cancel with a
                    # replacement: when the appended lesson is. The template labels each.
                    new_datetime=(
                        _format_dt(replacement.start_datetime)
                        if replacement is not None
                        else (_format_dt(lr.new_datetime) if lr.new_datetime else None)
                    ),
                    substitute_name=sub_teacher.name if sub_teacher else None,
                    requester_name=requester.name if requester else None,
                    reason=lr.reason,
                    curator_id=curator.id,
                    lesson_request_id=lr.id,
                )

    db.commit()

    # After the commit, never before. Sending inside the transaction meant a curator could
    # be told a lesson had moved and then have the write roll back underneath them —
    # and the send is a 10-second blocking HTTP call holding the transaction open while
    # Resend answers. A failure here is logged and dropped: the schedule change is
    # already durable, and an email problem must not surface as a failed approval.
    if pending_curator_email is not None:
        try:
            send_lesson_change_curator_notification(**pending_curator_email)
        except Exception:
            logger.exception(
                "Curator lesson-change email failed for request %s", lr.id
            )

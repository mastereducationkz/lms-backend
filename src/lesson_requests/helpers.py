"""Shared helpers for lesson request routes and CRM internal API."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.schemas.models import (
    UserInDB,
    Group,
    LessonSchedule,
    Event,
    LessonRequest,
    LessonRequestSchema,
    Notification,
    GroupStudent,
)
from src.services.event_service import EventService
from src.services.attendance_service import AttendanceService
from src.services.email_service import send_lesson_change_curator_notification
from src.lesson_requests.services import resolve_head_teachers_for_group

logger = logging.getLogger(__name__)


def enrich_request(lr: LessonRequest, db: Session) -> LessonRequestSchema:
    requester = db.query(UserInDB).filter(UserInDB.id == lr.requester_id).first()
    group = db.query(Group).filter(Group.id == lr.group_id).first()
    sub_teacher = None
    if lr.substitute_teacher_id:
        sub_teacher = db.query(UserInDB).filter(UserInDB.id == lr.substitute_teacher_id).first()

    confirmed_teacher = None
    if lr.confirmed_teacher_id:
        confirmed_teacher = db.query(UserInDB).filter(UserInDB.id == lr.confirmed_teacher_id).first()

    teacher_ids_list = None
    teacher_names_list = None
    if lr.substitute_teacher_ids:
        try:
            teacher_ids_list = json.loads(lr.substitute_teacher_ids)
            teacher_names_list = []
            for tid in teacher_ids_list:
                t = db.query(UserInDB).filter(UserInDB.id == tid).first()
                teacher_names_list.append(t.name if t else "Unknown")
        except (json.JSONDecodeError, TypeError):
            pass

    return LessonRequestSchema(
        id=lr.id,
        request_type=lr.request_type,
        status=lr.status,
        requester_id=lr.requester_id,
        requester_name=requester.name if requester else None,
        lesson_schedule_id=lr.lesson_schedule_id,
        event_id=lr.event_id,
        group_id=lr.group_id,
        group_name=group.name if group else None,
        original_datetime=lr.original_datetime,
        substitute_teacher_id=lr.substitute_teacher_id,
        substitute_teacher_name=sub_teacher.name if sub_teacher else None,
        substitute_teacher_ids=teacher_ids_list,
        substitute_teacher_names=teacher_names_list,
        confirmed_teacher_id=lr.confirmed_teacher_id,
        confirmed_teacher_name=confirmed_teacher.name if confirmed_teacher else None,
        new_datetime=lr.new_datetime,
        reason=lr.reason,
        admin_comment=lr.admin_comment,
        created_at=lr.created_at,
        resolved_at=lr.resolved_at,
        resolved_by=lr.resolved_by,
    )


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


def apply_reschedule(db: Session, lr: LessonRequest, resolver_id: int) -> None:
    event_id = lr.event_id
    if not event_id and lr.lesson_schedule_id:
        event_id = EventService.materialize_lesson_schedule(db, lr.lesson_schedule_id, user_id=resolver_id)
        lr.event_id = event_id
        db.add(lr)

    if event_id and lr.new_datetime:
        event = db.query(Event).filter(Event.id == event_id).first()
        if event:
            duration = event.end_datetime - event.start_datetime
            event.start_datetime = lr.new_datetime
            event.end_datetime = lr.new_datetime + duration
            db.flush()

    if lr.lesson_schedule_id and lr.new_datetime:
        schedule = db.query(LessonSchedule).filter(LessonSchedule.id == lr.lesson_schedule_id).first()
        if schedule:
            schedule.scheduled_at = lr.new_datetime
            db.flush()


def apply_cancel(db: Session, lr: LessonRequest, resolver_id: int) -> None:
    event_id = lr.event_id
    if not event_id and lr.lesson_schedule_id:
        event_id = EventService.materialize_lesson_schedule(db, lr.lesson_schedule_id, user_id=resolver_id)
        lr.event_id = event_id
        db.add(lr)

    if event_id:
        event = db.query(Event).filter(Event.id == event_id).first()
        if event:
            event.is_active = False
            db.flush()

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
                    new_datetime=_format_dt(lr.new_datetime) if lr.new_datetime else None,
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

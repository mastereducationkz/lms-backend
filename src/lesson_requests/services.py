"""Business logic for lesson substitution, reschedule, and cancel requests."""
from __future__ import annotations

import calendar
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.schemas.models import (
    UserInDB,
    Group,
    LessonRequest,
    CreateLessonRequestSchema,
    Course,
    CourseGroupAccess,
    CourseHeadTeacher,
)

HEAD_ROUTING: dict[str, str] = {
    "sat": "sat",
    "nuet": "nuet",
    "ielts": "ielts",
    "general_english": "ielts",
}

VALID_REQUEST_TYPES = ("substitution", "reschedule", "cancel")


def get_target_program_type(group_program_type: str | None) -> str:
    normalized = (group_program_type or "general_english").lower()
    return HEAD_ROUTING.get(normalized, normalized)


def resolve_head_teachers_for_group(db: Session, group_id: int) -> list[UserInDB]:
    """Find head teachers who should receive notifications for this group's requests."""
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        return []

    target_type = get_target_program_type(getattr(group, "program_type", None))

    linked_course_ids = [
        row[0]
        for row in db.query(CourseGroupAccess.course_id).filter(
            CourseGroupAccess.group_id == group_id,
            CourseGroupAccess.is_active == True,
        ).all()
    ]
    if not linked_course_ids:
        return []

    matching_course_ids = [
        row[0]
        for row in db.query(Course.id).filter(
            Course.id.in_(linked_course_ids),
            Course.course_type == target_type,
        ).all()
    ]
    if not matching_course_ids:
        return []

    head_teacher_ids = {
        row[0]
        for row in db.query(CourseHeadTeacher.head_teacher_id).filter(
            CourseHeadTeacher.course_id.in_(matching_course_ids),
        ).all()
    }
    if not head_teacher_ids:
        return []

    return (
        db.query(UserInDB)
        .filter(
            UserInDB.id.in_(head_teacher_ids),
            UserInDB.role == "head_teacher",
            UserInDB.is_active == True,
        )
        .all()
    )


def get_group_ids_in_head_teacher_scope(db: Session, head_teacher_id: int) -> list[int]:
    """Group IDs whose lesson requests this head teacher may approve."""
    managed_course_ids = [
        row[0]
        for row in db.query(CourseHeadTeacher.course_id).filter(
            CourseHeadTeacher.head_teacher_id == head_teacher_id,
        ).all()
    ]
    if not managed_course_ids:
        return []

    managed_courses = db.query(Course.id, Course.course_type).filter(
        Course.id.in_(managed_course_ids),
    ).all()
    managed_types = {course_type for _, course_type in managed_courses}
    managed_by_type: dict[str, set[int]] = {}
    for course_id, course_type in managed_courses:
        managed_by_type.setdefault(course_type, set()).add(course_id)

    group_ids: set[int] = set()
    all_groups = db.query(Group.id, Group.program_type).filter(Group.is_active == True).all()
    for group_id, program_type in all_groups:
        target_type = get_target_program_type(program_type)
        if target_type not in managed_types:
            continue
        relevant_course_ids = managed_by_type.get(target_type, set())
        if not relevant_course_ids:
            continue
        has_link = (
            db.query(CourseGroupAccess.id)
            .filter(
                CourseGroupAccess.group_id == group_id,
                CourseGroupAccess.course_id.in_(relevant_course_ids),
                CourseGroupAccess.is_active == True,
            )
            .first()
        )
        if has_link:
            group_ids.add(group_id)

    return sorted(group_ids)


def head_teacher_can_approve_group(db: Session, head_teacher_id: int, group_id: int) -> bool:
    return group_id in get_group_ids_in_head_teacher_scope(db, head_teacher_id)


def user_can_resolve_request(db: Session, user: UserInDB, group_id: int) -> bool:
    if user.role == "admin":
        return True
    if user.role == "head_teacher":
        return head_teacher_can_approve_group(db, user.id, group_id)
    return False


def create_lesson_request_record(
    db: Session,
    requester: UserInDB,
    data: CreateLessonRequestSchema,
) -> LessonRequest:
    """Validate and persist a new lesson request."""
    if data.request_type not in VALID_REQUEST_TYPES:
        raise HTTPException(
            status_code=400,
            detail="request_type must be 'substitution', 'reschedule', or 'cancel'",
        )

    group = db.query(Group).filter(Group.id == data.group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    teacher_ids = data.substitute_teacher_ids or (
        [data.substitute_teacher_id] if data.substitute_teacher_id else []
    )

    if data.request_type == "substitution":
        if not teacher_ids:
            raise HTTPException(
                status_code=400,
                detail="At least one substitute teacher is required for substitution",
            )
        for tid in teacher_ids:
            sub_teacher = (
                db.query(UserInDB)
                .filter(
                    UserInDB.id == tid,
                    UserInDB.role == "teacher",
                    UserInDB.is_active == True,
                )
                .first()
            )
            if not sub_teacher:
                raise HTTPException(status_code=404, detail=f"Substitute teacher {tid} not found")
            if sub_teacher.no_substitutions:
                raise HTTPException(
                    status_code=400,
                    detail=f"Teacher {sub_teacher.name} has opted out of substitutions",
                )

    if data.request_type == "reschedule" and not data.new_datetime:
        raise HTTPException(status_code=400, detail="new_datetime required for reschedule")

    original_dt = data.original_datetime
    if not original_dt.tzinfo:
        original_dt = original_dt.replace(tzinfo=timezone.utc)

    if original_dt < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Cannot create requests for past lessons.")

    req_year = original_dt.year
    req_month = original_dt.month
    _, last_day = calendar.monthrange(req_year, req_month)
    month_start = datetime(req_year, req_month, 1, 0, 0, 0, tzinfo=timezone.utc)
    month_end = datetime(req_year, req_month, last_day, 23, 59, 59, tzinfo=timezone.utc)

    month_count = (
        db.query(LessonRequest)
        .filter(
            LessonRequest.requester_id == requester.id,
            LessonRequest.group_id == data.group_id,
            LessonRequest.status.in_(["pending", "pending_teacher", "approved"]),
            LessonRequest.original_datetime >= month_start,
            LessonRequest.original_datetime <= month_end,
        )
        .count()
    )
    if month_count >= 2:
        raise HTTPException(
            status_code=400,
            detail="Monthly limit reached: You can only request 2 lesson changes per group per month.",
        )

    dup_query = db.query(LessonRequest).filter(
        LessonRequest.requester_id == requester.id,
        LessonRequest.group_id == data.group_id,
        LessonRequest.status.in_(["pending", "pending_teacher"]),
    )
    if data.event_id:
        dup_query = dup_query.filter(LessonRequest.event_id == data.event_id)
    elif data.lesson_schedule_id:
        dup_query = dup_query.filter(LessonRequest.lesson_schedule_id == data.lesson_schedule_id)
    else:
        dup_query = dup_query.filter(LessonRequest.original_datetime == original_dt)
    if dup_query.first():
        raise HTTPException(
            status_code=400,
            detail="A pending request already exists for this lesson.",
        )

    if data.request_type == "substitution":
        initial_status = "pending_teacher"
    else:
        initial_status = "pending"

    new_request = LessonRequest(
        request_type=data.request_type,
        requester_id=requester.id,
        lesson_schedule_id=data.lesson_schedule_id,
        event_id=data.event_id,
        group_id=data.group_id,
        original_datetime=data.original_datetime,
        substitute_teacher_id=teacher_ids[0] if teacher_ids else None,
        substitute_teacher_ids=json.dumps(teacher_ids) if teacher_ids else None,
        new_datetime=data.new_datetime,
        reason=data.reason,
        status=initial_status,
    )
    db.add(new_request)
    db.commit()
    db.refresh(new_request)
    return new_request

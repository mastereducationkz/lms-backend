"""Shared logic for completing or resetting lesson progress on behalf of students."""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from src.schemas.models import (
    Course,
    GroupStudent,
    Lesson,
    Module,
    Step,
    StepProgress,
    StudentProgress,
    UserInDB,
)


def _get_steps_query(db: Session, course_id: int, lesson_ids: Optional[List[int]], step_ids: Optional[List[int]]):
    query = db.query(Step).join(Lesson).join(Module).filter(Module.course_id == course_id)

    if step_ids:
        query = query.filter(Step.id.in_(step_ids))
    elif lesson_ids:
        query = query.filter(Lesson.id.in_(lesson_ids))

    return query


def complete_steps_for_user(
    db: Session,
    user_id: int,
    course_id: int,
    lesson_ids: Optional[List[int]] = None,
    step_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    steps = _get_steps_query(db, course_id, lesson_ids, step_ids).all()
    if not steps:
        return {
            "total_steps": 0,
            "newly_completed": 0,
            "updated": 0,
            "already_completed": 0,
            "lessons_marked_complete": 0,
        }

    completed_count = 0
    updated_count = 0
    skipped_count = 0
    affected_lesson_ids = set()

    now = datetime.now(timezone.utc)

    for step in steps:
        affected_lesson_ids.add(step.lesson_id)
        progress = db.query(StepProgress).filter(
            StepProgress.user_id == user_id,
            StepProgress.step_id == step.id,
        ).first()

        if progress:
            if progress.status == "completed":
                skipped_count += 1
            else:
                progress.status = "completed"
                progress.completed_at = now
                if not progress.visited_at:
                    progress.visited_at = now
                updated_count += 1
        else:
            db.add(
                StepProgress(
                    user_id=user_id,
                    course_id=course_id,
                    lesson_id=step.lesson_id,
                    step_id=step.id,
                    status="completed",
                    visited_at=now,
                    completed_at=now,
                    time_spent_minutes=0,
                )
            )
            completed_count += 1

    lessons_marked = 0
    for lesson_id in affected_lesson_ids:
        if _mark_lesson_complete(db, user_id, course_id, lesson_id, now):
            lessons_marked += 1

    return {
        "total_steps": len(steps),
        "newly_completed": completed_count,
        "updated": updated_count,
        "already_completed": skipped_count,
        "lessons_marked_complete": lessons_marked,
    }


def reset_steps_for_user(
    db: Session,
    user_id: int,
    course_id: int,
    lesson_ids: Optional[List[int]] = None,
    step_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    query = db.query(StepProgress).filter(
        StepProgress.user_id == user_id,
        StepProgress.course_id == course_id,
    )

    if step_ids:
        query = query.filter(StepProgress.step_id.in_(step_ids))
    elif lesson_ids:
        query = query.filter(StepProgress.lesson_id.in_(lesson_ids))

    deleted_records = query.delete(synchronize_session=False)

    lesson_query = db.query(StudentProgress).filter(
        StudentProgress.user_id == user_id,
        StudentProgress.course_id == course_id,
    )
    if lesson_ids:
        lesson_query = lesson_query.filter(StudentProgress.lesson_id.in_(lesson_ids))

    deleted_lessons = lesson_query.delete(synchronize_session=False)

    return {
        "deleted_step_records": deleted_records,
        "deleted_lesson_records": deleted_lessons,
    }


def get_user_lesson_progress_summary(
    db: Session,
    user_id: int,
    course_id: int,
) -> Dict[str, Any]:
    user = db.query(UserInDB).filter(UserInDB.id == user_id).first()
    course = db.query(Course).filter(Course.id == course_id).first()

    modules = db.query(Module).filter(Module.course_id == course_id).all()
    lessons_summary = []
    total_steps = 0
    completed_steps = 0

    for module in modules:
        lessons = db.query(Lesson).filter(Lesson.module_id == module.id).order_by(Lesson.order_index).all()

        for lesson in lessons:
            steps = db.query(Step).filter(Step.lesson_id == lesson.id).all()
            lesson_total = len(steps)
            total_steps += lesson_total

            progress_records = db.query(StepProgress).filter(
                StepProgress.user_id == user_id,
                StepProgress.lesson_id == lesson.id,
                StepProgress.status == "completed",
            ).count()

            completed_steps += progress_records
            is_complete = lesson_total > 0 and progress_records >= lesson_total

            lessons_summary.append({
                "lesson_id": lesson.id,
                "lesson_title": lesson.title,
                "module_title": module.title,
                "order_index": lesson.order_index,
                "total_steps": lesson_total,
                "completed_steps": progress_records,
                "completion_percentage": round((progress_records / lesson_total * 100), 1) if lesson_total > 0 else 0,
                "is_complete": is_complete,
            })

    overall_percentage = round((completed_steps / total_steps * 100), 1) if total_steps > 0 else 0

    return {
        "user": {
            "id": user.id if user else user_id,
            "name": user.name if user else "",
            "email": user.email if user else "",
        },
        "course": {
            "id": course.id if course else course_id,
            "title": course.title if course else "",
        },
        "overall": {
            "total_steps": total_steps,
            "completed_steps": completed_steps,
            "completion_percentage": overall_percentage,
        },
        "lessons": lessons_summary,
    }


def get_group_lesson_progress_summary(
    db: Session,
    group_id: int,
    course_id: int,
) -> Dict[str, Any]:
    student_ids = [
        gs.student_id
        for gs in db.query(GroupStudent).filter(GroupStudent.group_id == group_id).all()
    ]

    if not student_ids:
        return {
            "group_id": group_id,
            "course_id": course_id,
            "student_count": 0,
            "lessons": [],
        }

    modules = db.query(Module).filter(Module.course_id == course_id).all()
    lessons_summary = []

    for module in modules:
        lessons = db.query(Lesson).filter(Lesson.module_id == module.id).order_by(Lesson.order_index).all()

        for lesson in lessons:
            steps = db.query(Step).filter(Step.lesson_id == lesson.id).all()
            lesson_total = len(steps)

            completed_students = 0
            if lesson_total > 0:
                for student_id in student_ids:
                    progress_records = db.query(StepProgress).filter(
                        StepProgress.user_id == student_id,
                        StepProgress.lesson_id == lesson.id,
                        StepProgress.status == "completed",
                    ).count()
                    if progress_records >= lesson_total:
                        completed_students += 1

            lessons_summary.append({
                "lesson_id": lesson.id,
                "lesson_title": lesson.title,
                "module_title": module.title,
                "order_index": lesson.order_index,
                "total_steps": lesson_total,
                "student_count": len(student_ids),
                "completed_students": completed_students,
                "completion_percentage": round((completed_students / len(student_ids) * 100), 1),
                "is_complete": completed_students == len(student_ids),
            })

    return {
        "group_id": group_id,
        "course_id": course_id,
        "student_count": len(student_ids),
        "lessons": lessons_summary,
    }


def _mark_lesson_complete(
    db: Session,
    user_id: int,
    course_id: int,
    lesson_id: int,
    now: datetime,
) -> bool:
    lesson_steps = db.query(Step).filter(Step.lesson_id == lesson_id).count()
    if lesson_steps == 0:
        return False

    completed_steps = db.query(StepProgress).filter(
        StepProgress.user_id == user_id,
        StepProgress.lesson_id == lesson_id,
        StepProgress.status == "completed",
    ).count()

    if completed_steps < lesson_steps:
        return False

    progress = db.query(StudentProgress).filter(
        StudentProgress.user_id == user_id,
        StudentProgress.lesson_id == lesson_id,
    ).first()

    if progress:
        if progress.status == "completed" and progress.completion_percentage == 100:
            return False
        progress.status = "completed"
        progress.completion_percentage = 100
        progress.last_accessed = now
        progress.completed_at = now
        return True

    db.add(
        StudentProgress(
            user_id=user_id,
            course_id=course_id,
            lesson_id=lesson_id,
            status="completed",
            completion_percentage=100,
            time_spent_minutes=0,
            last_accessed=now,
            completed_at=now,
        )
    )
    return True

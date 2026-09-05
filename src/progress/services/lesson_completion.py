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


def _get_completed_lesson_ids_for_user(
    db: Session,
    user_id: int,
    course_id: int,
) -> set[int]:
    rows = db.query(StudentProgress.lesson_id).filter(
        StudentProgress.user_id == user_id,
        StudentProgress.course_id == course_id,
        StudentProgress.lesson_id.isnot(None),
        StudentProgress.status == "completed",
    ).all()
    return {row[0] for row in rows}


def _is_lesson_complete_for_user(
    lesson_id: int,
    lesson_total: int,
    completed_step_count: int,
    completed_lesson_ids: set[int],
) -> bool:
    if lesson_id in completed_lesson_ids:
        return True
    return lesson_total > 0 and completed_step_count >= lesson_total


def calculate_student_module_progress(
    db: Session,
    user_id: int,
    course_id: int,
) -> Dict[str, Any]:
    """
    Course progress by units (lessons): completed lessons / total lessons.
    Current unit = first incomplete lesson with step-level progress.
    """
    completed_lesson_ids = _get_completed_lesson_ids_for_user(db, user_id, course_id)
    modules = (
        db.query(Module)
        .filter(Module.course_id == course_id)
        .order_by(Module.order_index)
        .all()
    )

    total_lessons = 0
    completed_lessons = 0
    current_lesson_title: Optional[str] = None
    current_lesson_id: Optional[int] = None
    current_lesson_progress = 0
    last_lesson_title: Optional[str] = None
    last_lesson_id: Optional[int] = None

    for module in modules:
        lessons = (
            db.query(Lesson)
            .filter(Lesson.module_id == module.id)
            .order_by(Lesson.order_index)
            .all()
        )

        for lesson in lessons:
            if lesson.kind == "checkpoint":
                continue
            # Only required (non-optional) steps count toward completion
            lesson_total = db.query(Step).filter(
                Step.lesson_id == lesson.id,
                Step.is_optional == False,  # noqa: E712
            ).count()
            if lesson_total == 0:
                # fallback to all steps if none are marked required
                lesson_total = db.query(Step).filter(Step.lesson_id == lesson.id).count()
            if lesson_total == 0:
                continue

            total_lessons += 1
            last_lesson_title = lesson.title
            last_lesson_id = lesson.id

            required_step_ids = [
                s.id for s in db.query(Step).filter(
                    Step.lesson_id == lesson.id,
                    Step.is_optional == False,  # noqa: E712
                ).all()
            ] or [s.id for s in db.query(Step).filter(Step.lesson_id == lesson.id).all()]

            completed_steps = db.query(StepProgress).filter(
                StepProgress.user_id == user_id,
                StepProgress.step_id.in_(required_step_ids),
                StepProgress.status == "completed",
            ).count()
            is_complete = _is_lesson_complete_for_user(
                lesson.id,
                lesson_total,
                completed_steps,
                completed_lesson_ids,
            )

            if is_complete:
                completed_lessons += 1
            elif current_lesson_title is None:
                current_lesson_title = lesson.title
                current_lesson_id = lesson.id
                current_lesson_progress = round((completed_steps / lesson_total) * 100)

    if current_lesson_title is None and total_lessons > 0:
        current_lesson_title = last_lesson_title
        current_lesson_id = last_lesson_id
        current_lesson_progress = 100

    overall_progress = (
        round((completed_lessons / total_lessons) * 100)
        if total_lessons > 0 else 0
    )

    return {
        "overall_progress": overall_progress,
        "completed_modules": completed_lessons,
        "total_modules": total_lessons,
        "current_module_title": current_lesson_title,
        "current_module_id": current_lesson_id,
        "current_module_progress": current_lesson_progress,
    }


def calculate_module_progress_for_students(
    db: Session,
    student_ids: List[int],
    course_id: int,
) -> Dict[int, Dict[str, Any]]:
    """
    Batched equivalent of calculate_student_module_progress() for many students
    against the SAME course. The course structure (modules/lessons/steps) is
    identical for every student, so it is loaded ONCE here instead of once per
    student, eliminating the N+1 query pattern. The per-student results are
    computed with the exact same semantics as calculate_student_module_progress.
    """
    if not student_ids:
        return {}

    student_ids = list(dict.fromkeys(student_ids))  # de-dup, preserve order

    # 1) Course structure, loaded once.
    modules = (
        db.query(Module)
        .filter(Module.course_id == course_id)
        .order_by(Module.order_index)
        .all()
    )
    module_ids = [m.id for m in modules]

    lessons_by_module: Dict[int, List[Lesson]] = {mid: [] for mid in module_ids}
    if module_ids:
        all_lessons = (
            db.query(Lesson)
            .filter(Lesson.module_id.in_(module_ids))
            .order_by(Lesson.order_index)
            .all()
        )
        for lesson in all_lessons:
            lessons_by_module.setdefault(lesson.module_id, []).append(lesson)

    all_lesson_ids = [
        lesson.id for lessons in lessons_by_module.values() for lesson in lessons
    ]

    steps_by_lesson: Dict[int, List[Step]] = {lid: [] for lid in all_lesson_ids}
    if all_lesson_ids:
        all_steps = (
            db.query(Step)
            .filter(Step.lesson_id.in_(all_lesson_ids))
            .all()
        )
        for step in all_steps:
            steps_by_lesson.setdefault(step.lesson_id, []).append(step)

    # Ordered list of (lesson, lesson_total, counting_step_ids) mirroring the
    # exact required-vs-all-steps fallback used by calculate_student_module_progress,
    # in the same module/lesson traversal order.
    lesson_specs: List[Dict[str, Any]] = []
    step_id_to_lesson_id: Dict[int, int] = {}
    for module in modules:
        for lesson in lessons_by_module.get(module.id, []):
            if lesson.kind == "checkpoint":
                continue
            steps = steps_by_lesson.get(lesson.id, [])
            required_step_ids = [s.id for s in steps if not s.is_optional]
            if not required_step_ids:
                required_step_ids = [s.id for s in steps]
            lesson_total = len(required_step_ids)
            if lesson_total == 0:
                continue

            lesson_specs.append({
                "lesson_id": lesson.id,
                "lesson_title": lesson.title,
                "lesson_total": lesson_total,
            })
            for step_id in required_step_ids:
                step_id_to_lesson_id[step_id] = lesson.id

    all_counting_step_ids = list(step_id_to_lesson_id.keys())

    # 2) Per-student completed-step counts, in one query.
    completed_counts: Dict[int, Dict[int, int]] = {sid: {} for sid in student_ids}
    if all_counting_step_ids:
        rows = (
            db.query(StepProgress.user_id, StepProgress.step_id)
            .filter(
                StepProgress.user_id.in_(student_ids),
                StepProgress.step_id.in_(all_counting_step_ids),
                StepProgress.status == "completed",
            )
            .all()
        )
        for user_id, step_id in rows:
            lesson_id = step_id_to_lesson_id.get(step_id)
            if lesson_id is None:
                continue
            lesson_counts = completed_counts.setdefault(user_id, {})
            lesson_counts[lesson_id] = lesson_counts.get(lesson_id, 0) + 1

    # 3) Per-student completed lesson ids (StudentProgress), in one query.
    completed_lesson_ids_by_user: Dict[int, set] = {sid: set() for sid in student_ids}
    lesson_progress_rows = (
        db.query(StudentProgress.user_id, StudentProgress.lesson_id)
        .filter(
            StudentProgress.user_id.in_(student_ids),
            StudentProgress.course_id == course_id,
            StudentProgress.lesson_id.isnot(None),
            StudentProgress.status == "completed",
        )
        .all()
    )
    for user_id, lesson_id in lesson_progress_rows:
        completed_lesson_ids_by_user.setdefault(user_id, set()).add(lesson_id)

    # 4) Compute the per-student result using the shared structure.
    results: Dict[int, Dict[str, Any]] = {}
    for student_id in student_ids:
        completed_lesson_ids = completed_lesson_ids_by_user.get(student_id, set())
        student_completed = completed_counts.get(student_id, {})

        total_lessons = 0
        completed_lessons = 0
        current_lesson_title: Optional[str] = None
        current_lesson_id: Optional[int] = None
        current_lesson_progress = 0
        last_lesson_title: Optional[str] = None
        last_lesson_id: Optional[int] = None

        for spec in lesson_specs:
            lesson_id = spec["lesson_id"]
            lesson_total = spec["lesson_total"]

            total_lessons += 1
            last_lesson_title = spec["lesson_title"]
            last_lesson_id = lesson_id

            completed_steps = student_completed.get(lesson_id, 0)
            is_complete = _is_lesson_complete_for_user(
                lesson_id,
                lesson_total,
                completed_steps,
                completed_lesson_ids,
            )

            if is_complete:
                completed_lessons += 1
            elif current_lesson_title is None:
                current_lesson_title = spec["lesson_title"]
                current_lesson_id = lesson_id
                current_lesson_progress = round((completed_steps / lesson_total) * 100)

        if current_lesson_title is None and total_lessons > 0:
            current_lesson_title = last_lesson_title
            current_lesson_id = last_lesson_id
            current_lesson_progress = 100

        overall_progress = (
            round((completed_lessons / total_lessons) * 100)
            if total_lessons > 0 else 0
        )

        results[student_id] = {
            "overall_progress": overall_progress,
            "completed_modules": completed_lessons,
            "total_modules": total_lessons,
            "current_module_title": current_lesson_title,
            "current_module_id": current_lesson_id,
            "current_module_progress": current_lesson_progress,
        }

    return results


def get_user_lesson_progress_summary(
    db: Session,
    user_id: int,
    course_id: int,
) -> Dict[str, Any]:
    user = db.query(UserInDB).filter(UserInDB.id == user_id).first()
    course = db.query(Course).filter(Course.id == course_id).first()
    completed_lesson_ids = _get_completed_lesson_ids_for_user(db, user_id, course_id)

    modules = db.query(Module).filter(Module.course_id == course_id).all()
    lessons_summary = []
    total_steps = 0
    completed_steps = 0

    for module in modules:
        lessons = db.query(Lesson).filter(Lesson.module_id == module.id).order_by(Lesson.order_index).all()

        for lesson in lessons:
            is_checkpoint = lesson.kind == "checkpoint"
            all_steps = db.query(Step).filter(Step.lesson_id == lesson.id).all()
            required_steps = [s for s in all_steps if not s.is_optional]
            lesson_total = len(required_steps) if required_steps else len(all_steps)
            if not is_checkpoint:
                total_steps += lesson_total

            step_ids = [s.id for s in (required_steps if required_steps else all_steps)]
            progress_records = db.query(StepProgress).filter(
                StepProgress.user_id == user_id,
                StepProgress.step_id.in_(step_ids),
                StepProgress.status == "completed",
            ).count() if step_ids else 0

            is_complete = _is_lesson_complete_for_user(
                lesson.id,
                lesson_total,
                progress_records,
                completed_lesson_ids,
            )

            display_completed_steps = lesson_total if is_complete and lesson_total > 0 else progress_records
            if not is_checkpoint:
                completed_steps += display_completed_steps if is_complete else progress_records

            if is_complete:
                completion_percentage = 100.0
            else:
                completion_percentage = (
                    round((progress_records / lesson_total * 100), 1) if lesson_total > 0 else 0
                )

            lessons_summary.append({
                "lesson_id": lesson.id,
                "lesson_title": lesson.title,
                "module_title": module.title,
                "order_index": lesson.order_index,
                "total_steps": lesson_total,
                "completed_steps": display_completed_steps,
                "completion_percentage": completion_percentage,
                "is_complete": is_complete,
                "kind": lesson.kind,
            })

    unit_lessons_summary = [lesson for lesson in lessons_summary if lesson["kind"] != "checkpoint"]
    overall_percentage = round((completed_steps / total_steps * 100), 1) if total_steps > 0 else 0
    completed_lesson_count = sum(1 for lesson in unit_lessons_summary if lesson["is_complete"])

    if total_steps == 0 and unit_lessons_summary:
        overall_percentage = round((completed_lesson_count / len(unit_lessons_summary) * 100), 1)

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

    completed_lessons_by_user = {
        student_id: _get_completed_lesson_ids_for_user(db, student_id, course_id)
        for student_id in student_ids
    }

    modules = db.query(Module).filter(Module.course_id == course_id).all()
    lessons_summary = []

    for module in modules:
        lessons = db.query(Lesson).filter(Lesson.module_id == module.id).order_by(Lesson.order_index).all()

        for lesson in lessons:
            steps = db.query(Step).filter(Step.lesson_id == lesson.id).all()
            lesson_total = len(steps)

            completed_students = 0
            for student_id in student_ids:
                progress_records = db.query(StepProgress).filter(
                    StepProgress.user_id == student_id,
                    StepProgress.lesson_id == lesson.id,
                    StepProgress.status == "completed",
                ).count()

                if _is_lesson_complete_for_user(
                    lesson.id,
                    lesson_total,
                    progress_records,
                    completed_lessons_by_user.get(student_id, set()),
                ):
                    completed_students += 1

            student_count = len(student_ids)
            completion_percentage = (
                round((completed_students / student_count * 100), 1) if student_count > 0 else 0
            )

            lessons_summary.append({
                "lesson_id": lesson.id,
                "lesson_title": lesson.title,
                "module_title": module.title,
                "order_index": lesson.order_index,
                "total_steps": lesson_total,
                "student_count": student_count,
                "completed_students": completed_students,
                "completion_percentage": completion_percentage,
                "is_complete": student_count > 0 and completed_students == student_count,
                "kind": lesson.kind,
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
    # Only non-optional steps are required for lesson completion
    required_steps = db.query(Step).filter(
        Step.lesson_id == lesson_id,
        Step.is_optional == False,  # noqa: E712
    ).count()

    # If there are no required steps, check total steps (edge case)
    if required_steps == 0:
        required_steps = db.query(Step).filter(Step.lesson_id == lesson_id).count()
    if required_steps == 0:
        return False

    required_step_ids = [
        s.id for s in db.query(Step).filter(
            Step.lesson_id == lesson_id,
            Step.is_optional == False,  # noqa: E712
        ).all()
    ] or [s.id for s in db.query(Step).filter(Step.lesson_id == lesson_id).all()]

    completed_steps = db.query(StepProgress).filter(
        StepProgress.user_id == user_id,
        StepProgress.step_id.in_(required_step_ids),
        StepProgress.status == "completed",
    ).count()

    if completed_steps < required_steps:
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

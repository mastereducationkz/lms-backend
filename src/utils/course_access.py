"""
Shared utilities for course access logic.

This module consolidates duplicated course access logic from various route handlers
into reusable functions, eliminating code duplication and ensuring consistency.
"""
from datetime import date
from typing import List, Optional, Tuple
from sqlalchemy import or_, asc
from sqlalchemy.orm import Session, joinedload

from src.schemas.models import (
    Course,
    Enrollment,
    GroupStudent,
    CourseGroupAccess,
    UserInDB,
    Group,
    Module,
    Lesson,
)


def get_user_courses(
    user_id: int,
    db: Session,
    include_inactive: bool = False
) -> List[Course]:
    """
    Get all courses accessible by a user via enrollment or group access.
    
    This is the canonical function for determining course access and should be
    used throughout the codebase instead of duplicating this logic.
    
    Args:
        user_id: The user's ID
        db: Database session
        include_inactive: If True, include inactive courses
        
    Returns:
        List of Course objects the user can access
    """
    # Get courses via direct enrollment
    enrollment_course_ids = db.query(Enrollment.course_id).filter(
        Enrollment.user_id == user_id,
        Enrollment.is_active == True
    ).subquery()
    
    # Get courses via group access
    user_group_ids = db.query(GroupStudent.group_id).filter(
        GroupStudent.student_id == user_id
    ).subquery()
    
    group_course_ids = db.query(CourseGroupAccess.course_id).filter(
        CourseGroupAccess.group_id.in_(user_group_ids),
        CourseGroupAccess.is_active == True
    ).subquery()
    
    # Combine and query courses
    query = db.query(Course).filter(
        or_(
            Course.id.in_(enrollment_course_ids),
            Course.id.in_(group_course_ids)
        )
    )
    
    if not include_inactive:
        query = query.filter(Course.is_active == True)
    
    return query.all()


def get_user_course_ids(user_id: int, db: Session) -> List[int]:
    """
    Get list of course IDs accessible by a user.
    
    Lightweight version of get_user_courses when you only need IDs.
    """
    # Via enrollment
    enrollment_ids = set(
        r[0] for r in db.query(Enrollment.course_id).filter(
            Enrollment.user_id == user_id,
            Enrollment.is_active == True
        ).all()
    )
    
    # Via group access
    group_ids = [
        r[0] for r in db.query(GroupStudent.group_id).filter(
            GroupStudent.student_id == user_id
        ).all()
    ]
    
    group_course_ids = set(
        r[0] for r in db.query(CourseGroupAccess.course_id).filter(
            CourseGroupAccess.group_id.in_(group_ids),
            CourseGroupAccess.is_active == True
        ).all()
    ) if group_ids else set()
    
    return list(enrollment_ids | group_course_ids)


def check_user_course_access(
    user_id: int,
    course_id: int,
    db: Session
) -> bool:
    """
    Check if a user has access to a specific course.
    
    Args:
        user_id: The user's ID
        course_id: The course ID to check
        db: Database session
        
    Returns:
        True if user has access, False otherwise
    """
    # Check enrollment
    has_enrollment = db.query(Enrollment).filter(
        Enrollment.user_id == user_id,
        Enrollment.course_id == course_id,
        Enrollment.is_active == True
    ).first() is not None
    
    if has_enrollment:
        return True
    
    # Check group access
    user_group_ids = [
        r[0] for r in db.query(GroupStudent.group_id).filter(
            GroupStudent.student_id == user_id
        ).all()
    ]
    
    if not user_group_ids:
        return False
    
    has_group_access = db.query(CourseGroupAccess).filter(
        CourseGroupAccess.course_id == course_id,
        CourseGroupAccess.group_id.in_(user_group_ids),
        CourseGroupAccess.is_active == True
    ).first() is not None
    
    return has_group_access


def get_ordered_lesson_ids_for_course(course_id: int, db: Session) -> List[int]:
    rows = (
        db.query(Lesson.id)
        .join(Module, Lesson.module_id == Module.id)
        .filter(Module.course_id == course_id)
        .order_by(asc(Module.order_index), asc(Lesson.order_index))
        .all()
    )
    return [r[0] for r in rows]


def get_lesson_global_index(lesson_id: int, course_id: int, db: Session) -> Optional[int]:
    ids = get_ordered_lesson_ids_for_course(course_id, db)
    try:
        return ids.index(lesson_id)
    except ValueError:
        return None


def get_lesson_index_in_module(lesson_id: int, module_id: int, db: Session) -> Optional[int]:
    """0-based position among lessons in the module (by order_index)."""
    rows = (
        db.query(Lesson.id)
        .filter(Lesson.module_id == module_id)
        .order_by(asc(Lesson.order_index))
        .all()
    )
    ids = [r[0] for r in rows]
    try:
        return ids.index(lesson_id)
    except ValueError:
        return None


def student_has_enrollment_for_course(user_id: int, course_id: int, db: Session) -> bool:
    return (
        db.query(Enrollment)
        .filter(
            Enrollment.user_id == user_id,
            Enrollment.course_id == course_id,
            Enrollment.is_active == True,
        )
        .first()
        is not None
    )


def student_can_see_homework_for_course(user_id: int, course_id: int, db: Session) -> bool:
    """
    Homework is allowed if the student has a non-special group grant to the course,
    or an individual enrollment (enrollment does not count as 'non-special group' for lesson caps,
    but keeps homework for enrolled-only students).
    """
    if student_has_nonspecial_group_access_to_course(user_id, course_id, db):
        return True
    return student_has_enrollment_for_course(user_id, course_id, db)


def student_has_only_special_groups(user_id: int, db: Session) -> bool:
    """
    True if the student is in at least one active group and every active group is special.
    Matches frontend Sidebar / get_my_groups (active groups only).
    """
    rows = (
        db.query(Group.is_special)
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .filter(
            GroupStudent.student_id == user_id,
            Group.is_active == True,
        )
        .all()
    )
    if not rows:
        return False
    return all(r[0] for r in rows)


def student_has_nonspecial_group_access_to_course(user_id: int, course_id: int, db: Session) -> bool:
    """If True, full course rules apply (no special cap, homework allowed via normal rules)."""
    rows = (
        db.query(Group.is_special)
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .join(CourseGroupAccess, CourseGroupAccess.group_id == Group.id)
        .filter(
            GroupStudent.student_id == user_id,
            CourseGroupAccess.course_id == course_id,
            CourseGroupAccess.is_active == True,
        )
        .all()
    )
    if not rows:
        return False
    return any(not r[0] for r in rows)


def get_special_lesson_cap_for_student_course(user_id: int, course_id: int, db: Session) -> Optional[int]:
    """
    When the student has course access only via special groups, return the minimum
    max_open_lessons among those accesses (None in DB = unlimited for that row).
    The cap applies per module: only the first N lessons (by order) in each module are open.
    Returns None if no cap applies.
    """
    if student_has_nonspecial_group_access_to_course(user_id, course_id, db):
        return None
    caps: List[int] = []
    rows = (
        db.query(CourseGroupAccess.max_open_lessons, Group.is_special)
        .join(Group, Group.id == CourseGroupAccess.group_id)
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .filter(
            GroupStudent.student_id == user_id,
            CourseGroupAccess.course_id == course_id,
            CourseGroupAccess.is_active == True,
        )
        .all()
    )
    for max_open, is_sp in rows:
        if not is_sp:
            continue
        if max_open is not None:
            caps.append(max_open)
    if not caps:
        return None
    return min(caps)


def lesson_blocked_by_special_cap(
    user_id: int, course_id: int, lesson_id: int, db: Session
) -> Tuple[bool, Optional[str]]:
    cap = get_special_lesson_cap_for_student_course(user_id, course_id, db)
    if cap is None:
        return False, None
    lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
    if not lesson:
        return True, "Lesson not found"
    idx = get_lesson_index_in_module(lesson_id, lesson.module_id, db)
    if idx is None:
        return True, "Lesson not found in module order"
    if idx >= cap:
        return True, f"Only the first {cap} lessons per module are open for your group"
    return False, None


def calc_program_week_from_start_date(start_date_str: Optional[str], reference_date: Optional[date] = None) -> Optional[int]:
    """
    Calculate program week (1-based) from group schedule_config start_date.

    Args:
        start_date_str: ISO date string from schedule_config["start_date"]
        reference_date: Date to calculate relative to. Defaults to today.

    Returns:
        Week number (1, 2, 3...) or None if not started or invalid.
    """
    if not start_date_str:
        return None
    try:
        start = date.fromisoformat(start_date_str)
        ref = reference_date if reference_date is not None else date.today()
        delta = (ref - start).days
        if delta < 0:
            return None
        return delta // 7 + 1
    except (ValueError, TypeError):
        return None


def get_courses_with_teacher(
    user_id: int,
    db: Session
) -> List[dict]:
    """
    Get courses with teacher info for a user.
    
    Returns course data with teacher name pre-loaded to avoid N+1 queries.
    """
    courses = get_user_courses(user_id, db)
    
    # Batch load teacher info
    teacher_ids = list(set(c.teacher_id for c in courses if c.teacher_id))
    teachers = {
        t.id: t.name
        for t in db.query(UserInDB).filter(UserInDB.id.in_(teacher_ids)).all()
    } if teacher_ids else {}
    
    return [
        {
            "id": c.id,
            "title": c.title,
            "description": c.description,
            "cover_image_url": c.cover_image_url,
            "teacher_id": c.teacher_id,
            "teacher_name": teachers.get(c.teacher_id, "Unknown"),
            "is_linear": getattr(c, "is_linear", False)
        }
        for c in courses
    ]

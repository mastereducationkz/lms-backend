"""
Shared utilities for course access logic.

This module consolidates duplicated course access logic from various route handlers
into reusable functions, eliminating code duplication and ensuring consistency.
"""
from datetime import date
from typing import List, Optional
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from src.schemas.models import (
    Course, Enrollment, GroupStudent, CourseGroupAccess, UserInDB
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

"""
Service for maintaining cached progress summaries.

This service updates StudentCourseSummary and CourseAnalyticsCache
tables when progress changes occur, keeping cached data in sync.
"""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.schemas.models import (
    StudentCourseSummary, CourseAnalyticsCache,
    Step, Lesson, Module, StepProgress, Assignment, AssignmentSubmission
)


def update_student_course_summary(
    user_id: int,
    course_id: int,
    db: Session,
    time_spent_delta: int = 0,
    step_completed: bool = False,
    lesson_id: Optional[int] = None,
    lesson_title: Optional[str] = None
) -> StudentCourseSummary:
    """
    Update or create a student's course summary cache.
    
    Called when:
    - A step is completed
    - An assignment is graded
    - Progress is recorded
    
    Args:
        user_id: Student's user ID
        course_id: Course ID
        db: Database session
        time_spent_delta: Additional time spent to add (minutes)
        step_completed: Whether a step was just completed
        lesson_id: Current lesson ID (for last activity tracking)
        lesson_title: Current lesson title
        
    Returns:
        Updated StudentCourseSummary object
    """
    # Get or create summary
    summary = db.query(StudentCourseSummary).filter(
        StudentCourseSummary.user_id == user_id,
        StudentCourseSummary.course_id == course_id
    ).first()
    
    if not summary:
        # Get total steps for this course
        total_steps = db.query(func.count(Step.id)).join(Lesson).join(Module).filter(
            Module.course_id == course_id
        ).scalar() or 0
        
        # Get total assignments
        total_assignments = db.query(func.count(Assignment.id)).join(
            Lesson
        ).join(Module).filter(
            Module.course_id == course_id,
            Assignment.is_active == True
        ).scalar() or 0
        
        summary = StudentCourseSummary(
            user_id=user_id,
            course_id=course_id,
            total_steps=total_steps,
            total_assignments=total_assignments,
            completed_steps=0,
            completed_assignments=0,
            total_time_spent_minutes=0
        )
        db.add(summary)
    
    # Normalize potentially None values from existing records
    if summary.completed_steps is None: summary.completed_steps = 0
    if summary.total_steps is None: summary.total_steps = 0
    if summary.total_time_spent_minutes is None: summary.total_time_spent_minutes = 0
    
    # Increment counters
    if step_completed:
        summary.completed_steps += 1
        summary.completion_percentage = (
            (summary.completed_steps / summary.total_steps * 100)
            if summary.total_steps > 0 else 0
        )
    
    if time_spent_delta > 0:
        summary.total_time_spent_minutes += time_spent_delta
    
    # Update last activity
    summary.last_activity_at = datetime.now(timezone.utc)
    if lesson_id:
        summary.last_lesson_id = lesson_id
    if lesson_title:
        summary.last_lesson_title = lesson_title
    
    summary.updated_at = datetime.now(timezone.utc)
    
    return summary


def update_summary_for_assignment(
    user_id: int,
    course_id: int,
    score: float,
    max_score: float,
    db: Session
):
    """
    Update summary when an assignment is graded.
    
    Args:
        user_id: Student's user ID
        course_id: Course ID
        score: Score received
        max_score: Maximum possible score
        db: Database session
    """
    summary = db.query(StudentCourseSummary).filter(
        StudentCourseSummary.user_id == user_id,
        StudentCourseSummary.course_id == course_id
    ).first()
    
    if not summary:
        # Create new summary (shouldn't normally happen)
        summary = update_student_course_summary(user_id, course_id, db)
    
    # Normalize potentially None values from existing records
    if summary.completed_assignments is None: summary.completed_assignments = 0
    if summary.total_assignment_score is None: summary.total_assignment_score = 0.0
    if summary.max_possible_score is None: summary.max_possible_score = 0.0
    
    # Update assignment metrics
    summary.completed_assignments += 1
    summary.total_assignment_score += score
    summary.max_possible_score += max_score
    
    if summary.max_possible_score > 0:
        summary.average_assignment_percentage = (
            summary.total_assignment_score / summary.max_possible_score * 100
        )
    
    summary.updated_at = datetime.now(timezone.utc)


def recalculate_student_summary(
    user_id: int,
    course_id: int,
    db: Session
) -> StudentCourseSummary:
    """
    Fully recalculate a student's course summary from raw data.
    
    Use this when incremental updates might be out of sync.
    """
    # Calculate metrics from raw data
    total_steps = db.query(func.count(Step.id)).join(Lesson).join(Module).filter(
        Module.course_id == course_id
    ).scalar() or 0
    
    completed_steps = db.query(func.count(StepProgress.id)).join(
        Step, StepProgress.step_id == Step.id
    ).join(Lesson, Step.lesson_id == Lesson.id).join(
        Module, Lesson.module_id == Module.id
    ).filter(
        StepProgress.user_id == user_id,
        Module.course_id == course_id,
        StepProgress.status == "completed"
    ).scalar() or 0
    
    total_time = db.query(func.sum(StepProgress.time_spent_minutes)).join(
        Step, StepProgress.step_id == Step.id
    ).join(Lesson, Step.lesson_id == Lesson.id).join(
        Module, Lesson.module_id == Module.id
    ).filter(
        StepProgress.user_id == user_id,
        Module.course_id == course_id
    ).scalar() or 0
    
    # Assignment metrics
    assignment_stats = db.query(
        func.count(AssignmentSubmission.id),
        func.sum(AssignmentSubmission.score),
        func.sum(AssignmentSubmission.max_score)
    ).join(Assignment).join(Lesson).join(Module).filter(
        AssignmentSubmission.user_id == user_id,
        Module.course_id == course_id,
        AssignmentSubmission.is_graded == True,
        AssignmentSubmission.is_current == True,
    ).first()
    
    total_assignments = db.query(func.count(Assignment.id)).join(
        Lesson
    ).join(Module).filter(
        Module.course_id == course_id,
        Assignment.is_active == True
    ).scalar() or 0
    
    # Get or create summary
    summary = db.query(StudentCourseSummary).filter(
        StudentCourseSummary.user_id == user_id,
        StudentCourseSummary.course_id == course_id
    ).first()
    
    if not summary:
        summary = StudentCourseSummary(
            user_id=user_id,
            course_id=course_id
        )
        db.add(summary)
    
    # Update all fields
    summary.total_steps = total_steps
    summary.completed_steps = completed_steps
    summary.completion_percentage = (completed_steps / total_steps * 100) if total_steps > 0 else 0
    summary.total_time_spent_minutes = total_time
    summary.total_assignments = total_assignments
    summary.completed_assignments = assignment_stats[0] or 0
    summary.total_assignment_score = assignment_stats[1] or 0
    summary.max_possible_score = assignment_stats[2] or 0
    summary.average_assignment_percentage = (
        (summary.total_assignment_score / summary.max_possible_score * 100)
        if summary.max_possible_score > 0 else 0
    )
    summary.updated_at = datetime.now(timezone.utc)
    
    return summary

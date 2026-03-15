from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, and_, or_
from typing import List, Optional
from datetime import datetime, timedelta
import json

from src.config import get_db
from src.schemas.models import (
    UserInDB, Course, Module, Lesson, Enrollment, StudentProgress,
    DashboardStatsSchema, CourseProgressSchema, UserSchema, Step, StepProgress
)
from src.routes.auth import get_current_user_dependency
from src.utils.permissions import require_role
from src.schemas.models import GroupStudent
from src.services.attendance_service import AttendanceService

router = APIRouter()

@router.get("/stats", response_model=DashboardStatsSchema)
async def get_dashboard_stats(
    group_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Get dashboard statistics for current user
    Supports different views based on user role
    """
    if current_user.role == "student":
        return get_student_dashboard_stats(current_user, db)
    elif current_user.role == "teacher":
        return get_teacher_dashboard_stats(current_user, db)
    elif current_user.role == "curator":
        return get_curator_dashboard_stats(current_user, db, group_id, start_date, end_date)
    elif current_user.role == "head_curator":
        return get_head_curator_dashboard_stats(current_user, db, group_id, start_date, end_date)
    elif current_user.role == "admin":
        return get_admin_dashboard_stats(current_user, db)
    elif current_user.role == "head_teacher":
        return get_head_teacher_dashboard_stats(current_user, db)
    else:
        raise HTTPException(status_code=403, detail="Invalid user role")

def get_student_dashboard_stats(user: UserInDB, db: Session) -> DashboardStatsSchema:
    """Get dashboard stats for student"""
    # Get student's active enrollments
    enrollments = db.query(Enrollment).filter(
        Enrollment.user_id == user.id,
        Enrollment.is_active == True
    ).all()
    
    # Get group access courses
    from src.schemas.models import GroupStudent, CourseGroupAccess
    
    group_student = db.query(GroupStudent).filter(
        GroupStudent.student_id == user.id
    ).first()
    
    group_courses = []
    if group_student:
        group_access = db.query(CourseGroupAccess).filter(
            CourseGroupAccess.group_id == group_student.group_id,
            CourseGroupAccess.is_active == True
        ).all()
        
        for access in group_access:
            course = db.query(Course).filter(
                Course.id == access.course_id,
                Course.is_active == True
            ).first()
            if course:
                group_courses.append(course)
    
    # Combine both sets of courses (enrollment + group access)
    all_courses = []
    
    # Add enrollment courses
    for enrollment in enrollments:
        course = db.query(Course).filter(
            Course.id == enrollment.course_id,
            Course.is_active == True
        ).first()
        if course:
            all_courses.append(course)
    
    # Add group access courses (avoid duplicates)
    enrollment_course_ids = [e.course_id for e in enrollments]
    for course in group_courses:
        if course.id not in enrollment_course_ids:
            all_courses.append(course)
    
    enrolled_courses_count = len(all_courses)
    
    # Calculate total study time (convert minutes to hours)
    total_study_time_hours = (user.total_study_time_minutes or 0) // 60
    
    # Calculate average progress across all courses using StepProgress
    total_progress = 0
    course_progresses = []
    
    for course in all_courses:
        # Get all steps in this course
        total_steps = db.query(Step).join(Lesson).join(Module).filter(
            Module.course_id == course.id
        ).count()
        
        # Get completed steps for this user in this course
        completed_steps = db.query(StepProgress).filter(
            StepProgress.user_id == user.id,
            StepProgress.course_id == course.id,
            StepProgress.status == "completed"
        ).count()
        
        # Calculate progress percentage
        if total_steps > 0:
            course_avg_progress = (completed_steps / total_steps) * 100
        else:
            course_avg_progress = 0
        
        total_progress += course_avg_progress
        
        # Get teacher info
        teacher = db.query(UserInDB).filter(UserInDB.id == course.teacher_id).first()
        teacher_name = teacher.name if teacher else "Unknown Teacher"
        
        # Count total modules in course
        total_modules = db.query(Module).filter(Module.course_id == course.id).count()
        
        # Get last accessed time from StepProgress
        last_step_progress = db.query(StepProgress).filter(
            StepProgress.user_id == user.id,
            StepProgress.course_id == course.id
        ).order_by(desc(StepProgress.visited_at)).first()
        
        course_progresses.append({
            "id": course.id,
            "title": course.title,
            "cover_image": course.cover_image_url,
            "teacher": teacher_name,
            "total_modules": total_modules,
            "progress": round(course_avg_progress),
            "status": "completed" if course_avg_progress >= 100 else "in_progress" if course_avg_progress > 0 else "not_started",
            "last_accessed": last_step_progress.visited_at if (last_step_progress and last_step_progress.visited_at) else None
        })
    
    # Calculate overall average progress
    average_progress = round(total_progress / enrolled_courses_count) if enrolled_courses_count > 0 else 0
    
    # Sort courses by last accessed (most recent first), handling None values
    # Courses with None last_accessed (never accessed) will be placed at the end
    course_progresses.sort(
        key=lambda x: x["last_accessed"] if x["last_accessed"] is not None else datetime.min, 
        reverse=True
    )
    
    return DashboardStatsSchema(
        user={
            "name": user.name.split()[0],  # First name only like "Fikrat"
            "full_name": user.name,
            "role": user.role,
            "avatar_url": user.avatar_url
        },
        stats={
            "enrolled_courses": enrolled_courses_count,
            "total_study_time_hours": total_study_time_hours,
            "average_progress": average_progress
        },
        recent_courses=course_progresses[:6]  # Limit to 6 recent courses
    )

def get_teacher_dashboard_stats(user: UserInDB, db: Session) -> DashboardStatsSchema:
    """Get dashboard stats for teacher"""
    from src.schemas.models import Assignment, AssignmentSubmission, CourseGroupAccess, Group
    
    # Get teacher's groups (groups where this teacher is the owner)
    teacher_groups = db.query(Group).filter(
        Group.teacher_id == user.id,
        Group.is_active == True
    ).all()
    
    teacher_group_ids = [g.id for g in teacher_groups] if teacher_groups else []
    
    # Get students from teacher's groups
    student_ids_set = set()
    if teacher_group_ids:
        group_students = db.query(GroupStudent).filter(
            GroupStudent.group_id.in_(teacher_group_ids)
        ).all()
        for gs in group_students:
            student_ids_set.add(gs.student_id)
    
    total_students = len(student_ids_set)
    
    # Get courses that have access for teacher's groups
    course_ids_with_access = []
    if teacher_group_ids:
        course_ids_with_access = db.query(CourseGroupAccess.course_id).filter(
            CourseGroupAccess.group_id.in_(teacher_group_ids),
            CourseGroupAccess.is_active == True
        ).distinct().all()
        course_ids_with_access = [c[0] for c in course_ids_with_access]
    
    # Get those courses
    teacher_courses = db.query(Course).filter(
        Course.id.in_(course_ids_with_access),
        Course.is_active == True
    ).all() if course_ids_with_access else []
    
    total_courses = len(teacher_courses)
    
    # Get active students (accessed lessons in last 7 days)
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    active_students = 0
    
    if teacher_courses and student_ids_set:
        active_students = db.query(func.count(func.distinct(StudentProgress.user_id))).filter(
            StudentProgress.course_id.in_([c.id for c in teacher_courses]),
            StudentProgress.user_id.in_(student_ids_set),
            StudentProgress.last_accessed >= seven_days_ago
        ).scalar() or 0
    
    # Calculate average student progress across all students
    avg_student_progress = 0
    if teacher_courses and student_ids_set:
        progress_records = db.query(StudentProgress).filter(
            StudentProgress.course_id.in_([c.id for c in teacher_courses]),
            StudentProgress.user_id.in_(student_ids_set)
        ).all()
        
        if progress_records:
            total_progress = sum(p.completion_percentage for p in progress_records)
            avg_student_progress = round(total_progress / len(progress_records))
    
    # Get pending submissions (ungraded)
    pending_submissions = 0
    total_submissions = 0
    graded_submissions_list = []
    avg_student_score = 0
    
    if teacher_courses:
        teacher_assignments = db.query(Assignment).filter(
            Assignment.lesson_id.in_(
                db.query(Lesson.id).filter(
                    Lesson.module_id.in_(
                        db.query(Module.id).filter(
                            Module.course_id.in_([c.id for c in teacher_courses])
                        )
                    )
                )
            ),
            Assignment.is_active == True
        ).all()
        
        if teacher_assignments:
            pending_submissions = db.query(AssignmentSubmission).filter(
                AssignmentSubmission.assignment_id.in_([a.id for a in teacher_assignments]),
                AssignmentSubmission.is_graded == False
            ).count()
            
            total_submissions = db.query(AssignmentSubmission).filter(
                AssignmentSubmission.assignment_id.in_([a.id for a in teacher_assignments])
            ).count()
            
            graded_submissions_list = db.query(AssignmentSubmission).filter(
                AssignmentSubmission.assignment_id.in_([a.id for a in teacher_assignments]),
                AssignmentSubmission.is_graded == True,
                AssignmentSubmission.score.isnot(None)
            ).all()
            
            # Calculate average student score
            if graded_submissions_list:
                total_score = sum(sub.score for sub in graded_submissions_list if sub.score is not None)
                avg_student_score = round(total_score / len(graded_submissions_list))
    
    # Get recent enrollments (last 7 days)
    recent_enrollments = 0
    if teacher_courses:
        recent_enrollments = db.query(Enrollment).filter(
            Enrollment.course_id.in_([c.id for c in teacher_courses]),
            Enrollment.enrolled_at >= seven_days_ago,
            Enrollment.is_active == True
        ).count()
    
    # Calculate average completion rate
    total_completion_rate = 0
    
    course_stats = []
    
    for course in teacher_courses:
        # Count enrolled students for this course (for course_stats only, not total_students)
        enrolled_students = db.query(Enrollment).filter(
            Enrollment.course_id == course.id,
            Enrollment.is_active == True
        ).count()
        
        # Also add students from group access for this course
        group_accesses = db.query(CourseGroupAccess).filter(
            CourseGroupAccess.course_id == course.id,
            CourseGroupAccess.is_active == True
        ).all()
        
        course_group_students = 0
        for access in group_accesses:
            course_group_students += db.query(GroupStudent).filter(
                GroupStudent.group_id == access.group_id
            ).count()
        
        total_course_students = enrolled_students + course_group_students
        
        # Count modules
        total_modules = db.query(Module).filter(Module.course_id == course.id).count()
        
        # Calculate average progress for this course
        progress_records = db.query(StudentProgress).filter(
            StudentProgress.course_id == course.id
        ).all()
        
        if progress_records:
            avg_progress = sum(p.completion_percentage for p in progress_records) / len(progress_records)
            total_completion_rate += avg_progress
        else:
            avg_progress = 0
        
        # Get course completion rate
        course_completion_rate = 0
        if total_course_students > 0 and progress_records:
            # Calculate percentage of students who completed the course
            completed_count = sum(1 for p in progress_records if p.completion_percentage >= 100)
            course_completion_rate = round((completed_count / total_course_students) * 100)
        
        course_stats.append({
            "id": course.id,
            "title": course.title,
            "cover_image": course.cover_image_url,
            "teacher": user.name,
            "total_modules": total_modules,
            "enrolled_students": total_course_students,
            "completion_rate": course_completion_rate,
            "progress": round(avg_progress),
            "status": "active",
            "last_accessed": course.updated_at
        })
    
    # Calculate average completion rate
    avg_completion_rate = round(total_completion_rate / total_courses) if total_courses > 0 else 0
    
    # Ensure all values are numeric
    graded_submissions_count = len(graded_submissions_list) if graded_submissions_list else 0
    grading_progress = round((graded_submissions_count / total_submissions) * 100) if total_submissions > 0 else 0
    
    # Find past events with missing attendance (for reminders)
    from src.schemas.models import Event, EventGroup

    missing_attendance_reminders = []

    cutoff_date = datetime(2026, 2, 16, 0, 0, 0)

    if teacher_group_ids:
        past_events = db.query(Event).join(EventGroup).filter(
            EventGroup.group_id.in_(teacher_group_ids),
            Event.event_type == "class",
            Event.end_datetime <= datetime.utcnow(),
            Event.end_datetime >= cutoff_date,
            Event.is_active == True
        ).all()

        for event in past_events:
            event_group_ids = [eg.group_id for eg in event.event_groups]
            expected_students = db.query(GroupStudent.student_id).filter(
                GroupStudent.group_id.in_(event_group_ids)
            ).all()
            expected_count = len(expected_students)

            # Count via Attendance (single source of truth)
            attendance_count = AttendanceService.count_for_event(
                db, event.id, statuses=["present", "late", "absent"]
            )

            if attendance_count < expected_count:
                group_name = ""
                group_id = None
                if event.event_groups:
                    group_name = event.event_groups[0].group.name if event.event_groups[0].group else ""
                    group_id = event.event_groups[0].group.id if event.event_groups[0].group else None
                
                missing_attendance_reminders.append({
                    "event_id": event.id,
                    "title": event.title,
                    "group_name": group_name,
                    "group_id": group_id,
                    "event_date": event.start_datetime.isoformat(),
                    "expected_students": expected_count,
                    "recorded_students": attendance_count
                })
    
    return DashboardStatsSchema(
        user={
            "name": user.name.split()[0],
            "full_name": user.name,
            "role": user.role,
            "avatar_url": user.avatar_url
        },
        stats={
            "total_courses": total_courses,
            "total_students": total_students,
            "active_students": active_students,
            "avg_student_progress": avg_student_progress,
            "pending_submissions": pending_submissions,
            "recent_enrollments": recent_enrollments,
            "avg_completion_rate": avg_completion_rate,
            "avg_student_score": avg_student_score,
            "total_submissions": total_submissions,
            "graded_submissions": graded_submissions_count,
            "grading_progress": grading_progress,
            "missing_attendance_reminders": missing_attendance_reminders
        },
        recent_courses=course_stats[:6]
    )

def get_curator_dashboard_stats(
    user: UserInDB, 
    db: Session,
    group_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> DashboardStatsSchema:
    """Get dashboard stats for curator - matched to head curator layout but scoped to their groups"""
    from src.schemas.models import Group, Assignment, AssignmentSubmission, GroupStudent, GroupAssignment, StepProgress
    from sqlalchemy import func
    
    # 1. Curator's Groups and Students
    group_query = db.query(Group).filter(Group.curator_id == user.id, Group.is_active == True)
    if group_id:
        group_query = group_query.filter(Group.id == group_id)
    
    curator_groups = group_query.all()
    curator_group_ids = [g.id for g in curator_groups]
    total_groups = len(curator_groups)
    
    curator_students = db.query(UserInDB).join(GroupStudent, UserInDB.id == GroupStudent.student_id).filter(
        GroupStudent.group_id.in_(curator_group_ids),
        UserInDB.role == "student",
        UserInDB.is_active == True
    ).all() if curator_group_ids else []
    
    total_students = len(curator_students)
    current_student_ids = [s.id for s in curator_students]
    
    # Activity Trends Period
    if start_date:
        try:
            date_start = datetime.fromisoformat(start_date)
        except:
            date_start = datetime.utcnow() - timedelta(days=7)
    else:
        date_start = datetime.utcnow() - timedelta(days=7)
    
    if end_date:
        try:
            date_end = datetime.fromisoformat(end_date)
        except:
            date_end = datetime.utcnow()
    else:
        date_end = datetime.utcnow()

    total_active_students = 0
    if current_student_ids:
        active_student_ids_set = set()
        active_steps = db.query(StepProgress.user_id).filter(
            StepProgress.user_id.in_(current_student_ids),
            StepProgress.visited_at >= date_start,
            StepProgress.visited_at <= date_end
        ).distinct().all()
        for s in active_steps: active_student_ids_set.add(s[0])
        
        active_submissions = db.query(AssignmentSubmission.user_id).filter(
            AssignmentSubmission.user_id.in_(current_student_ids),
            AssignmentSubmission.submitted_at >= date_start,
            AssignmentSubmission.submitted_at <= date_end,
            AssignmentSubmission.is_hidden == False
        ).distinct().all()
        for s in active_submissions: active_student_ids_set.add(s[0])
        total_active_students = len(active_student_ids_set)

    total_inactive_students = total_students - total_active_students
    
    # 2. Global stats for curator's groups
    total_overdue_global = 0
    total_pending_global = 0
    
    if curator_group_ids and current_student_ids:
        # Overdues (GA + Direct)
        unsubmitted_ga = db.query(GroupAssignment, GroupStudent).join(
            GroupStudent, GroupAssignment.group_id == GroupStudent.group_id
        ).filter(
            GroupAssignment.group_id.in_(curator_group_ids),
            GroupAssignment.due_date < datetime.utcnow(),
            GroupAssignment.is_active == True,
            GroupStudent.student_id.in_(current_student_ids),
            ~db.query(AssignmentSubmission).filter(
                AssignmentSubmission.assignment_id == GroupAssignment.assignment_id,
                AssignmentSubmission.user_id == GroupStudent.student_id,
                AssignmentSubmission.is_hidden == False
            ).exists()
        ).count()
        
        late_ga = db.query(AssignmentSubmission).join(
            GroupAssignment, AssignmentSubmission.assignment_id == GroupAssignment.assignment_id
        ).filter(
            AssignmentSubmission.user_id.in_(current_student_ids),
            AssignmentSubmission.submitted_at > GroupAssignment.due_date,
            GroupAssignment.group_id.in_(curator_group_ids),
            AssignmentSubmission.is_hidden == False
        ).count()

        unsubmitted_direct = db.query(Assignment, GroupStudent).join(
            GroupStudent, Assignment.group_id == GroupStudent.group_id
        ).filter(
            Assignment.group_id.in_(curator_group_ids),
            Assignment.due_date < datetime.utcnow(),
            Assignment.is_active == True,
            Assignment.is_hidden == False,
            GroupStudent.student_id.in_(current_student_ids),
            ~db.query(AssignmentSubmission).filter(
                AssignmentSubmission.assignment_id == Assignment.id,
                AssignmentSubmission.user_id == GroupStudent.student_id,
                AssignmentSubmission.is_hidden == False
            ).exists()
        ).count()
        
        late_direct = db.query(AssignmentSubmission).join(
            Assignment, AssignmentSubmission.assignment_id == Assignment.id
        ).filter(
            AssignmentSubmission.user_id.in_(current_student_ids),
            AssignmentSubmission.submitted_at > Assignment.due_date,
            Assignment.group_id.in_(curator_group_ids),
            Assignment.is_active == True,
            Assignment.is_hidden == False,
            AssignmentSubmission.is_hidden == False
        ).count()
        
        total_overdue_global = unsubmitted_ga + late_ga + unsubmitted_direct + late_direct
        
        # Pending Grading
        total_pending_global = db.query(AssignmentSubmission).filter(
            AssignmentSubmission.user_id.in_(current_student_ids),
            AssignmentSubmission.is_graded == False,
            AssignmentSubmission.is_hidden == False
        ).count()

    # 3. Stats per Group (replacing Curator Performance)
    group_performance = []
    for g in curator_groups:
        g_student_ids = [gs.student_id for gs in db.query(GroupStudent).filter(GroupStudent.group_id == g.id).all()]
        
        avg_progress = 0
        overdue_count = 0
        pending_grading = 0
        total_due = 0
        total_submissions = 0
        
        if g_student_ids:
            # Avg Progress
            progress_records = db.query(StudentProgress).filter(StudentProgress.user_id.in_(g_student_ids)).all()
            if progress_records:
                avg_progress = sum(p.completion_percentage for p in progress_records) / len(progress_records)
            
            # Overdue calc for this group
            ug = db.query(GroupAssignment, GroupStudent).join(GroupStudent, GroupAssignment.group_id == GroupStudent.group_id).filter(
                GroupAssignment.group_id == g.id, GroupAssignment.due_date < datetime.utcnow(), GroupAssignment.is_active == True,
                GroupStudent.student_id.in_(g_student_ids),
                ~db.query(AssignmentSubmission).filter(AssignmentSubmission.assignment_id == GroupAssignment.assignment_id, AssignmentSubmission.user_id == GroupStudent.student_id, AssignmentSubmission.is_hidden == False).exists()
            ).count()
            lg = db.query(AssignmentSubmission).join(GroupAssignment, AssignmentSubmission.assignment_id == GroupAssignment.assignment_id).filter(
                AssignmentSubmission.user_id.in_(g_student_ids), AssignmentSubmission.submitted_at > GroupAssignment.due_date, GroupAssignment.group_id == g.id, AssignmentSubmission.is_hidden == False
            ).count()
            ud = db.query(Assignment, GroupStudent).join(GroupStudent, Assignment.group_id == GroupStudent.group_id).filter(
                Assignment.group_id == g.id, Assignment.due_date < datetime.utcnow(), Assignment.is_active == True, GroupStudent.student_id.in_(g_student_ids),
                ~db.query(AssignmentSubmission).filter(AssignmentSubmission.assignment_id == Assignment.id, AssignmentSubmission.user_id == GroupStudent.student_id, AssignmentSubmission.is_hidden == False).exists()
            ).count()
            ld = db.query(AssignmentSubmission).join(Assignment, AssignmentSubmission.assignment_id == Assignment.id).filter(
                AssignmentSubmission.user_id.in_(g_student_ids), AssignmentSubmission.submitted_at > Assignment.due_date, Assignment.group_id == g.id, Assignment.is_active == True, AssignmentSubmission.is_hidden == False
            ).count()
            overdue_count = ug + lg + ud + ld
            
            # Pending Grading
            pending_grading = db.query(AssignmentSubmission).filter(
                AssignmentSubmission.user_id.in_(g_student_ids), AssignmentSubmission.is_graded == False, AssignmentSubmission.is_hidden == False
            ).count()
            
            # Totals for percentages
            total_due = db.query(GroupAssignment, GroupStudent).join(GroupStudent, GroupAssignment.group_id == GroupStudent.group_id).filter(
                GroupAssignment.group_id == g.id, GroupAssignment.due_date < datetime.utcnow(), GroupAssignment.is_active == True, GroupStudent.student_id.in_(g_student_ids)
            ).count() + db.query(Assignment, GroupStudent).join(GroupStudent, Assignment.group_id == GroupStudent.group_id).filter(
                Assignment.group_id == g.id, Assignment.due_date < datetime.utcnow(), Assignment.is_active == True, GroupStudent.student_id.in_(g_student_ids)
            ).count()
            
            total_submissions = db.query(AssignmentSubmission).filter(
                AssignmentSubmission.user_id.in_(g_student_ids), AssignmentSubmission.is_hidden == False
            ).count()

        group_performance.append({
            "id": g.id,
            "name": g.name,
            "students_count": len(g_student_ids),
            "avg_progress": round(avg_progress, 1),
            "overdue_count": overdue_count,
            "total_due": total_due,
            "overdue_perc": round(overdue_count / total_due * 100, 1) if total_due > 0 else 0,
            "pending_grading": pending_grading,
            "total_submissions": total_submissions,
            "pending_perc": round(pending_grading / total_submissions * 100, 1) if total_submissions > 0 else 0
        })

    # 4. Activity Trends
    activity_trends = []
    
    # Calculate days between start and end
    delta = date_end - date_start
    num_days = delta.days + 1
    if num_days > 60: num_days = 60 # Cap to 60 days to prevent performance issues
    
    for i in range(num_days):
        day = (date_start + timedelta(days=i)).date()
        day_active_count = db.query(func.count(func.distinct(StepProgress.user_id))).filter(
            func.date(StepProgress.visited_at) == day,
            StepProgress.user_id.in_(current_student_ids) if current_student_ids else False
        ).scalar() or 0
        
        activity_trends.append({
            "date": day.isoformat(),
            "count": day_active_count,
            "percentage": round(day_active_count / total_students * 100, 1) if total_students > 0 else 0
        })

    # 5. Missing Attendance Reminders (similar to teacher)
    from src.schemas.models import EventGroup, Event
    missing_attendance_reminders = []

    cutoff_date = datetime(2026, 2, 16, 0, 0, 0)

    if curator_group_ids:
        past_events = db.query(Event).join(EventGroup).filter(
            EventGroup.group_id.in_(curator_group_ids),
            Event.event_type == "class",
            Event.end_datetime <= datetime.utcnow(),
            Event.end_datetime >= cutoff_date,
            Event.is_active == True
        ).all()

        for event in past_events:
            eg_ids = [eg.group_id for eg in event.event_groups if eg.group_id in curator_group_ids]
            if not eg_ids:
                continue

            e_expected_students = db.query(GroupStudent.student_id).filter(
                GroupStudent.group_id.in_(eg_ids)
            ).all()
            e_expected_count = len(e_expected_students)

            e_attendance_count = AttendanceService.count_for_event(
                db, event.id, statuses=["present", "late", "absent"]
            )

            if e_attendance_count < e_expected_count:
                g_name = ""
                g_id = None
                if event.event_groups:
                    target_eg = next((eg for eg in event.event_groups if eg.group_id in curator_group_ids), event.event_groups[0])
                    g_name = target_eg.group.name if target_eg.group else ""
                    g_id = target_eg.group.id if target_eg.group else None
                
                missing_attendance_reminders.append({
                    "event_id": event.id,
                    "title": event.title,
                    "group_name": g_name,
                    "group_id": g_id,
                    "event_date": event.start_datetime.isoformat(),
                    "expected_students": e_expected_count,
                    "recorded_students": e_attendance_count
                })

    return DashboardStatsSchema(
        user={
            "name": user.name.split()[0],
            "full_name": user.name,
            "role": user.role,
            "avatar_url": user.avatar_url
        },
        stats={
            "total_groups": total_groups,
            "total_students": total_students,
            "active_students_7d": total_active_students,
            "inactive_students": total_inactive_students,
            "total_overdue": total_overdue_global,
            "total_pending": total_pending_global,
            "curator_performance": group_performance, # Re-using key name for frontend
            "activity_trends": activity_trends,
            "missing_attendance_reminders": missing_attendance_reminders
        },
        recent_courses=[
            {
                "id": p["id"],
                "title": p["name"],
                "curator": user.name,
                "overdue_count": p["overdue_count"]
            }
            for p in group_performance if p["overdue_count"] > 0
        ]
    )

def get_head_curator_dashboard_stats(
    user: UserInDB, 
    db: Session, 
    group_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> DashboardStatsSchema:
    """Get dashboard stats for Head of Curators"""
    from src.schemas.models import Group, Assignment, AssignmentSubmission, GroupStudent, GroupAssignment, StepProgress
    from sqlalchemy.orm import aliased
    
    # 1. Сводная статистика
    total_curators = db.query(UserInDB).filter(UserInDB.role == "curator", UserInDB.is_active == True).count()
    
    # Base query for groups
    group_query = db.query(Group).filter(
        Group.is_active == True,
        Group.is_special == False
    )
    if group_id:
        group_query = group_query.filter(Group.id == group_id)
    
    current_groups = group_query.all()
    total_groups = len(current_groups)
    current_group_ids = [g.id for g in current_groups]
    
    # Base query for students
    student_query = db.query(UserInDB).filter(UserInDB.role == "student", UserInDB.is_active == True)
    if group_id:
        student_query = student_query.join(GroupStudent, UserInDB.id == GroupStudent.student_id).filter(GroupStudent.group_id == group_id)
    
    current_students = student_query.all()
    total_students = len(current_students)
    current_student_ids = [s.id for s in current_students]
    
    # Активность за 7 дней (Active criteria: activity in last 7 days)
    # Use provided date range or default to last 7 days
    if start_date:
        try:
            date_start = datetime.fromisoformat(start_date)
        except:
            date_start = datetime.utcnow() - timedelta(days=7)
    else:
        date_start = datetime.utcnow() - timedelta(days=7)
    
    if end_date:
        try:
            date_end = datetime.fromisoformat(end_date)
        except:
            date_end = datetime.utcnow()
    else:
        date_end = datetime.utcnow()
    
    seven_days_ago = date_start
    
    # Count active vs inactive students
    active_student_ids_set = set()
    if current_student_ids:
        # Step progress activity
        active_steps = db.query(StepProgress.user_id).filter(
            StepProgress.user_id.in_(current_student_ids),
            StepProgress.visited_at >= seven_days_ago
        ).distinct().all()
        for s in active_steps: active_student_ids_set.add(s[0])
        
        # Assignment submission activity
        active_submissions = db.query(AssignmentSubmission.user_id).filter(
            AssignmentSubmission.user_id.in_(current_student_ids),
            AssignmentSubmission.submitted_at >= seven_days_ago,
            AssignmentSubmission.is_hidden == False
        ).distinct().all()
        for s in active_submissions: active_student_ids_set.add(s[0])

    total_active_students = len(active_student_ids_set)
    total_inactive_students = total_students - total_active_students
    
    # 2. Global stats for KPIs (includes all groups, not just those with curators)
    total_overdue_global = 0
    total_pending_global = 0
    
    if current_group_ids and current_student_ids:
        # --- 1. From GroupAssignment (Lesson-based assignments assigned to groups) ---
        unsubmitted_ga = db.query(GroupAssignment, GroupStudent).join(
            GroupStudent, GroupAssignment.group_id == GroupStudent.group_id
        ).filter(
            GroupAssignment.group_id.in_(current_group_ids),
            GroupAssignment.due_date < datetime.utcnow(),
            GroupAssignment.is_active == True,
            GroupStudent.student_id.in_(current_student_ids),
            ~db.query(AssignmentSubmission).filter(
                AssignmentSubmission.assignment_id == GroupAssignment.assignment_id,
                AssignmentSubmission.user_id == GroupStudent.student_id,
                AssignmentSubmission.is_hidden == False
            ).exists()
        ).count()
        
        late_ga = db.query(AssignmentSubmission).join(
            GroupAssignment, AssignmentSubmission.assignment_id == GroupAssignment.assignment_id
        ).filter(
            AssignmentSubmission.user_id.in_(current_student_ids),
            AssignmentSubmission.submitted_at > GroupAssignment.due_date,
            GroupAssignment.group_id.in_(current_group_ids),
            AssignmentSubmission.is_hidden == False
        ).count()

        # --- 2. From Assignment directly (Group-specific assignments) ---
        unsubmitted_direct = db.query(Assignment, GroupStudent).join(
            GroupStudent, Assignment.group_id == GroupStudent.group_id
        ).filter(
            Assignment.group_id.in_(current_group_ids),
            Assignment.due_date < datetime.utcnow(),
            Assignment.is_active == True,
            Assignment.is_hidden == False,
            GroupStudent.student_id.in_(current_student_ids),
            ~db.query(AssignmentSubmission).filter(
                AssignmentSubmission.assignment_id == Assignment.id,
                AssignmentSubmission.user_id == GroupStudent.student_id,
                AssignmentSubmission.is_hidden == False
            ).exists()
        ).count()
        
        late_direct = db.query(AssignmentSubmission).join(
            Assignment, AssignmentSubmission.assignment_id == Assignment.id
        ).filter(
            AssignmentSubmission.user_id.in_(current_student_ids),
            AssignmentSubmission.submitted_at > Assignment.due_date,
            Assignment.group_id.in_(current_group_ids),
            Assignment.is_active == True,
            Assignment.is_hidden == False,
            AssignmentSubmission.is_hidden == False
        ).count()
        
        total_overdue_global = unsubmitted_ga + late_ga + unsubmitted_direct + late_direct
        
        # Global Pending Grading
        total_pending_global = db.query(AssignmentSubmission).filter(
            AssignmentSubmission.user_id.in_(current_student_ids),
            AssignmentSubmission.is_graded == False,
            AssignmentSubmission.is_hidden == False
        ).count()

    # 3. Детальная статистика по кураторам
    curators = db.query(UserInDB).filter(UserInDB.role == "curator", UserInDB.is_active == True).all()
    curator_performance = []
    
    for curator in curators:
        # Группы куратора
        c_groups_query = db.query(Group).filter(
            Group.curator_id == curator.id,
            Group.is_active == True,
            Group.is_special == False
        )
        if group_id:
            c_groups_query = c_groups_query.filter(Group.id == group_id)
        
        c_groups = c_groups_query.all()
        c_group_ids = [g.id for g in c_groups]
        
        # Студенты куратора
        c_student_ids = [gs.student_id for gs in db.query(GroupStudent).filter(GroupStudent.group_id.in_(c_group_ids)).all()] if c_group_ids else []
        
        avg_progress = 0
        overdue_count = 0
        pending_grading = 0
        overdue_perc = 0
        pending_perc = 0
        
        if c_student_ids:
            # Средний прогресс (за все время, так как это статус завершенности)
            progress_records = db.query(StudentProgress).filter(StudentProgress.user_id.in_(c_student_ids)).all()
            if progress_records:
                avg_progress = sum(p.completion_percentage for p in progress_records) / len(progress_records)
            
            # Просроченные задания (Overdue)
            # 1. Lesson assignments via GroupAssignment
            unsubmitted_ga = db.query(GroupAssignment, GroupStudent).join(
                GroupStudent, GroupAssignment.group_id == GroupStudent.group_id
            ).filter(
                GroupAssignment.group_id.in_(c_group_ids),
                GroupAssignment.due_date < datetime.utcnow(),
                GroupAssignment.is_active == True,
                GroupStudent.student_id.in_(c_student_ids),
                ~db.query(AssignmentSubmission).filter(
                    AssignmentSubmission.assignment_id == GroupAssignment.assignment_id,
                    AssignmentSubmission.user_id == GroupStudent.student_id,
                    AssignmentSubmission.is_hidden == False
                ).exists()
            ).count()
            
            late_ga = db.query(AssignmentSubmission).join(
                GroupAssignment, AssignmentSubmission.assignment_id == GroupAssignment.assignment_id
            ).filter(
                AssignmentSubmission.user_id.in_(c_student_ids),
                AssignmentSubmission.submitted_at > GroupAssignment.due_date,
                GroupAssignment.group_id.in_(c_group_ids),
                AssignmentSubmission.is_hidden == False
            ).count()

            # 2. Group-specific assignments directly in Assignment table
            unsubmitted_direct = db.query(Assignment, GroupStudent).join(
                GroupStudent, Assignment.group_id == GroupStudent.group_id
            ).filter(
                Assignment.group_id.in_(c_group_ids),
                Assignment.due_date < datetime.utcnow(),
                Assignment.is_active == True,
                Assignment.is_hidden == False,
                GroupStudent.student_id.in_(c_student_ids),
                ~db.query(AssignmentSubmission).filter(
                    AssignmentSubmission.assignment_id == Assignment.id,
                    AssignmentSubmission.user_id == GroupStudent.student_id,
                    AssignmentSubmission.is_hidden == False
                ).exists()
            ).count()
            
            late_direct = db.query(AssignmentSubmission).join(
                Assignment, AssignmentSubmission.assignment_id == Assignment.id
            ).filter(
                AssignmentSubmission.user_id.in_(c_student_ids),
                AssignmentSubmission.submitted_at > Assignment.due_date,
                Assignment.group_id.in_(c_group_ids),
                Assignment.is_active == True,
                Assignment.is_hidden == False,
                AssignmentSubmission.is_hidden == False
            ).count()
            
            overdue_count = unsubmitted_ga + late_ga + unsubmitted_direct + late_direct
            
            # Ожидают проверки (все активные, так как их нужно проверить)
            pending_grading = db.query(AssignmentSubmission).filter(
                AssignmentSubmission.user_id.in_(c_student_ids),
                AssignmentSubmission.is_graded == False,
                AssignmentSubmission.is_hidden == False
            ).count()
            
            # --- Percentages Calculations ---
            # Total Due (Universe for Overdue)
            total_due_ga = db.query(GroupAssignment, GroupStudent).join(
                GroupStudent, GroupAssignment.group_id == GroupStudent.group_id
            ).filter(
                GroupAssignment.group_id.in_(c_group_ids),
                GroupAssignment.due_date < datetime.utcnow(),
                GroupAssignment.is_active == True,
                GroupStudent.student_id.in_(c_student_ids)
            ).count()
            
            total_due_direct = db.query(Assignment, GroupStudent).join(
                GroupStudent, Assignment.group_id == GroupStudent.group_id
            ).filter(
                Assignment.group_id.in_(c_group_ids),
                Assignment.due_date < datetime.utcnow(),
                Assignment.is_active == True,
                Assignment.is_hidden == False,
                GroupStudent.student_id.in_(c_student_ids)
            ).count()
            
            total_due = total_due_ga + total_due_direct
            overdue_perc = round((overdue_count / total_due * 100), 1) if total_due > 0 else 0
            
            # Total Submissions (Universe for Pending Grading)
            total_submissions = db.query(AssignmentSubmission).filter(
                AssignmentSubmission.user_id.in_(c_student_ids),
                AssignmentSubmission.is_hidden == False
            ).count()
            pending_perc = round((pending_grading / total_submissions * 100), 1) if total_submissions > 0 else 0

        curator_performance.append({
            "id": curator.id,
            "name": curator.name,
            "groups_count": len(c_groups),
            "students_count": len(c_student_ids),
            "avg_progress": round(avg_progress, 1),
            "overdue_count": overdue_count,
            "total_due": total_due,
            "overdue_perc": overdue_perc,
            "pending_grading": pending_grading,
            "total_submissions": total_submissions,
            "pending_perc": pending_perc
        })

    # 4. Активность за 14 дней (Engagement Trends in PERCENTAGE)
    activity_trends = []
    
    # Calculate number of days in range
    days_diff = (date_end.date() - date_start.date()).days
    # Limit to reasonable range (max 90 days)
    days_to_show = min(days_diff + 1, 90) if days_diff > 0 else 14
    
    for i in range(days_to_show):
        day = date_start.date() + timedelta(days=i)
        if day > date_end.date():
            break
        # Count unique students active on that day
        day_active_count = db.query(func.count(func.distinct(StepProgress.user_id))).filter(
            func.date(StepProgress.visited_at) == day,
            StepProgress.user_id.in_(current_student_ids) if current_student_ids else False
        ).scalar() or 0
        
        percentage = round((day_active_count / total_students) * 100, 1) if total_students > 0 else 0
        activity_trends.append({
            "date": day.isoformat(),
            "count": day_active_count,
            "percentage": percentage
        })

    at_risk_groups = []
    if current_group_ids:
        # Calculate overdue per group (including both GA and Direct)
        temp_group_overdue = {}
        for gid in current_group_ids:
            # 1. GA source
            u_ga = db.query(GroupAssignment, GroupStudent).join(
                GroupStudent, GroupAssignment.group_id == GroupStudent.group_id
            ).filter(
                GroupAssignment.group_id == gid,
                GroupAssignment.due_date < datetime.utcnow(),
                GroupAssignment.is_active == True,
                ~db.query(AssignmentSubmission).filter(
                    AssignmentSubmission.assignment_id == GroupAssignment.assignment_id,
                    AssignmentSubmission.user_id == GroupStudent.student_id,
                    AssignmentSubmission.is_hidden == False
                ).exists()
            ).count()
            
            l_ga = db.query(AssignmentSubmission).join(
                GroupAssignment, AssignmentSubmission.assignment_id == GroupAssignment.assignment_id
            ).filter(
                GroupAssignment.group_id == gid,
                AssignmentSubmission.submitted_at > GroupAssignment.due_date,
                AssignmentSubmission.is_hidden == False
            ).count()
            
            # 2. Direct source
            u_direct = db.query(Assignment, GroupStudent).join(
                GroupStudent, Assignment.group_id == GroupStudent.group_id
            ).filter(
                Assignment.group_id == gid,
                Assignment.due_date < datetime.utcnow(),
                Assignment.is_active == True,
                Assignment.is_hidden == False,
                ~db.query(AssignmentSubmission).filter(
                    AssignmentSubmission.assignment_id == Assignment.id,
                    AssignmentSubmission.user_id == GroupStudent.student_id,
                    AssignmentSubmission.is_hidden == False
                ).exists()
            ).count()
            
            l_direct = db.query(AssignmentSubmission).join(
                Assignment, AssignmentSubmission.assignment_id == Assignment.id
            ).filter(
                Assignment.group_id == gid,
                AssignmentSubmission.submitted_at > Assignment.due_date,
                Assignment.is_active == True,
                Assignment.is_hidden == False,
                AssignmentSubmission.is_hidden == False
            ).count()
            
            total = u_ga + l_ga + u_direct + l_direct
            if total > 0:
                temp_group_overdue[gid] = total

        if temp_group_overdue:
            # Sort and pick top 10
            sorted_gids = sorted(temp_group_overdue.keys(), key=lambda x: temp_group_overdue[x], reverse=True)[:10]
            for gid in sorted_gids:
                group = db.query(Group).filter(Group.id == gid).first()
                curator = db.query(UserInDB).filter(UserInDB.id == group.curator_id).first() if group.curator_id else None
                at_risk_groups.append({
                    "id": gid,
                    "title": group.name,
                    "curator": curator.name if curator else "No Curator",
                    "overdue_count": temp_group_overdue[gid],
                    "status": "critical" if temp_group_overdue[gid] > 5 else "warning"
                })

    # 5. Missing Attendance Reminders (similar to teacher/curator)
    from src.schemas.models import EventGroup, Event
    missing_attendance_reminders = []

    cutoff_date = datetime(2026, 2, 16, 0, 0, 0)

    if current_group_ids:
        past_events_rem = db.query(Event).join(EventGroup).filter(
            EventGroup.group_id.in_(current_group_ids),
            Event.event_type == "class",
            Event.end_datetime <= datetime.utcnow(),
            Event.end_datetime >= cutoff_date,
            Event.is_active == True
        ).all()

        for event in past_events_rem:
            eg_ids = [eg.group_id for eg in event.event_groups if eg.group_id in current_group_ids]
            if not eg_ids:
                continue

            e_expected_students = db.query(GroupStudent.student_id).filter(
                GroupStudent.group_id.in_(eg_ids)
            ).all()
            e_expected_count = len(e_expected_students)

            e_attendance_count = AttendanceService.count_for_event(
                db, event.id, statuses=["present", "late", "absent"]
            )

            if e_attendance_count < e_expected_count:
                g_name = ""
                g_id = None
                if event.event_groups:
                    target_eg = next((eg for eg in event.event_groups if eg.group_id in current_group_ids), event.event_groups[0])
                    g_name = target_eg.group.name if target_eg.group else ""
                    g_id = target_eg.group.id if target_eg.group else None
                
                missing_attendance_reminders.append({
                    "event_id": event.id,
                    "title": event.title,
                    "group_name": g_name,
                    "group_id": g_id,
                    "event_date": event.start_datetime.isoformat(),
                    "expected_students": e_expected_count,
                    "recorded_students": e_attendance_count
                })

    return DashboardStatsSchema(
        user={
            "name": user.name.split()[0],
            "full_name": user.name,
            "role": user.role,
            "avatar_url": user.avatar_url
        },
        stats={
            "total_curators": total_curators,
            "total_groups": total_groups,
            "total_students": total_students,
            "active_students_7d": total_active_students,
            "inactive_students": total_inactive_students,
            "total_overdue": total_overdue_global,
            "total_pending": total_pending_global,
            "curator_performance": curator_performance,
            "activity_trends": activity_trends,
            "missing_attendance_reminders": missing_attendance_reminders
        },
        recent_courses=at_risk_groups
    )

def get_head_teacher_dashboard_stats(user: UserInDB, db: Session) -> DashboardStatsSchema:
    """Get dashboard stats for head teacher - missing attendance for managed course groups"""
    from src.schemas.models import (
        Event, EventGroup, Group,
        CourseHeadTeacher, CourseGroupAccess
    )

    # Get groups in courses managed by this head teacher
    managed_course_ids = db.query(CourseHeadTeacher.course_id).filter(
        CourseHeadTeacher.head_teacher_id == user.id
    ).all()
    managed_course_ids = [c[0] for c in managed_course_ids]

    if not managed_course_ids:
        return DashboardStatsSchema(
            user={
                "name": user.name.split()[0] if user.name else "User",
                "full_name": user.name,
                "role": user.role,
                "avatar_url": user.avatar_url
            },
            stats={"missing_attendance_reminders": []},
            recent_courses=[]
        )

    group_accesses = db.query(CourseGroupAccess).filter(
        CourseGroupAccess.course_id.in_(managed_course_ids),
        CourseGroupAccess.is_active == True
    ).all()
    managed_group_ids = [ga.group_id for ga in group_accesses]
    if managed_group_ids:
        managed_group_ids = [
            group_id for (group_id,) in db.query(Group.id).filter(
                Group.id.in_(managed_group_ids),
                Group.is_special == False
            ).all()
        ]

    missing_attendance_reminders = []
    cutoff_date = datetime(2026, 2, 16, 0, 0, 0)

    if managed_group_ids:
        past_events = db.query(Event).join(EventGroup).filter(
            EventGroup.group_id.in_(managed_group_ids),
            Event.event_type == "class",
            Event.end_datetime <= datetime.utcnow(),
            Event.end_datetime >= cutoff_date,
            Event.is_active == True
        ).all()

        for event in past_events:
            event_group_ids = [eg.group_id for eg in event.event_groups]
            expected_count = db.query(GroupStudent.student_id).filter(
                GroupStudent.group_id.in_(event_group_ids)
            ).count()

            attendance_count = AttendanceService.count_for_event(
                db, event.id, statuses=["present", "late", "absent"]
            )

            if attendance_count < expected_count:
                group_name = ""
                group_id = None
                if event.event_groups:
                    group_name = event.event_groups[0].group.name if event.event_groups[0].group else ""
                    group_id = event.event_groups[0].group.id if event.event_groups[0].group else None

                missing_attendance_reminders.append({
                    "event_id": event.id,
                    "title": event.title,
                    "group_name": group_name,
                    "group_id": group_id,
                    "event_date": event.start_datetime.isoformat(),
                    "expected_students": expected_count,
                    "recorded_students": attendance_count
                })

    # Get recent managed courses for recent_courses
    recent_courses = db.query(Course).filter(
        Course.id.in_(managed_course_ids),
        Course.is_active == True
    ).order_by(desc(Course.updated_at)).limit(6).all()

    course_list = []
    for course in recent_courses:
        teacher = db.query(UserInDB).filter(UserInDB.id == course.teacher_id).first()
        course_list.append({
            "id": course.id,
            "title": course.title,
            "cover_image": course.cover_image_url,
            "teacher": teacher.name if teacher else "Unknown",
            "enrolled_students": 0,
            "status": "active",
            "last_accessed": course.updated_at
        })

    return DashboardStatsSchema(
        user={
            "name": user.name.split()[0] if user.name else "User",
            "full_name": user.name,
            "role": user.role,
            "avatar_url": user.avatar_url
        },
        stats={"missing_attendance_reminders": missing_attendance_reminders},
        recent_courses=course_list
    )


def get_admin_dashboard_stats(user: UserInDB, db: Session) -> DashboardStatsSchema:
    """Get dashboard stats for admin"""
    # Get platform-wide statistics
    total_users = db.query(UserInDB).filter(UserInDB.is_active == True).count()
    total_students = db.query(UserInDB).filter(
        UserInDB.role == "student",
        UserInDB.is_active == True
    ).count()
    total_teachers = db.query(UserInDB).filter(
        UserInDB.role == "teacher",
        UserInDB.is_active == True
    ).count()
    total_courses = db.query(Course).filter(Course.is_active == True).count()
    total_enrollments = db.query(Enrollment).filter(Enrollment.is_active == True).count()
    
    # Get recent course activity
    recent_courses = db.query(Course).filter(
        Course.is_active == True
    ).order_by(desc(Course.updated_at)).limit(6).all()
    
    course_list = []
    for course in recent_courses:
        teacher = db.query(UserInDB).filter(UserInDB.id == course.teacher_id).first()
        enrolled_count = db.query(Enrollment).filter(
            Enrollment.course_id == course.id,
            Enrollment.is_active == True
        ).count()
        
        course_list.append({
            "id": course.id,
            "title": course.title,
            "cover_image": course.cover_image_url,
            "teacher": teacher.name if teacher else "Unknown",
            "enrolled_students": enrolled_count,
            "status": "active",
            "last_accessed": course.updated_at
        })
    
    # Find past events with missing attendance (for reminders)
    from src.schemas.models import Event, EventGroup, Group

    missing_attendance_reminders = []

    # Only check events from February 4, 2026 onwards (production launch date)
    cutoff_date = datetime(2026, 2, 16, 0, 0, 0)

    past_events = db.query(Event).join(EventGroup).filter(
        Event.event_type == "class",
        Event.end_datetime <= datetime.utcnow(),
        Event.end_datetime >= cutoff_date,
        Event.is_active == True
    ).all()

    for event in past_events:
        event_group_ids = [eg.group_id for eg in event.event_groups]
        expected_students = db.query(GroupStudent.student_id).filter(
            GroupStudent.group_id.in_(event_group_ids)
        ).all()
        expected_count = len(expected_students)

        attendance_count = AttendanceService.count_for_event(
            db, event.id, statuses=["present", "late", "absent"]
        )

        if attendance_count < expected_count:
            group_name = ""
            group_id = None
            if event.event_groups:
                group_name = event.event_groups[0].group.name if event.event_groups[0].group else ""
                group_id = event.event_groups[0].group.id if event.event_groups[0].group else None
            
            missing_attendance_reminders.append({
                "event_id": event.id,
                "title": event.title,
                "group_name": group_name,
                "group_id": group_id,
                "event_date": event.start_datetime.isoformat(),
                "expected_students": expected_count,
                "recorded_students": attendance_count
            })
    
    return DashboardStatsSchema(
        user={
            "name": user.name.split()[0],
            "full_name": user.name,
            "role": user.role,
            "avatar_url": user.avatar_url
        },
        stats={
            "total_users": total_users,
            "total_students": total_students,
            "total_teachers": total_teachers,
            "total_courses": total_courses,
            "total_enrollments": total_enrollments,
            "missing_attendance_reminders": missing_attendance_reminders
        },
        recent_courses=course_list
    )

@router.get("/curator/homework-by-group")
async def get_curator_homework_by_group(
    group_id: Optional[int] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Get homework data grouped by curator's groups with detailed student submissions.
    Returns groups with their assignments and student progress.
    """
    if current_user.role not in ["curator", "head_curator"]:
        raise HTTPException(status_code=403, detail="Only curators and head curators can access this endpoint")
    
    from src.schemas.models import Assignment, AssignmentSubmission, CourseGroupAccess, Group
    
    # Get curator's groups
    if current_user.role == "head_curator":
        # Head curator sees all groups that have a curator or all active groups? 
        # Usually they oversee everything.
        curator_groups_query = db.query(Group)
    else:
        curator_groups_query = db.query(Group).filter(Group.curator_id == current_user.id)
        
    if group_id:
        curator_groups_query = curator_groups_query.filter(Group.id == group_id)
    
    curator_groups = curator_groups_query.all()
    
    if not curator_groups:
        return {"groups": []}
    
    result = []
    
    for group in curator_groups:
        # Get students in this group
        group_student_ids = [gs.student_id for gs in db.query(GroupStudent).filter(
            GroupStudent.group_id == group.id
        ).all()]
        
        if not group_student_ids:
            result.append({
                "group_id": group.id,
                "group_name": group.name,
                "students_count": 0,
                "assignments": []
            })
            continue
        
        # Get students info
        students = db.query(UserInDB).filter(UserInDB.id.in_(group_student_ids)).all()
        students_map = {s.id: s for s in students}
        
        # Get courses that this group has access to
        course_access = db.query(CourseGroupAccess).filter(
            CourseGroupAccess.group_id == group.id,
            CourseGroupAccess.is_active == True
        ).all()
        course_ids = [ca.course_id for ca in course_access]
        
        # Get lesson-based assignments from those courses
        lesson_based_assignments = []
        if course_ids:
            lesson_ids = db.query(Lesson.id).filter(
                Lesson.module_id.in_(
                    db.query(Module.id).filter(Module.course_id.in_(course_ids))
                )
            ).all()
            lesson_ids = [l[0] for l in lesson_ids]
            
            if lesson_ids:
                lesson_based_assignments = db.query(Assignment).filter(
                    Assignment.lesson_id.in_(lesson_ids),
                    Assignment.is_active == True,
                    (Assignment.is_hidden == False) | (Assignment.is_hidden.is_(None))
                ).all()
        
        # Get group-based assignments
        group_based_assignments = db.query(Assignment).filter(
            Assignment.group_id == group.id,
            Assignment.lesson_id.is_(None),
            Assignment.is_active == True,
            (Assignment.is_hidden == False) | (Assignment.is_hidden.is_(None))
        ).all()
        
        # Combine assignments
        all_assignments = lesson_based_assignments + group_based_assignments
        
        assignments_data = []
        for assignment in all_assignments:
            # Get course info
            course_title = "Unknown"
            if assignment.lesson_id:
                lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
                if lesson:
                    module = db.query(Module).filter(Module.id == lesson.module_id).first()
                    if module:
                        course = db.query(Course).filter(Course.id == module.course_id).first()
                        if course:
                            course_title = course.title
            elif assignment.group_id:
                course_title = f"Group Assignment"
            
            # Get submissions for this assignment from group students
            submissions = db.query(AssignmentSubmission).filter(
                AssignmentSubmission.assignment_id == assignment.id,
                AssignmentSubmission.user_id.in_(group_student_ids),
                AssignmentSubmission.is_hidden == False
            ).all()
            
            submissions_map = {s.user_id: s for s in submissions}
            
            # Build student progress list
            students_progress = []
            for student_id in group_student_ids:
                student = students_map.get(student_id)
                if not student:
                    continue
                
                submission = submissions_map.get(student_id)
                
                # Determine status
                status = 'not_submitted'
                if submission:
                    if submission.is_graded:
                        status = 'graded'
                    else:
                        status = 'submitted'
                elif assignment.due_date and assignment.due_date < datetime.utcnow():
                    status = 'overdue'
                
                students_progress.append({
                    "student_id": student.id,
                    "student_name": student.name,
                    "student_email": student.email,
                    "status": status,
                    "submission_id": submission.id if submission else None,
                    "score": submission.score if submission else None,
                    "max_score": assignment.max_score,
                    "submitted_at": submission.submitted_at.isoformat() if submission and submission.submitted_at else None,
                    "graded_at": submission.graded_at.isoformat() if submission and submission.graded_at else None,
                    "feedback": submission.feedback if submission else None
                })
            
            # Calculate summary
            submitted_count = len([s for s in students_progress if s["status"] in ['submitted', 'graded']])
            graded_count = len([s for s in students_progress if s["status"] == 'graded'])
            not_submitted_count = len([s for s in students_progress if s["status"] == 'not_submitted'])
            overdue_count = len([s for s in students_progress if s["status"] == 'overdue'])
            
            scores = [s["score"] for s in students_progress if s["score"] is not None]
            avg_score = round(sum(scores) / len(scores), 1) if scores else 0
            
            # Parse assignment content
            assignment_content = None
            if assignment.content:
                try:
                    assignment_content = json.loads(assignment.content) if isinstance(assignment.content, str) else assignment.content
                except:
                    assignment_content = None
            
            assignments_data.append({
                "id": assignment.id,
                "title": assignment.title,
                "description": assignment.description,
                "course_title": course_title,
                "due_date": assignment.due_date.isoformat() if assignment.due_date else None,
                "max_score": assignment.max_score,
                "assignment_type": assignment.assignment_type,
                "content": assignment_content,
                "summary": {
                    "total_students": len(group_student_ids),
                    "submitted": submitted_count,
                    "graded": graded_count,
                    "not_submitted": not_submitted_count,
                    "overdue": overdue_count,
                    "average_score": avg_score
                },
                "students": students_progress
            })
        
        # Sort assignments by due date (most recent first)
        assignments_data.sort(key=lambda x: x["due_date"] or "9999-99-99", reverse=True)
        
        result.append({
            "group_id": group.id,
            "group_name": group.name,
            "students_count": len(group_student_ids),
            "assignments": assignments_data
        })
    
    return {"groups": result}


@router.get("/curator/{curator_id}")
async def get_curator_details(
    curator_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Get detailed stats for a specific curator.
    Access: head_curator or admin only.
    """
    if current_user.role not in ["head_curator", "admin"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    from src.schemas.models import Group, GroupStudent, Assignment, AssignmentSubmission, GroupAssignment
    
    curator = db.query(UserInDB).filter(
        UserInDB.id == curator_id,
        UserInDB.role == "curator",
        UserInDB.is_active == True
    ).first()
    
    if not curator:
        raise HTTPException(status_code=404, detail="Curator not found")
    
    # Get curator's groups
    curator_groups = db.query(Group).filter(
        Group.curator_id == curator_id,
        Group.is_active == True
    ).all()
    
    groups_data = []
    total_students = 0
    total_overdue = 0
    total_progress = 0
    
    for group in curator_groups:
        # Get students in this group
        group_students = db.query(GroupStudent).filter(GroupStudent.group_id == group.id).all()
        student_ids = [gs.student_id for gs in group_students]
        student_count = len(student_ids)
        total_students += student_count
        
        # Calculate average progress for this group
        avg_progress = 0
        if student_ids:
            progress_records = db.query(StudentProgress).filter(
                StudentProgress.user_id.in_(student_ids)
            ).all()
            if progress_records:
                avg_progress = sum(p.completion_percentage for p in progress_records) / len(progress_records)
        
        total_progress += avg_progress * student_count if student_count else 0
        
        # Calculate overdue for this group
        overdue_count = 0
        if student_ids:
            # 1. GroupAssignment source
            unsubmitted_ga = db.query(GroupAssignment, GroupStudent).join(
                GroupStudent, GroupAssignment.group_id == GroupStudent.group_id
            ).filter(
                GroupAssignment.group_id == group.id,
                GroupAssignment.due_date < datetime.utcnow(),
                GroupAssignment.is_active == True,
                GroupStudent.student_id.in_(student_ids),
                ~db.query(AssignmentSubmission).filter(
                    AssignmentSubmission.assignment_id == GroupAssignment.assignment_id,
                    AssignmentSubmission.user_id == GroupStudent.student_id,
                    AssignmentSubmission.is_hidden == False
                ).exists()
            ).count()
            
            late_ga = db.query(AssignmentSubmission).join(
                GroupAssignment, AssignmentSubmission.assignment_id == GroupAssignment.assignment_id
            ).filter(
                AssignmentSubmission.user_id.in_(student_ids),
                AssignmentSubmission.submitted_at > GroupAssignment.due_date,
                GroupAssignment.group_id == group.id,
                AssignmentSubmission.is_hidden == False
            ).count()
            
            # 2. Direct Assignment source
            unsubmitted_direct = db.query(Assignment, GroupStudent).join(
                GroupStudent, Assignment.group_id == GroupStudent.group_id
            ).filter(
                Assignment.group_id == group.id,
                Assignment.due_date < datetime.utcnow(),
                Assignment.is_active == True,
                Assignment.is_hidden == False,
                GroupStudent.student_id.in_(student_ids),
                ~db.query(AssignmentSubmission).filter(
                    AssignmentSubmission.assignment_id == Assignment.id,
                    AssignmentSubmission.user_id == GroupStudent.student_id,
                    AssignmentSubmission.is_hidden == False
                ).exists()
            ).count()
            
            late_direct = db.query(AssignmentSubmission).join(
                Assignment, AssignmentSubmission.assignment_id == Assignment.id
            ).filter(
                AssignmentSubmission.user_id.in_(student_ids),
                AssignmentSubmission.submitted_at > Assignment.due_date,
                Assignment.group_id == group.id,
                Assignment.is_active == True,
                Assignment.is_hidden == False,
                AssignmentSubmission.is_hidden == False
            ).count()
            
            overdue_count = unsubmitted_ga + late_ga + unsubmitted_direct + late_direct
        
        total_overdue += overdue_count
        
        # Get student details for this group
        students_list = []
        for student_id in student_ids:
            student = db.query(UserInDB).filter(UserInDB.id == student_id).first()
            if not student:
                continue
            
            # Calculate individual student progress
            student_progress_records = db.query(StudentProgress).filter(
                StudentProgress.user_id == student_id
            ).all()
            student_avg_progress = 0
            if student_progress_records:
                student_avg_progress = sum(p.completion_percentage for p in student_progress_records) / len(student_progress_records)
            
            # Calculate individual student overdue count
            student_overdue = 0
            
            # GroupAssignment overdues
            unsubmitted_ga_student = db.query(GroupAssignment).join(
                GroupStudent, GroupAssignment.group_id == GroupStudent.group_id
            ).filter(
                GroupAssignment.group_id == group.id,
                GroupAssignment.due_date < datetime.utcnow(),
                GroupAssignment.is_active == True,
                GroupStudent.student_id == student_id,
                ~db.query(AssignmentSubmission).filter(
                    AssignmentSubmission.assignment_id == GroupAssignment.assignment_id,
                    AssignmentSubmission.user_id == student_id,
                    AssignmentSubmission.is_hidden == False
                ).exists()
            ).count()
            
            late_ga_student = db.query(AssignmentSubmission).join(
                GroupAssignment, AssignmentSubmission.assignment_id == GroupAssignment.assignment_id
            ).filter(
                AssignmentSubmission.user_id == student_id,
                AssignmentSubmission.submitted_at > GroupAssignment.due_date,
                GroupAssignment.group_id == group.id,
                AssignmentSubmission.is_hidden == False
            ).count()
            
            # Direct Assignment overdues
            unsubmitted_direct_student = db.query(Assignment).join(
                GroupStudent, Assignment.group_id == GroupStudent.group_id
            ).filter(
                Assignment.group_id == group.id,
                Assignment.due_date < datetime.utcnow(),
                Assignment.is_active == True,
                Assignment.is_hidden == False,
                GroupStudent.student_id == student_id,
                ~db.query(AssignmentSubmission).filter(
                    AssignmentSubmission.assignment_id == Assignment.id,
                    AssignmentSubmission.user_id == student_id,
                    AssignmentSubmission.is_hidden == False
                ).exists()
            ).count()
            
            late_direct_student = db.query(AssignmentSubmission).join(
                Assignment, AssignmentSubmission.assignment_id == Assignment.id
            ).filter(
                AssignmentSubmission.user_id == student_id,
                AssignmentSubmission.submitted_at > Assignment.due_date,
                Assignment.group_id == group.id,
                Assignment.is_active == True,
                Assignment.is_hidden == False,
                AssignmentSubmission.is_hidden == False
            ).count()
            
            student_overdue = unsubmitted_ga_student + late_ga_student + unsubmitted_direct_student + late_direct_student
            
            students_list.append({
                "id": student.id,
                "name": student.name,
                "email": student.email,
                "avatar_url": student.avatar_url,
                "avg_progress": round(student_avg_progress, 1),
                "overdue_count": student_overdue
            })
        
        groups_data.append({
            "id": group.id,
            "name": group.name,
            "student_count": student_count,
            "overdue_count": overdue_count,
            "avg_progress": round(avg_progress, 1),
            "students": students_list
        })
    
    overall_avg_progress = round(total_progress / total_students, 1) if total_students > 0 else 0
    
    # Calculate performance distribution
    performance_distribution = [
        {"range": "0-20%", "count": 0},
        {"range": "20-40%", "count": 0},
        {"range": "40-60%", "count": 0},
        {"range": "60-80%", "count": 0},
        {"range": "80-100%", "count": 0}
    ]
    
    # Count students in each progress range
    all_student_ids = []
    for group in curator_groups:
        group_students = db.query(GroupStudent).filter(GroupStudent.group_id == group.id).all()
        all_student_ids.extend([gs.student_id for gs in group_students])
    
    for student_id in all_student_ids:
        progress_records = db.query(StudentProgress).filter(
            StudentProgress.user_id == student_id
        ).all()
        if progress_records:
            avg = sum(p.completion_percentage for p in progress_records) / len(progress_records)
            if avg < 20:
                performance_distribution[0]["count"] += 1
            elif avg < 40:
                performance_distribution[1]["count"] += 1
            elif avg < 60:
                performance_distribution[2]["count"] += 1
            elif avg < 80:
                performance_distribution[3]["count"] += 1
            else:
                performance_distribution[4]["count"] += 1
    
    # Calculate overdu history (last 30 days)
    overdue_history = []
    for i in range(30):
        day = (datetime.utcnow() - timedelta(days=29 - i)).date()
        
        # Count overdue assignments on this specific day
        day_start = datetime.combine(day, datetime.min.time())
        day_end = datetime.combine(day, datetime.max.time())
        
        # Assignments that became overdue on this day (due_date is on this day and not submitted)
        overdue_on_day = 0
        
        # GroupAssignment source
        ga_overdue_unsubmitted = db.query(GroupAssignment, GroupStudent).join(
            GroupStudent, GroupAssignment.group_id == GroupStudent.group_id
        ).filter(
            GroupAssignment.group_id.in_([g.id for g in curator_groups]),
            func.date(GroupAssignment.due_date) == day,
            GroupAssignment.is_active == True,
            GroupStudent.student_id.in_(all_student_ids) if all_student_ids else False,
            ~db.query(AssignmentSubmission).filter(
                AssignmentSubmission.assignment_id == GroupAssignment.assignment_id,
                AssignmentSubmission.user_id == GroupStudent.student_id,
                AssignmentSubmission.is_hidden == False
            ).exists()
        ).count()

        ga_overdue_late = db.query(GroupAssignment, GroupStudent).join(
            GroupStudent, GroupAssignment.group_id == GroupStudent.group_id
        ).join(
            AssignmentSubmission, 
            (AssignmentSubmission.assignment_id == GroupAssignment.assignment_id) & 
            (AssignmentSubmission.user_id == GroupStudent.student_id)
        ).filter(
            GroupAssignment.group_id.in_([g.id for g in curator_groups]),
            func.date(GroupAssignment.due_date) == day,
            GroupAssignment.is_active == True,
            GroupStudent.student_id.in_(all_student_ids) if all_student_ids else False,
            AssignmentSubmission.submitted_at > GroupAssignment.due_date,
            AssignmentSubmission.is_hidden == False
        ).count()

        # Direct Assignment source
        direct_overdue_unsubmitted = db.query(Assignment).join(
            GroupStudent, Assignment.group_id == GroupStudent.group_id
        ).filter(
            Assignment.group_id.in_([g.id for g in curator_groups]),
            func.date(Assignment.due_date) == day,
            Assignment.is_active == True,
            Assignment.is_hidden == False,
            GroupStudent.student_id.in_(all_student_ids) if all_student_ids else False,
            ~db.query(AssignmentSubmission).filter(
                AssignmentSubmission.assignment_id == Assignment.id,
                AssignmentSubmission.user_id == GroupStudent.student_id,
                AssignmentSubmission.is_hidden == False
            ).exists()
        ).count()

        direct_overdue_late = db.query(Assignment).join(
            GroupStudent, Assignment.group_id == GroupStudent.group_id
        ).join(
            AssignmentSubmission,
            (AssignmentSubmission.assignment_id == Assignment.id) &
            (AssignmentSubmission.user_id == GroupStudent.student_id)
        ).filter(
            Assignment.group_id.in_([g.id for g in curator_groups]),
            func.date(Assignment.due_date) == day,
            Assignment.is_active == True,
            Assignment.is_hidden == False,
            GroupStudent.student_id.in_(all_student_ids) if all_student_ids else False,
            AssignmentSubmission.submitted_at > Assignment.due_date,
            AssignmentSubmission.is_hidden == False
        ).count()
        
        overdue_on_day += ga_overdue_unsubmitted + ga_overdue_late + direct_overdue_unsubmitted + direct_overdue_late
        
        overdue_history.append({
            "date": day.isoformat(),
            "count": overdue_on_day
        })
    
    # Group comparison data
    group_comparison = []
    for group_data in groups_data:
        group_comparison.append({
            "group_name": group_data["name"],
            "avg_progress": group_data["avg_progress"],
            "student_count": group_data["student_count"],
            "overdue_count": group_data["overdue_count"]
        })
    
    return {
        "id": curator.id,
        "name": curator.name,
        "email": curator.email,
        "avatar_url": curator.avatar_url,
        "groups": groups_data,
        "total_students": total_students,
        "total_overdue": total_overdue,
        "avg_progress": overall_avg_progress,
        "performance_distribution": performance_distribution,
        "overdue_history": overdue_history,
        "group_comparison": group_comparison
    }

@router.get("/my-courses", response_model=List[CourseProgressSchema])
async def get_my_courses(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get detailed list of user's courses with progress"""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can access this endpoint")
    
    # Get student's enrollments
    enrollments = db.query(Enrollment).filter(
        Enrollment.user_id == current_user.id,
        Enrollment.is_active == True
    ).all()
    
    # Get group access courses
    from src.schemas.models import GroupStudent, CourseGroupAccess
    
    group_student = db.query(GroupStudent).filter(
        GroupStudent.student_id == current_user.id
    ).first()
    
    group_courses = []
    if group_student:
        group_access = db.query(CourseGroupAccess).filter(
            CourseGroupAccess.group_id == group_student.group_id,
            CourseGroupAccess.is_active == True
        ).all()
        
        for access in group_access:
            course = db.query(Course).filter(
                Course.id == access.course_id,
                Course.is_active == True
            ).first()
            if course:
                group_courses.append(course)
    
    # Combine both sets of courses (enrollment + group access)
    all_courses = []
    
    # Add enrollment courses
    for enrollment in enrollments:
        course = db.query(Course).filter(
            Course.id == enrollment.course_id,
            Course.is_active == True
        ).first()
        if course:
            all_courses.append(course)
    
    # Add group access courses (avoid duplicates)
    enrollment_course_ids = [e.course_id for e in enrollments]
    for course in group_courses:
        if course.id not in enrollment_course_ids:
            all_courses.append(course)
    
    courses_with_progress = []
    
    for course in all_courses:
        # Get teacher info
        teacher = db.query(UserInDB).filter(UserInDB.id == course.teacher_id).first()
        teacher_name = teacher.name if teacher else "Unknown Teacher"
        
        # Count total modules
        total_modules = db.query(Module).filter(Module.course_id == course.id).count()
        
        # Calculate progress
        progress_records = db.query(StudentProgress).filter(
            StudentProgress.user_id == current_user.id,
            StudentProgress.course_id == course.id
        ).all()
        
        if progress_records:
            completion_percentage = round(sum(p.completion_percentage for p in progress_records) / len(progress_records))
            last_accessed = max(p.last_accessed for p in progress_records)
        else:
            completion_percentage = 0
            # Use current time for group access courses that haven't been accessed yet
            last_accessed = datetime.utcnow()
        
        # Determine status
        if completion_percentage >= 100:
            status = "completed"
        elif completion_percentage > 0:
            status = "in_progress"
        else:
            status = "not_started"
        
        courses_with_progress.append(CourseProgressSchema(
            course_id=course.id,
            course_title=course.title,
            teacher_name=teacher_name,
            cover_image_url=course.cover_image_url,
            total_modules=total_modules,
            completion_percentage=completion_percentage,
            status=status,
            last_accessed=last_accessed
        ))
    
    # Sort by last accessed (most recent first)
    courses_with_progress.sort(key=lambda x: x.last_accessed or datetime.min, reverse=True)
    
    return courses_with_progress

@router.get("/recent-activity")
async def get_recent_activity(
    limit: int = 10,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get recent learning activity for current user"""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can access this endpoint")
    
    # Get recent progress records
    recent_progress = db.query(StudentProgress).filter(
        StudentProgress.user_id == current_user.id
    ).order_by(desc(StudentProgress.last_accessed)).limit(limit).all()
    
    activities = []
    
    for progress in recent_progress:
        course = db.query(Course).filter(Course.id == progress.course_id).first()
        lesson = db.query(Lesson).filter(Lesson.id == progress.lesson_id).first() if progress.lesson_id else None
        
        activity = {
            "id": progress.id,
            "type": "lesson" if lesson else "course",
            "course_title": course.title if course else "Unknown Course",
            "lesson_title": lesson.title if lesson else None,
            "progress": progress.completion_percentage,
            "status": progress.status,
            "time_spent": progress.time_spent_minutes,
            "last_accessed": progress.last_accessed
        }
        activities.append(activity)
    
    return {"recent_activities": activities}

@router.post("/update-study-time")
async def update_study_time(
    minutes_studied: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Update user's total study time"""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can update study time")
    
    current_user.total_study_time_minutes += minutes_studied
    db.commit()
    
    return {
        "detail": "Study time updated successfully",
        "total_study_time_minutes": current_user.total_study_time_minutes,
        "total_study_time_hours": current_user.total_study_time_minutes // 60
    }

@router.get("/teacher/pending-submissions")
async def get_teacher_pending_submissions(
    limit: Optional[int] = Query(None, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get pending submissions for teacher's students (from teacher's groups)"""
    if current_user.role not in ["teacher", "admin"]:
        raise HTTPException(status_code=403, detail="Only teachers can access this endpoint")
    
    from src.schemas.models import Assignment, AssignmentSubmission, CourseGroupAccess, Group
    
    # Get teacher's groups
    teacher_groups = db.query(Group).filter(
        Group.teacher_id == current_user.id,
        Group.is_active == True
    ).all()
    
    if not teacher_groups:
        return {"pending_submissions": [], "total_pending_count": 0, "has_more": False}
    
    teacher_group_ids = [g.id for g in teacher_groups]
    
    # Get students from teacher's groups
    teacher_student_ids = set()
    group_students = db.query(GroupStudent).filter(
        GroupStudent.group_id.in_(teacher_group_ids)
    ).all()
    for gs in group_students:
        teacher_student_ids.add(gs.student_id)
    
    if not teacher_student_ids:
        return {"pending_submissions": [], "total_pending_count": 0, "has_more": False}
    
    # Get courses that teacher's groups have access to
    course_ids = db.query(CourseGroupAccess.course_id).filter(
        CourseGroupAccess.group_id.in_(teacher_group_ids),
        CourseGroupAccess.is_active == True
    ).distinct().all()
    course_ids = [c[0] for c in course_ids]
    
    if not course_ids:
        return {"pending_submissions": [], "total_pending_count": 0, "has_more": False}
    
    # Get assignments from those courses
    teacher_assignments = db.query(Assignment).filter(
        Assignment.lesson_id.in_(
            db.query(Lesson.id).filter(
                Lesson.module_id.in_(
                    db.query(Module.id).filter(
                        Module.course_id.in_(course_ids)
                    )
                )
            )
        ),
        Assignment.is_active == True
    ).all()
    
    # Add assignments directly linked to groups
    group_assignments = db.query(Assignment).filter(
        Assignment.group_id.in_(teacher_group_ids),
        Assignment.is_active == True
    ).all()
    teacher_assignments.extend(group_assignments)
    assignment_ids = list({a.id for a in teacher_assignments})

    if not assignment_ids:
        return {"pending_submissions": [], "total_pending_count": 0, "has_more": False}
    
    # Get pending submissions ONLY from teacher's students
    pending_query = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.assignment_id.in_(assignment_ids),
        AssignmentSubmission.user_id.in_(teacher_student_ids),
        AssignmentSubmission.is_graded == False
    )
    total_pending_count = pending_query.count()

    pending_query = pending_query.order_by(AssignmentSubmission.submitted_at.desc())
    if limit is not None:
        pending_query = pending_query.offset(offset).limit(limit)

    pending_submissions = pending_query.all()
    if not pending_submissions:
        return {"pending_submissions": [], "total_pending_count": total_pending_count, "has_more": False}

    submission_assignment_ids = list({s.assignment_id for s in pending_submissions})
    submission_student_ids = list({s.user_id for s in pending_submissions})

    assignments = db.query(Assignment).filter(Assignment.id.in_(submission_assignment_ids)).all()
    assignments_map = {a.id: a for a in assignments}

    students = db.query(UserInDB).filter(UserInDB.id.in_(submission_student_ids)).all()
    students_map = {s.id: s for s in students}

    lesson_ids = list({a.lesson_id for a in assignments if a.lesson_id is not None})
    lessons = db.query(Lesson).filter(Lesson.id.in_(lesson_ids)).all() if lesson_ids else []
    lessons_map = {l.id: l for l in lessons}

    module_ids = list({l.module_id for l in lessons if l.module_id is not None})
    modules = db.query(Module).filter(Module.id.in_(module_ids)).all() if module_ids else []
    modules_map = {m.id: m for m in modules}

    course_ids_for_assignments = list({m.course_id for m in modules if m.course_id is not None})
    courses = db.query(Course).filter(Course.id.in_(course_ids_for_assignments)).all() if course_ids_for_assignments else []
    courses_map = {c.id: c for c in courses}

    submissions_data = []
    for submission in pending_submissions:
        assignment = assignments_map.get(submission.assignment_id)
        student = students_map.get(submission.user_id)

        course_title = "Unknown Course"
        if assignment and assignment.lesson_id:
            lesson = lessons_map.get(assignment.lesson_id)
            if lesson:
                module = modules_map.get(lesson.module_id)
                if module:
                    course = courses_map.get(module.course_id)
                    if course:
                        course_title = course.title

        submissions_data.append({
            "id": submission.id,
            "assignment_id": submission.assignment_id,
            "assignment_title": assignment.title if assignment else "Unknown Assignment",
            "course_title": course_title,
            "user_id": submission.user_id,
            "student_name": student.name if student else "Unknown Student",
            "student_email": student.email if student else "",
            "submitted_at": submission.submitted_at,
            "max_score": submission.max_score,
            "file_url": submission.file_url,
            "submitted_file_name": submission.submitted_file_name
        })
    
    has_more = (offset + len(submissions_data)) < total_pending_count if limit is not None else False
    return {
        "pending_submissions": submissions_data,
        "total_pending_count": total_pending_count,
        "has_more": has_more
    }


@router.get("/teacher/auto-grade-unit-homework/preview")
async def get_teacher_auto_grade_unit_homework_preview(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    if current_user.role not in ["teacher", "admin"]:
        raise HTTPException(status_code=403, detail="Only teachers can access this endpoint")

    from src.schemas.models import Assignment, AssignmentSubmission, CourseGroupAccess, Group

    teacher_groups = db.query(Group).filter(
        Group.teacher_id == current_user.id,
        Group.is_active == True
    ).all()
    if not teacher_groups:
        return {"eligible_count": 0, "items": []}

    teacher_group_ids = [g.id for g in teacher_groups]

    teacher_student_ids = {
        gs.student_id
        for gs in db.query(GroupStudent).filter(
            GroupStudent.group_id.in_(teacher_group_ids)
        ).all()
    }
    if not teacher_student_ids:
        return {"eligible_count": 0, "items": []}

    course_ids = [
        c[0] for c in db.query(CourseGroupAccess.course_id).filter(
            CourseGroupAccess.group_id.in_(teacher_group_ids),
            CourseGroupAccess.is_active == True
        ).distinct().all()
    ]
    if not course_ids:
        return {"eligible_count": 0, "items": []}

    lesson_based_assignments = db.query(Assignment).filter(
        Assignment.lesson_id.in_(
            db.query(Lesson.id).filter(
                Lesson.module_id.in_(
                    db.query(Module.id).filter(Module.course_id.in_(course_ids))
                )
            )
        ),
        Assignment.is_active == True
    ).all()

    group_assignments = db.query(Assignment).filter(
        Assignment.group_id.in_(teacher_group_ids),
        Assignment.is_active == True
    ).all()

    assignment_map = {a.id: a for a in lesson_based_assignments + group_assignments}
    if not assignment_map:
        return {"eligible_count": 0, "items": []}

    pending_submissions = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.assignment_id.in_(list(assignment_map.keys())),
        AssignmentSubmission.user_id.in_(teacher_student_ids),
        AssignmentSubmission.is_graded == False
    ).all()
    if not pending_submissions:
        return {"eligible_count": 0, "items": []}

    def is_unit_only_multitask(assignment: Assignment) -> bool:
        if assignment.assignment_type != "multi_task":
            return False

        try:
            content = json.loads(assignment.content) if isinstance(assignment.content, str) else assignment.content
        except Exception:
            return False

        if not isinstance(content, dict):
            return False

        tasks = content.get("tasks", [])
        if not isinstance(tasks, list) or len(tasks) == 0:
            return False

        return all(
            isinstance(task, dict) and task.get("task_type") == "course_unit"
            for task in tasks
        )

    student_ids = list({sub.user_id for sub in pending_submissions})
    students = db.query(UserInDB).filter(UserInDB.id.in_(student_ids)).all() if student_ids else []
    students_map = {s.id: s for s in students}

    items = []
    for submission in pending_submissions:
        assignment = assignment_map.get(submission.assignment_id)
        if not assignment or not is_unit_only_multitask(assignment):
            continue

        student = students_map.get(submission.user_id)
        items.append({
            "submission_id": submission.id,
            "assignment_id": assignment.id,
            "assignment_title": assignment.title,
            "student_name": student.name if student else "Unknown Student",
            "student_email": student.email if student else "",
            "submitted_at": submission.submitted_at,
            "target_score": submission.max_score
        })

    items.sort(key=lambda x: x["submitted_at"] if x["submitted_at"] is not None else datetime.min, reverse=True)

    return {"eligible_count": len(items), "items": items}


@router.post("/teacher/auto-grade-unit-homework")
async def auto_grade_teacher_unit_homework(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Auto-grade pending homework submissions with max score when:
    - assignment is multi_task
    - all tasks in assignment are course_unit tasks
    """
    if current_user.role not in ["teacher", "admin"]:
        raise HTTPException(status_code=403, detail="Only teachers can access this endpoint")

    from src.schemas.models import Assignment, AssignmentSubmission, CourseGroupAccess, Group

    teacher_groups = db.query(Group).filter(
        Group.teacher_id == current_user.id,
        Group.is_active == True
    ).all()

    if not teacher_groups:
        return {"graded_count": 0, "eligible_count": 0}

    teacher_group_ids = [g.id for g in teacher_groups]

    teacher_student_ids = {
        gs.student_id
        for gs in db.query(GroupStudent).filter(
            GroupStudent.group_id.in_(teacher_group_ids)
        ).all()
    }

    if not teacher_student_ids:
        return {"graded_count": 0, "eligible_count": 0}

    course_ids = [
        c[0] for c in db.query(CourseGroupAccess.course_id).filter(
            CourseGroupAccess.group_id.in_(teacher_group_ids),
            CourseGroupAccess.is_active == True
        ).distinct().all()
    ]

    if not course_ids:
        return {"graded_count": 0, "eligible_count": 0}

    lesson_based_assignments = db.query(Assignment).filter(
        Assignment.lesson_id.in_(
            db.query(Lesson.id).filter(
                Lesson.module_id.in_(
                    db.query(Module.id).filter(Module.course_id.in_(course_ids))
                )
            )
        ),
        Assignment.is_active == True
    ).all()

    group_assignments = db.query(Assignment).filter(
        Assignment.group_id.in_(teacher_group_ids),
        Assignment.is_active == True
    ).all()

    assignment_map = {a.id: a for a in lesson_based_assignments + group_assignments}
    if not assignment_map:
        return {"graded_count": 0, "eligible_count": 0}

    pending_submissions = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.assignment_id.in_(list(assignment_map.keys())),
        AssignmentSubmission.user_id.in_(teacher_student_ids),
        AssignmentSubmission.is_graded == False
    ).all()

    def is_unit_only_multitask(assignment: Assignment) -> bool:
        if assignment.assignment_type != "multi_task":
            return False

        try:
            content = json.loads(assignment.content) if isinstance(assignment.content, str) else assignment.content
        except Exception:
            return False

        if not isinstance(content, dict):
            return False

        tasks = content.get("tasks", [])
        if not isinstance(tasks, list) or len(tasks) == 0:
            return False

        return all(
            isinstance(task, dict) and task.get("task_type") == "course_unit"
            for task in tasks
        )

    eligible_submissions = []
    for submission in pending_submissions:
        assignment = assignment_map.get(submission.assignment_id)
        if assignment and is_unit_only_multitask(assignment):
            eligible_submissions.append(submission)

    if not eligible_submissions:
        return {"graded_count": 0, "eligible_count": 0}

    graded_at = datetime.utcnow()
    for submission in eligible_submissions:
        submission.score = submission.max_score
        submission.feedback = "Auto-graded: unit-only homework completed"
        submission.graded_by = current_user.id
        submission.is_graded = True
        submission.graded_at = graded_at

    db.commit()

    return {
        "graded_count": len(eligible_submissions),
        "eligible_count": len(eligible_submissions)
    }

@router.get("/teacher/recent-submissions")
async def get_teacher_recent_submissions(
    limit: int = 10,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get recent submissions for teacher's students (from teacher's groups)"""
    if current_user.role not in ["teacher", "admin"]:
        raise HTTPException(status_code=403, detail="Only teachers can access this endpoint")
    
    from src.schemas.models import Assignment, AssignmentSubmission, CourseGroupAccess, Group
    
    # Get teacher's groups
    teacher_groups = db.query(Group).filter(
        Group.teacher_id == current_user.id,
        Group.is_active == True
    ).all()
    
    if not teacher_groups:
        return {"recent_submissions": []}
    
    teacher_group_ids = [g.id for g in teacher_groups]
    
    # Get students from teacher's groups
    teacher_student_ids = set()
    group_students = db.query(GroupStudent).filter(
        GroupStudent.group_id.in_(teacher_group_ids)
    ).all()
    for gs in group_students:
        teacher_student_ids.add(gs.student_id)
    
    if not teacher_student_ids:
        return {"recent_submissions": []}
    
    # Get courses that teacher's groups have access to
    course_ids = db.query(CourseGroupAccess.course_id).filter(
        CourseGroupAccess.group_id.in_(teacher_group_ids),
        CourseGroupAccess.is_active == True
    ).distinct().all()
    course_ids = [c[0] for c in course_ids]
    
    if not course_ids:
        return {"recent_submissions": []}
    
    # Get assignments from those courses
    teacher_assignments = db.query(Assignment).filter(
        Assignment.lesson_id.in_(
            db.query(Lesson.id).filter(
                Lesson.module_id.in_(
                    db.query(Module.id).filter(
                        Module.course_id.in_(course_ids)
                    )
                )
            )
        ),
        Assignment.is_active == True
    ).all()
    
    # Add assignments directly linked to groups
    group_assignments = db.query(Assignment).filter(
        Assignment.group_id.in_(teacher_group_ids),
        Assignment.is_active == True
    ).all()
    teacher_assignments.extend(group_assignments)
    assignment_ids = list({a.id for a in teacher_assignments})

    if not assignment_ids:
        return {"recent_submissions": []}
    
    # Get recent submissions ONLY from teacher's students
    recent_submissions = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.assignment_id.in_(assignment_ids),
        AssignmentSubmission.user_id.in_(teacher_student_ids)
    ).order_by(AssignmentSubmission.submitted_at.desc()).limit(limit).all()

    if not recent_submissions:
        return {"recent_submissions": []}

    submission_assignment_ids = list({s.assignment_id for s in recent_submissions})
    submission_student_ids = list({s.user_id for s in recent_submissions})
    grader_ids = list({s.graded_by for s in recent_submissions if s.graded_by is not None})

    assignments = db.query(Assignment).filter(Assignment.id.in_(submission_assignment_ids)).all()
    assignments_map = {a.id: a for a in assignments}

    students = db.query(UserInDB).filter(UserInDB.id.in_(submission_student_ids)).all()
    students_map = {s.id: s for s in students}

    graders = db.query(UserInDB).filter(UserInDB.id.in_(grader_ids)).all() if grader_ids else []
    graders_map = {g.id: g for g in graders}

    lesson_ids = list({a.lesson_id for a in assignments if a.lesson_id is not None})
    lessons = db.query(Lesson).filter(Lesson.id.in_(lesson_ids)).all() if lesson_ids else []
    lessons_map = {l.id: l for l in lessons}

    module_ids = list({l.module_id for l in lessons if l.module_id is not None})
    modules = db.query(Module).filter(Module.id.in_(module_ids)).all() if module_ids else []
    modules_map = {m.id: m for m in modules}

    course_ids_for_assignments = list({m.course_id for m in modules if m.course_id is not None})
    courses = db.query(Course).filter(Course.id.in_(course_ids_for_assignments)).all() if course_ids_for_assignments else []
    courses_map = {c.id: c for c in courses}

    submissions_data = []
    for submission in recent_submissions:
        assignment = assignments_map.get(submission.assignment_id)
        student = students_map.get(submission.user_id)

        course_title = "Unknown Course"
        if assignment and assignment.lesson_id:
            lesson = lessons_map.get(assignment.lesson_id)
            if lesson:
                module = modules_map.get(lesson.module_id)
                if module:
                    course = courses_map.get(module.course_id)
                    if course:
                        course_title = course.title

        grader = graders_map.get(submission.graded_by) if submission.graded_by else None

        submissions_data.append({
            "id": submission.id,
            "assignment_id": submission.assignment_id,
            "assignment_title": assignment.title if assignment else "Unknown Assignment",
            "course_title": course_title,
            "user_id": submission.user_id,
            "student_name": student.name if student else "Unknown Student",
            "student_email": student.email if student else "",
            "submitted_at": submission.submitted_at,
            "graded_at": submission.graded_at,
            "score": submission.score,
            "max_score": submission.max_score,
            "is_graded": submission.is_graded,
            "feedback": submission.feedback,
            "grader_name": grader.name if grader else None,
            "file_url": submission.file_url,
            "submitted_file_name": submission.submitted_file_name
        })
    
    return {"recent_submissions": submissions_data}

@router.get("/teacher/students-progress")
async def get_teacher_students_progress(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get list of students with their current lesson progress for teacher's groups"""
    if current_user.role not in ["teacher", "admin"]:
        raise HTTPException(status_code=403, detail="Only teachers can access this endpoint")
    
    from src.schemas.models import CourseGroupAccess, Group
    
    # Get teacher's groups (groups where this teacher is the owner)
    teacher_groups = db.query(Group).filter(
        Group.teacher_id == current_user.id,
        Group.is_active == True
    ).all()
    
    if not teacher_groups:
        return {"students_progress": []}
    
    teacher_group_ids = [g.id for g in teacher_groups]
    teacher_groups_map = {g.id: g for g in teacher_groups}
    
    # Get all students from teacher's groups with their group info
    group_student_records = db.query(GroupStudent).filter(
        GroupStudent.group_id.in_(teacher_group_ids)
    ).all()
    
    if not group_student_records:
        return {"students_progress": []}
    
    students_data = []
    student_ids_seen = set()
    
    # Process each student from teacher's groups
    for gs in group_student_records:
        if gs.student_id in student_ids_seen:
            continue
            
        student = db.query(UserInDB).filter(UserInDB.id == gs.student_id).first()
        if not student:
            continue
        
        group = teacher_groups_map.get(gs.group_id)
        group_name = group.name if group else None
        
        # Get courses that this student's group has access to
        group_course_access = db.query(CourseGroupAccess).filter(
            CourseGroupAccess.group_id == gs.group_id,
            CourseGroupAccess.is_active == True
        ).all()
        
        if not group_course_access:
            # Student has no courses assigned - still show them
            students_data.append({
                "student_id": student.id,
                "student_name": student.name,
                "student_email": student.email,
                "student_avatar": student.avatar_url,
                "group_name": group_name,
                "course_id": None,
                "course_title": "No courses assigned",
                "current_lesson_id": None,
                "current_lesson_title": "Not started",
                "lesson_progress": 0,
                "overall_progress": 0,
                "last_activity": None
            })
            student_ids_seen.add(gs.student_id)
            continue
        
        # For each course the student has access to
        for access in group_course_access:
            course = db.query(Course).filter(
                Course.id == access.course_id,
                Course.is_active == True
            ).first()
            
            if not course:
                continue
            
            # Find last accessed lesson through StepProgress
            last_step_progress = db.query(StepProgress).filter(
                StepProgress.user_id == student.id,
                StepProgress.course_id == course.id
            ).order_by(desc(StepProgress.visited_at)).first()
            
            current_lesson_title = None
            current_lesson_id = None
            lesson_progress_percentage = 0
            
            if last_step_progress:
                # Get the lesson from the step
                step = db.query(Step).filter(Step.id == last_step_progress.step_id).first()
                if step:
                    lesson = db.query(Lesson).filter(Lesson.id == step.lesson_id).first()
                    if lesson:
                        current_lesson_title = lesson.title
                        current_lesson_id = lesson.id
                        
                        # Calculate lesson progress (completed steps / total steps in lesson)
                        lesson_steps = db.query(Step).filter(Step.lesson_id == lesson.id).count()
                        completed_lesson_steps = db.query(StepProgress).filter(
                            StepProgress.user_id == student.id,
                            StepProgress.step_id.in_(
                                db.query(Step.id).filter(Step.lesson_id == lesson.id)
                            ),
                            StepProgress.status == 'completed'
                        ).count()
                        
                        lesson_progress_percentage = round((completed_lesson_steps / lesson_steps) * 100) if lesson_steps > 0 else 0
            
            # Calculate overall course progress and get last activity
            all_progress = db.query(StudentProgress).filter(
                StudentProgress.user_id == student.id,
                StudentProgress.course_id == course.id
            ).all()
            
            overall_progress = 0
            last_activity = None
            
            if all_progress:
                overall_progress = round(sum(p.completion_percentage for p in all_progress) / len(all_progress))
                last_activity = max(p.last_accessed for p in all_progress if p.last_accessed)

            students_data.append({
                "student_id": student.id,
                "student_name": student.name,
                "student_email": student.email,
                "student_avatar": student.avatar_url,
                "group_name": group_name,
                "course_id": course.id,
                "course_title": course.title,
                "current_lesson_id": current_lesson_id,
                "current_lesson_title": current_lesson_title or "Not started",
                "lesson_progress": lesson_progress_percentage,
                "overall_progress": overall_progress,
                "last_activity": last_activity
            })
        
        student_ids_seen.add(gs.student_id)
    
    # Sort by last activity (most recent first), then by student name
    students_data.sort(key=lambda x: (x["last_activity"] or datetime.min, x["student_name"]), reverse=True)
    
    return {"students_progress": students_data}







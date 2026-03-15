from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, and_, or_
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta, date
from io import BytesIO
import json
import logging
import httpx
import os
import re
from collections import defaultdict

from src.config import get_db
import asyncio
from src.schemas.models import (
    StudentProgress, Course, Module, Lesson, Assignment, Enrollment, 
    UserInDB, AssignmentSubmission, StepProgress, Step, GroupStudent,
    Group, ProgressSnapshot, QuizAttempt, StudentCourseSummary, CourseGroupAccess
)
from src.routes.auth import get_current_user_dependency
from src.utils.permissions import check_course_access, check_student_access
from src.services.excel_export_service import get_excel_export_service

router = APIRouter()

@router.get("/student/{student_id}/detailed")
async def get_detailed_student_analytics(
    student_id: int,
    course_id: Optional[int] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get comprehensive analytics for a specific student"""
    
    # Check permissions
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Verify student exists
    student = db.query(UserInDB).filter(
        UserInDB.id == student_id, 
        UserInDB.role == "student"
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    # Check access rights based on role using centralized permission check
    if current_user.role != "admin" and not check_student_access(student_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this student")
    
    # Get student's courses
    courses_query = db.query(Course).join(Enrollment).filter(
        Enrollment.user_id == student_id,
        Enrollment.is_active == True,
        Course.is_active == True
    )
    
    if course_id:
        courses_query = courses_query.filter(Course.id == course_id)
    
    courses = courses_query.all()
    
    analytics_data = {
        "student_info": {
            "id": student.id,
            "name": student.name,
            "email": student.email,
            "student_id": student.student_id,
            "total_study_time_minutes": student.total_study_time_minutes,
            "daily_streak": student.daily_streak,
            "last_activity_date": student.last_activity_date
        },
        "courses": []
    }
    
    for course in courses:
        # Get course modules and lessons
        modules = db.query(Module).filter(Module.course_id == course.id).order_by(Module.order_index).all()
        
        course_data = {
            "course_id": course.id,
            "course_title": course.title,
            "teacher_name": course.teacher.name if course.teacher else "Unknown",
            "modules": []
        }
        
        for module in modules:
            lessons = db.query(Lesson).filter(Lesson.module_id == module.id).order_by(Lesson.order_index).all()
            
            module_data = {
                "module_id": module.id,
                "module_title": module.title,
                "lessons": []
            }
            
            for lesson in lessons:
                # Get lesson steps
                steps = db.query(Step).filter(Step.lesson_id == lesson.id).order_by(Step.order_index).all()
                
                # Get step progress
                step_progress = db.query(StepProgress).filter(
                    StepProgress.user_id == student_id,
                    StepProgress.lesson_id == lesson.id
                ).all()
                
                # Get assignments and submissions
                assignments = db.query(Assignment).filter(Assignment.lesson_id == lesson.id).all()
                assignment_data = []
                
                for assignment in assignments:
                    submission = db.query(AssignmentSubmission).filter(
                        AssignmentSubmission.assignment_id == assignment.id,
                        AssignmentSubmission.user_id == student_id
                    ).first()
                    
                    assignment_data.append({
                        "assignment_id": assignment.id,
                        "assignment_title": assignment.title,
                        "assignment_type": assignment.assignment_type,
                        "max_score": assignment.max_score,
                        "submission": {
                            "submitted": bool(submission),
                            "score": submission.score if submission else None,
                            "submitted_at": submission.submitted_at if submission else None,
                            "is_graded": submission.is_graded if submission else False
                        } if submission else None
                    })
                
                # Analyze step completion patterns
                step_details = []
                for step in steps:
                    progress = next((sp for sp in step_progress if sp.step_id == step.id), None)
                    step_details.append({
                        "step_id": step.id,
                        "step_title": step.title,
                        "content_type": step.content_type,
                        "order_index": step.order_index,
                        "progress": {
                            "status": progress.status if progress else "not_started",
                            "visited_at": progress.visited_at if progress else None,
                            "completed_at": progress.completed_at if progress else None,
                            "time_spent_minutes": progress.time_spent_minutes if progress else 0
                        }
                    })
                
                lesson_data = {
                    "lesson_id": lesson.id,
                    "lesson_title": lesson.title,
                    "total_steps": len(steps),
                    "completed_steps": len([sp for sp in step_progress if sp.status == "completed"]),
                    "total_time_spent": sum(sp.time_spent_minutes for sp in step_progress),
                    "steps": step_details,
                    "assignments": assignment_data
                }
                
                module_data["lessons"].append(lesson_data)
            
            course_data["modules"].append(module_data)
        
        analytics_data["courses"].append(course_data)
    
    return analytics_data

@router.get("/course/{course_id}/overview")
async def get_course_analytics_overview(
    course_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get analytics overview for a specific course"""
    
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Check course access (now properly validates curator access via group students)
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")
    
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Get students with progress in this course (via StepProgress - source of truth)
    # This finds students who actually have learning activity, not just enrollment records
    # Get students with progress in this course (via StepProgress - source of truth)
    # This finds students who actually have learning activity, not just enrollment records
    students_query = db.query(UserInDB).join(
        StepProgress, StepProgress.user_id == UserInDB.id
    ).join(
        Step, StepProgress.step_id == Step.id
    ).join(
        Lesson, Step.lesson_id == Lesson.id
    ).join(
        Module, Lesson.module_id == Module.id
    ).filter(
        Module.course_id == course_id,
        UserInDB.role == "student",
        UserInDB.is_active == True
    ).distinct()
    
    students_with_progress = students_query.all()
    
    # Log debug info
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Analytics Debug - Course {course_id}: Found {len(students_with_progress)} students with progress")
    
    # Also get students enrolled but without progress yet
    enrolled_students_ids = db.query(Enrollment.user_id).filter(
        Enrollment.course_id == course_id,
        Enrollment.is_active == True
    ).subquery()
    
    enrolled_no_progress = db.query(UserInDB).filter(
        UserInDB.id.in_(enrolled_students_ids),
        UserInDB.role == "student",
        UserInDB.is_active == True
    ).all()

    # also get students from GROUPS assigned to this course
    # because they might not have explicit Enrollments yet
    group_access_subquery = db.query(CourseGroupAccess.group_id).filter(
        CourseGroupAccess.course_id == course_id,
        CourseGroupAccess.is_active == True
    ).subquery()

    group_students_ids = db.query(GroupStudent.student_id).filter(
        GroupStudent.group_id.in_(group_access_subquery)
    ).subquery()

    group_students_no_progress = db.query(UserInDB).filter(
        UserInDB.id.in_(group_students_ids),
        UserInDB.role == "student",
        UserInDB.is_active == True
    ).all()
    
    # Combine all lists (students with progress + enrolled + group members)
    enrolled_students_set = {s.id: s for s in students_with_progress}
    for student in enrolled_no_progress:
        if student.id not in enrolled_students_set:
            enrolled_students_set[student.id] = student
            
    for student in group_students_no_progress:
        if student.id not in enrolled_students_set:
            enrolled_students_set[student.id] = student
    
    enrolled_students = list(enrolled_students_set.values())
    
    # Privacy Filter: If teacher is not course owner, restrict to their own groups
    # This prevents specialized teachers from seeing students outside their jurisdiction
    if current_user.role == "teacher" and course.teacher_id != current_user.id:
        teacher_group_ids = [g.id for g in db.query(Group.id).filter(Group.teacher_id == current_user.id).all()]
        if teacher_group_ids:
            # Find students in these groups
            allowed_student_ids = [gs.student_id for gs in db.query(GroupStudent.student_id).filter(
                GroupStudent.group_id.in_(teacher_group_ids)
            ).all()]
            allowed_student_ids_set = set(allowed_student_ids)
            
            # Filter the final list
            enrolled_students = [s for s in enrolled_students if s.id in allowed_student_ids_set]
        else:
            # Teacher has no groups? Then they see no students.
            enrolled_students = []

    # Privacy Filter for curator: only students from curator's own groups
    if current_user.role == "curator":
        curator_group_ids = [g.id for g in db.query(Group.id).filter(Group.curator_id == current_user.id).all()]
        if curator_group_ids:
            allowed_student_ids = [gs.student_id for gs in db.query(GroupStudent.student_id).filter(
                GroupStudent.group_id.in_(curator_group_ids)
            ).all()]
            allowed_student_ids_set = set(allowed_student_ids)
            enrolled_students = [s for s in enrolled_students if s.id in allowed_student_ids_set]
        else:
            enrolled_students = []
    
    # Get course structure
    modules = db.query(Module).filter(Module.course_id == course_id).order_by(Module.order_index).all()
    total_lessons = 0
    total_steps = 0
    
    lesson_step_counts = {}
    for module in modules:
        lessons = db.query(Lesson).filter(Lesson.module_id == module.id).all()
        total_lessons += len(lessons)
        for lesson in lessons:
            steps = db.query(Step).filter(Step.lesson_id == lesson.id).all()
            total_steps += len(steps)
            lesson_step_counts[lesson.id] = len(steps)
    
    # Calculate engagement metrics
    step_progress_records = db.query(StepProgress).filter(
        StepProgress.course_id == course_id
    ).all()
    
    total_time_spent = sum(sp.time_spent_minutes for sp in step_progress_records)
    completed_steps = len([sp for sp in step_progress_records if sp.status == "completed"])
    
    # Pre-fetch groups for students to avoid N+1
    student_ids = [s.id for s in enrolled_students]
    student_groups_map = {}
    if student_ids:
        # Get groups for these students
        # Note: A student might be in multiple groups, we take the first found one for display
        group_rows = db.query(GroupStudent.student_id, Group.name).join(
            Group, GroupStudent.group_id == Group.id
        ).filter(
            GroupStudent.student_id.in_(student_ids)
        ).all()
        
        for sid, gname in group_rows:
            if sid not in student_groups_map:
                student_groups_map[sid] = gname

    # Pre-fetch assignments
    assignments = db.query(Assignment).join(Lesson).join(Module).filter(
        Module.course_id == course_id
    ).all()
    
    total_assignments_count = len(assignments)
    logger.info(f"Course {course_id}: Found {total_assignments_count} assignments")

    # Pre-fetch lesson titles to map lesson_id -> title
    # We can get all lessons in the course via Module
    lesson_titles = {}
    course_lessons = db.query(Lesson.id, Lesson.title).join(Module).filter(
        Module.course_id == course_id
    ).all()
    for lid, ltitle in course_lessons:
        lesson_titles[lid] = ltitle

    # --- External SAT Data Fetching ---
    # Fetch SAT scores for all students in parallel
    # This is required because "Latest Test" refers to the external SAT system, not internal Quizzes
    
    api_key = os.getenv("MASTEREDU_API_KEY")
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json"
    }
    
    student_sat_map = {} # student_id -> latest_test_result
    
    # Main fetching logic
    max_sat_enrichment_students = 120
    if len(enrolled_students) > max_sat_enrichment_students:
        logger.warning(
            f"Skipping SAT enrichment for course {course_id}: too many students ({len(enrolled_students)} > {max_sat_enrichment_students})"
        )
    elif enrolled_students and api_key:
        students_with_email = [s for s in enrolled_students if s.email and s.email.strip()]
        missing_email_count = len(enrolled_students) - len(students_with_email)
        if missing_email_count > 0:
            logger.warning(
                f"Skipping SAT enrichment for {missing_email_count} students without email in course {course_id}"
            )

        email_to_student = {s.email.lower(): s for s in students_with_email}
        emails = list(email_to_student.keys())
        
        async def fetch_sat_for_student(student_obj, client_session):
            """Helper for single student fetch (fallback)"""
            if not student_obj.email:
                return student_obj.id, None
            email = student_obj.email.lower()
            try:
                # Try latest-test-details first (efficient)
                url = f"https://api.mastereducation.kz/api/lms/students/{email}/latest-test-details"
                response = await client_session.get(url, headers=headers, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    if "error" not in data:
                        return student_obj.id, data
                    else:
                        logger.warning(f"latest-test-details failed for {email}: {data.get('error')}")
                
                # If that failed or returned error, try full test-results (robust)
                url_history = f"https://api.mastereducation.kz/api/lms/students/{email}/test-results"
                response = await client_session.get(url_history, headers=headers, timeout=15.0)
                if response.status_code == 200:
                    data = response.json()
                    # test-results returns { "testPairs": [...] }
                    pairs = data.get("testPairs", [])
                    if pairs and len(pairs) > 0:
                        # Extract most recent pair and format it to look like latest-test-details
                        latest = pairs[0]
                        return student_obj.id, {
                            "mathTest": latest.get("mathTest"),
                            "verbalTest": latest.get("verbalTest"),
                            "combinedScore": latest.get("combinedScore")
                        }
            except Exception as e:
                logger.error(f"Fallback SAT fetch for {email} failed: {e}")
            return student_obj.id, None

        def parse_sat_data(student_obj_id, data):
            """Helper to parse raw SAT data into our format"""
            if not data: return None
            
            math_test = data.get("mathTest")
            verbal_test = data.get("verbalTest")
            total_sat_score = data.get("combinedScore") or 0
            
            # Fallback for combinedScore if missing but math/verbal exist
            if not total_sat_score:
                total_sat_score = (math_test.get("score") or 0) if math_test else 0
                total_sat_score += (verbal_test.get("score") or 0) if verbal_test else 0

            math_pct = 0
            verbal_pct = 0
            math_correct = 0
            math_max = 0
            verbal_correct = 0
            verbal_max = 0
            title = "SAT Practice"
            
            if math_test:
                questions = math_test.get("questions", [])
                math_q = [q for q in questions if q.get("questionType") == "Math"] or questions
                math_correct = len([q for q in math_q if q.get("isCorrect")])
                math_max = len(math_q)
                math_pct = (math_correct / math_max * 100) if math_max > 0 else 0
                title = math_test.get("testName") or title
            
            if verbal_test:
                questions = verbal_test.get("questions", [])
                verbal_q = [q for q in questions if q.get("questionType") == "Verbal"] or questions
                verbal_correct = len([q for q in verbal_q if q.get("isCorrect")])
                verbal_max = len(verbal_q)
                verbal_pct = (verbal_correct / verbal_max * 100) if verbal_max > 0 else 0
                if not math_test: title = verbal_test.get("testName") or title

            if math_pct > 0 and verbal_pct > 0:
                overall_pct = (math_pct + verbal_pct) / 2
            else:
                overall_pct = math_pct or verbal_pct or 0
            
            # Crucial: if we have NO scores yet date is missing, return None to avoid overwriting 
            # legit internal quiz results with an empty SAT record
            sat_date = (math_test.get("completedAt") or math_test.get("date")) if math_test else (verbal_test.get("completedAt") or verbal_test.get("date")) if verbal_test else None
            if not sat_date and math_correct == 0 and verbal_correct == 0:
                return None

            return {
                "title": title,
                "score": total_sat_score,
                "max_score": 1600,
                "percentage": round(overall_pct, 1),
                "type": "sat",
                "math_percent": round(math_pct, 1),
                "verbal_percent": round(verbal_pct, 1),
                "math_score": math_correct,
                "math_max": math_max,
                "verbal_score": verbal_correct,
                "verbal_max": verbal_max,
                "date": sat_date
            }

        async with httpx.AsyncClient() as client:
            batch_success = False
            try:
                batch_url = "https://api.mastereducation.kz/api/lms/students/latest-test-details"
                response = await client.post(batch_url, headers=headers, json={"emails": emails, "limit": 100}, timeout=4.0)
                
                if response.status_code == 200:
                    batch_data = response.json()
                    # If batch failed globally (MasterEDU crash)
                    if "error" in batch_data and not batch_data.get("results"):
                        logger.warning(f"Batch SAT API returned global error: {batch_data['error']}")
                    else:
                        results = batch_data.get("results", [])
                        for item in results:
                            email = item.get("email", "").lower()
                            data = item.get("data")
                            student_obj = email_to_student.get(email)
                            if student_obj and data:
                                parsed = parse_sat_data(student_obj.id, data)
                                if parsed: student_sat_map[student_obj.id] = parsed
                        batch_success = True
                elif response.status_code in [401, 403]:
                    logger.warning(f"Batch SAT API unauthorized ({response.status_code}). Skipping SAT enrichment for this request.")
            except Exception as e:
                logger.error(f"Batch SAT request failed: {e}")

            # SURGICAL FALLBACK: Fetch individual results for anyone STILL missing
            missing_students = [s for s in enrolled_students if s.id not in student_sat_map]
            
            # FAIL-FAST: If too many are missing, the batch API might be broken or we're stressing it
            # Fetching 200+ individuals sequentially/parallelly is too slow and risky
            if missing_students and batch_success:
                if len(missing_students) > 50:
                    logger.warning(f"Surgical fallback skipped: {len(missing_students)} students missing SAT data. Batch API might be failing or data missing at source.")
                else:
                    logger.info(f"Surgical fallback: Fetching individual SAT for {len(missing_students)} missing students")
                    # Use semaphore to limit concurrency
                    semaphore = asyncio.Semaphore(10)
                    
                    async def sem_fetch(student_obj):
                        async with semaphore:
                            return await fetch_sat_for_student(student_obj, client)
                            
                    tasks = [sem_fetch(s) for s in missing_students]
                    individual_results = await asyncio.gather(*tasks)
                    for sid, data in individual_results:
                        if data:
                            parsed = parse_sat_data(sid, data)
                            if parsed: student_sat_map[sid] = parsed
    elif enrolled_students and not api_key:
        logger.warning("MASTEREDU_API_KEY is not configured. Skipping external SAT enrichment.")
    # Pre-fetch Quiz Attempts (Internal) as fallback (optional, or remove if strictly separated)
    # Keeping it compatible with previous logic but External overrides
    quiz_attempts_query = db.query(QuizAttempt).filter(
        QuizAttempt.course_id == course_id,
        QuizAttempt.is_draft == False
    ).all()
    
    # Map user_id -> list of attempts
    student_quiz_attempts = {}
    for attempt in quiz_attempts_query:
        if attempt.user_id not in student_quiz_attempts:
            student_quiz_attempts[attempt.user_id] = []
        student_quiz_attempts[attempt.user_id].append(attempt)

    # Student performance summary
    student_performance = []
    
    # Bulk fetch student groups and group assignments to avoid N+1 in loop
    student_group_ids_map = {} # student_id -> list of group_ids
    all_group_ids = set()
    
    if enrolled_students:
        s_ids = [s.id for s in enrolled_students]
        gs_rows = db.query(GroupStudent.student_id, GroupStudent.group_id).filter(
            GroupStudent.student_id.in_(s_ids)
        ).all()
        for sid, gid in gs_rows:
            if sid not in student_group_ids_map:
                student_group_ids_map[sid] = []
            student_group_ids_map[sid].append(gid)
            all_group_ids.add(gid)
            
    group_assignments_map = {} # group_id -> list of assignments
    if all_group_ids:
        g_assignments = db.query(Assignment).filter(
            Assignment.group_id.in_(list(all_group_ids)),
            Assignment.is_active == True
        ).all()
        for asm in g_assignments:
            if asm.group_id not in group_assignments_map:
                group_assignments_map[asm.group_id] = []
            group_assignments_map[asm.group_id].append(asm)

    # Bulk-load submissions to avoid N+1 queries inside student/assignment loop
    latest_submission_map = {}
    if student_ids and all_group_ids:
        assignment_ids = sorted({
            asm.id
            for assignments_list in group_assignments_map.values()
            for asm in assignments_list
        })
        if assignment_ids:
            submissions = db.query(AssignmentSubmission).filter(
                AssignmentSubmission.assignment_id.in_(assignment_ids),
                AssignmentSubmission.user_id.in_(student_ids)
            ).all()

            for submission in submissions:
                key = (submission.user_id, submission.assignment_id)
                current_date = submission.submitted_at or submission.created_at
                existing_submission = latest_submission_map.get(key)

                if not existing_submission:
                    latest_submission_map[key] = submission
                    continue

                existing_date = existing_submission.submitted_at or existing_submission.created_at
                if current_date and (not existing_date or current_date > existing_date):
                    latest_submission_map[key] = submission

    for student in enrolled_students:
        student_steps = [sp for sp in step_progress_records if sp.user_id == student.id]
        student_completed = len([sp for sp in student_steps if sp.status == "completed"])
        student_time = sum(sp.time_spent_minutes for sp in student_steps)
        
        # Determine current lesson / last activity
        last_activity = None
        current_lesson_title = "Not started"
        
        # Sort by last_accessed descending to find most detailed activity
        if student_steps:
             active_steps = [s for s in student_steps if s.visited_at]
             if active_steps:
                 latest_step = max(active_steps, key=lambda x: x.visited_at)
                 last_activity = latest_step.visited_at
                 
                 # Resolve lesson title
                 if latest_step.lesson_id in lesson_titles:
                     current_lesson_title = lesson_titles[latest_step.lesson_id]
        
        # Calculate progress in current lesson
        current_lesson_progress = 0
        if last_activity and latest_step:
            c_lesson_id = latest_step.lesson_id
            l_total_steps = lesson_step_counts.get(c_lesson_id, 0)
            if l_total_steps > 0:
                l_completed = len([sp for sp in student_steps 
                                 if sp.lesson_id == c_lesson_id and sp.status == "completed"])
                current_lesson_progress = (l_completed / l_total_steps) * 100
                current_lesson_steps_completed = l_completed
                current_lesson_steps_total = l_total_steps
            else:
                current_lesson_steps_completed = 0
                current_lesson_steps_total = 0
        else:
            current_lesson_steps_completed = 0
            current_lesson_steps_total = 0
        
        # Get assignment performance for THIS STUDENT
        # Use group-based assignments (teacher-assigned homework)
        # Get assignment performance for THIS STUDENT
        # Use bulk fetched data
        current_s_group_ids = student_group_ids_map.get(student.id, [])
        student_group_assignments = []
        for gid in current_s_group_ids:
            if gid in group_assignments_map:
                student_group_assignments.extend(group_assignments_map[gid])
        
        # Deduplicate assignments by ID just in case
        unique_assignments = {a.id: a for a in student_group_assignments}
        student_group_assignments = list(unique_assignments.values())
        
        student_assignments_total = len(student_group_assignments)
        student_assignments_completed = 0
        total_score = 0
        max_possible_score = 0
        
        last_test_res = None
        last_submission_date = None
        
        for assignment in student_group_assignments:
            submission = latest_submission_map.get((student.id, assignment.id))
            
            if submission:
                if submission.is_graded:
                     student_assignments_completed += 1
                     if submission.score is not None:
                        total_score += submission.score
                        max_possible_score += assignment.max_score or 0
                
                # Track last submission for "Last Test" column
                # assuming submission.created_at is available or we use id if time missing, but created_at is better
                # Check model: usually created_at or submitted_at
                s_date = submission.submitted_at or submission.created_at
                if s_date:
                    if last_submission_date is None or s_date > last_submission_date:
                        last_submission_date = s_date
                        pct = (submission.score / assignment.max_score * 100) if (submission.score is not None and assignment.max_score) else 0
                        last_test_res = {
                            "title": assignment.title,
                            "score": submission.score,
                            "max_score": assignment.max_score,
                            "percentage": round(pct, 1),
                            "type": "assignment"
                        }

        # Check for Quiz Attempts (Step Quizzes)
        if student.id in student_quiz_attempts:
            for attempt in student_quiz_attempts[student.id]:
                a_date = attempt.completed_at or attempt.created_at
                if a_date:
                    # If this is newer than the last assignment submission
                    if last_submission_date is None or a_date > last_submission_date:
                        last_submission_date = a_date
                        
                        # Determine if SAT Math or Verbal
                        # Determine if SAT Math or Verbal
                        title = attempt.quiz_title or "Quiz"
                        
                        # Fallback or Augment with Lesson Title to catch "[Verbal]" or "[Math]" 
                        # if the quiz title is generic (e.g. "Quiz")
                        lesson_title = lesson_titles.get(attempt.lesson_id, "")
                        full_title_check = title + " " + lesson_title
                        
                        is_verbal = "[Verbal]" in full_title_check
                        is_math = "[Math]" in full_title_check
                        
                        test_type = "quiz"
                        result_math_pct = 0
                        result_verbal_pct = 0
                        
                        if is_verbal:
                            test_type = "sat_verbal"
                            result_verbal_pct = round(attempt.score_percentage, 1)
                        elif is_math:
                            test_type = "sat_math"
                            result_math_pct = round(attempt.score_percentage, 1)
                        
                        # Improve display title if generic
                        display_title = title
                        if title == "Quiz" and lesson_title:
                             display_title = lesson_title

                        last_test_res = {
                            "title": display_title,
                            "score": attempt.correct_answers,
                            "max_score": attempt.total_questions,
                            "percentage": round(attempt.score_percentage, 1),
                            "type": test_type,
                            "math_percent": result_math_pct,
                            "verbal_percent": result_verbal_pct
                        }
        
        # OVERRIDE with External SAT Data if available
        # User explicitly stated "Latest Test" is from external system
        if student.id in student_sat_map:
             sat_res = student_sat_map[student.id]
             # We could compare dates, but user said "Last Test IS NOT connected to lessons", 
             # likely implying this column is reserved for SAT results.
             # However, to be safe, let's prefer SAT result if it exists.
             last_test_res = sat_res

        student_performance.append({
            "student_id": student.id,
            "student_name": student.name,
            "email": student.email,
            "group_ids": current_s_group_ids,
            "group_name": student.group_name if hasattr(student, 'group_name') else student_groups_map.get(student.id, "No Group"),
            "completed_steps": student_completed,
            "total_steps_available": total_steps,
            "completion_percentage": (student_completed / total_steps * 100) if total_steps > 0 else 0,
            "time_spent_minutes": student_time,
            "completed_assignments": student_assignments_completed,
            "total_assignments": student_assignments_total,
            "assignment_score_percentage": (total_score / max_possible_score * 100) if max_possible_score > 0 else 0,
            "last_activity": last_activity,
            "current_lesson": current_lesson_title,
            "current_lesson_progress": current_lesson_progress,
            "current_lesson_steps_completed": current_lesson_steps_completed,
            "current_lesson_steps_total": current_lesson_steps_total,
            "last_test_result": last_test_res
        })
    
    return {
        "course_info": {
            "id": course.id,
            "title": course.title,
            "teacher_name": course.teacher.name if course.teacher else "Unknown"
        },
        "structure": {
            "total_modules": len(modules),
            "total_lessons": total_lessons,
            "total_steps": total_steps
        },
        "engagement": {
            "total_enrolled_students": len(enrolled_students),
            "total_time_spent_minutes": total_time_spent,
            "total_completed_steps": completed_steps,
            "average_completion_rate": (completed_steps / (total_steps * len(enrolled_students)) * 100) if total_steps > 0 and enrolled_students else 0
        },
        "student_performance": student_performance
    }

@router.get("/video-engagement/{course_id}")
async def get_video_engagement_analytics(
    course_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get video engagement analytics for a course"""
    
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Properly validate access including curator permissions
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")
    
    # Get video steps in the course
    video_steps = db.query(Step).join(Lesson).join(Module).filter(
        Module.course_id == course_id,
        Step.content_type == "video_text"
    ).all()
    
    video_analytics = []
    
    for step in video_steps:
        # Get progress for this video step
        step_progress = db.query(StepProgress).filter(
            StepProgress.step_id == step.id
        ).all()
        
        total_views = len(step_progress)
        completed_views = len([sp for sp in step_progress if sp.status == "completed"])
        total_time_spent = sum(sp.time_spent_minutes for sp in step_progress)
        
        video_analytics.append({
            "step_id": step.id,
            "step_title": step.title,
            "lesson_title": step.lesson.title if step.lesson else "Unknown",
            "video_url": step.video_url,
            "total_views": total_views,
            "completed_views": completed_views,
            "completion_rate": (completed_views / total_views * 100) if total_views > 0 else 0,
            "average_watch_time_minutes": (total_time_spent / total_views) if total_views > 0 else 0,
            "total_watch_time_minutes": total_time_spent
        })
    
    return {
        "course_id": course_id,
        "video_analytics": video_analytics,
        "summary": {
            "total_videos": len(video_steps),
            "total_video_views": sum(va["total_views"] for va in video_analytics),
            "average_completion_rate": sum(va["completion_rate"] for va in video_analytics) / len(video_analytics) if video_analytics else 0
        }
    }

@router.get("/quiz-performance/{course_id}")
async def get_quiz_performance_analytics(
    course_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get quiz performance analytics for a course"""
    
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Properly validate access including curator permissions
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")
    
    # Get quiz steps and assignments in the course
    quiz_steps = db.query(Step).join(Lesson).join(Module).filter(
        Module.course_id == course_id,
        Step.content_type == "quiz"
    ).all()
    
    quiz_assignments = db.query(Assignment).join(Lesson).join(Module).filter(
        Module.course_id == course_id,
        Assignment.assignment_type.in_(["single_choice", "multiple_choice", "fill_blank"])
    ).all()
    
    quiz_analytics = []
    
    # Analyze quiz steps
    for step in quiz_steps:
        step_progress = db.query(StepProgress).filter(
            StepProgress.step_id == step.id
        ).all()
        
        total_attempts = len(step_progress)
        completed_attempts = len([sp for sp in step_progress if sp.status == "completed"])
        
        quiz_analytics.append({
            "type": "quiz_step",
            "id": step.id,
            "title": step.title,
            "lesson_title": step.lesson.title if step.lesson else "Unknown",
            "total_attempts": total_attempts,
            "completed_attempts": completed_attempts,
            "completion_rate": (completed_attempts / total_attempts * 100) if total_attempts > 0 else 0,
            "average_time_spent": sum(sp.time_spent_minutes for sp in step_progress) / total_attempts if total_attempts > 0 else 0
        })
    
    # Analyze quiz assignments
    for assignment in quiz_assignments:
        submissions = db.query(AssignmentSubmission).filter(
            AssignmentSubmission.assignment_id == assignment.id
        ).all()
        
        total_submissions = len(submissions)
        graded_submissions = [s for s in submissions if s.is_graded and s.score is not None]
        
        if graded_submissions:
            scores = [s.score for s in graded_submissions]
            max_scores = [s.max_score for s in graded_submissions]
            average_score = sum(scores) / len(scores)
            average_percentage = sum(s.score / s.max_score * 100 for s in graded_submissions) / len(graded_submissions)
        else:
            average_score = 0
            average_percentage = 0
        
        quiz_analytics.append({
            "type": "quiz_assignment",
            "id": assignment.id,
            "title": assignment.title,
            "assignment_type": assignment.assignment_type,
            "max_score": assignment.max_score,
            "total_submissions": total_submissions,
            "graded_submissions": len(graded_submissions),
            "average_score": average_score,
            "average_percentage": average_percentage,
            "submission_rate": (total_submissions / db.query(Enrollment).filter(Enrollment.course_id == course_id).count() * 100) if db.query(Enrollment).filter(Enrollment.course_id == course_id).count() > 0 else 0
        })
    
    return {
        "course_id": course_id,
        "quiz_analytics": quiz_analytics,
        "summary": {
            "total_quizzes": len(quiz_steps) + len(quiz_assignments),
            "total_quiz_steps": len(quiz_steps),
            "total_quiz_assignments": len(quiz_assignments)
        }
    }

def extract_correct_answers_from_gaps(text: str, separator: str = ',') -> List[str]:
    if not text:
        return []
    
    # Matches [[...]]
    matches = re.findall(r'\[\[(.*?)\]\]', text)
    if not matches:
        return []
    
    results = []
    for match in matches:
        options = [o.strip() for o in match.split(separator.strip())]
        
        # If an option ends with *, it's the correct answer
        starred = next((o for o in options if o.endswith('*')), None)
        if starred:
            results.append(starred[:-1])
        else:
            # Otherwise the first option is correct
            results.append(options[0])
            
    return results

@router.get("/course/{course_id}/quiz-errors")
async def get_quiz_question_errors(
    course_id: int,
    group_id: Optional[int] = None,
    lesson_id: Optional[int] = None,
    limit: int = Query(500, description="Max number of questions to return"),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Get questions with the most errors for a course.
    Helps teachers identify difficult questions students struggle with.
    """
    
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")
    
    # Build query for quiz attempts
    query = db.query(QuizAttempt).filter(
        QuizAttempt.course_id == course_id,
        QuizAttempt.is_draft == False,
        QuizAttempt.answers.isnot(None)
    )
    
    if lesson_id:
        query = query.filter(QuizAttempt.lesson_id == lesson_id)

    # Role-based filtering (Teacher/Curator should only see their own groups if no specific group is selected)
    if not group_id and current_user.role not in ["admin", "head_curator"]:
        if current_user.role == "teacher":
            # Teacher's groups or courses they teach
            teacher_groups = db.query(Group.id).filter(Group.teacher_id == current_user.id).subquery()
            teacher_courses = db.query(Course.id).filter(Course.teacher_id == current_user.id).subquery()
            
            group_student_ids = db.query(GroupStudent.student_id).filter(GroupStudent.group_id.in_(teacher_groups)).subquery()
            course_student_ids = db.query(Enrollment.user_id).filter(Enrollment.course_id.in_(teacher_courses)).subquery()
            
            query = query.filter(
                or_(
                    QuizAttempt.user_id.in_(group_student_ids),
                    QuizAttempt.user_id.in_(course_student_ids)
                )
            )
        elif current_user.role == "curator":
            # Curator's groups
            curator_groups = db.query(Group.id).filter(Group.curator_id == current_user.id).subquery()
            group_student_ids = db.query(GroupStudent.student_id).filter(GroupStudent.group_id.in_(curator_groups)).subquery()
            query = query.filter(QuizAttempt.user_id.in_(group_student_ids))
            
    # Explicit group filter
    elif group_id:
        group_student_ids = db.query(GroupStudent.student_id).filter(
            GroupStudent.group_id == group_id
        ).subquery()
        query = query.filter(QuizAttempt.user_id.in_(group_student_ids))
    
    # Aggregate by question_id
    # key: (step_id, question_id)
    from collections import defaultdict
    error_stats = defaultdict(lambda: {"total": 0, "wrong": 0, "step_id": None, "lesson_id": None, "question_text": "", "lesson_title": ""})

    # --- 1. Internal Quiz Attempts Processing ---
    attempts = query.all()
    
    if not attempts:
        return {
            "course_id": course_id,
            "total_attempts_analyzed": 0,
            "questions": []
        }

    # Pre-fetch all referenced steps to verify correct answers
    step_ids = list(set(a.step_id for a in attempts))
    steps = db.query(Step).filter(Step.id.in_(step_ids)).all()
    
    # helper to check if answer is correct
    def get_answer_error_score(step_id, step_content, question_id, user_answer):
        """Returns error score from 0.0 (perfect) to 1.0 (all wrong). None if unverifiable."""
        try:
            if not step_content: return None
            questions = step_content.get("questions", [])
            for q in questions:
                if str(q.get("id")) == str(question_id):
                    q_type = q.get("question_type")
                    if q_type == "long_text":
                        return None # Non-gradable
                    
                    # Try different possible keys for correct answer
                    actual_correct = q.get("correct_answer")
                    if actual_correct is None:
                        actual_correct = q.get("correctAnswer")
                    
                    def norm_val(v):
                        if v is None: return ""
                        s = str(v).strip().lower()
                        # Replace comma with dot for international numbers
                        s = s.replace(",", ".")
                        # Remove all whitespace to handle "1 / 16"
                        s = "".join(s.split())
                        if s.startswith("[") and s.endswith("]") and "," not in s:
                            s = s[1:-1].strip()
                        return s

                    def to_float(s):
                        try:
                            if "/" in s:
                                parts = s.split("/")
                                if len(parts) == 2:
                                    return float(parts[0]) / float(parts[1])
                            return float(s)
                        except:
                            return None

                    def check_match(u, a):
                        u_norm = norm_val(u)
                        a_str = str(a).strip().lower()
                        # Support multiple options separated by |
                        options = [norm_val(o) for o in a_str.split("|")]
                        
                        # Direct string match
                        if u_norm in options:
                            return True
                            
                        # Try numeric evaluation (e.g. 0.0625 == 1/16)
                        u_float = to_float(u_norm)
                        if u_float is not None:
                            for opt in options:
                                o_float = to_float(opt)
                                if o_float is not None and abs(u_float - o_float) < 0.0001:
                                    return True
                        return False

                    # CHECK IF VERIFIABLE
                    if actual_correct is None: return None
                    if isinstance(actual_correct, str) and not actual_correct.strip(): return None
                    if isinstance(actual_correct, list) and len(actual_correct) == 0: return None
                    if isinstance(actual_correct, list) and all(not norm_val(x) for x in actual_correct): return None

                    # Handle lists (multiple gaps or multiple choice)
                    if isinstance(actual_correct, list):
                        u_list = user_answer if isinstance(user_answer, list) else [user_answer]
                        
                        # Partial credit: count how many are WRONG
                        base_size = max(len(actual_correct), 1)
                        mismatches = 0
                        
                        # For fill_blank, order matters strictly
                        for i in range(len(actual_correct)):
                            u_v = u_list[i] if i < len(u_list) else ""
                            if not check_match(u_v, actual_correct[i]):
                                mismatches += 1
                        
                        if len(u_list) > len(actual_correct):
                            mismatches += (len(u_list) - len(actual_correct))
                            
                        return min(mismatches / base_size, 1.0)
                    
                    # Single value comparison
                    u_val = user_answer
                    if isinstance(user_answer, list) and len(user_answer) > 0:
                        u_val = user_answer[0]
                    
                    is_correct = check_match(u_val, actual_correct)
                    return 0.0 if is_correct else 1.0
                    
            return None # Question not matching this ID
        except:
            return None

    step_data_cache = {}
    for s in steps:
        try:
            step_data_cache[s.id] = json.loads(s.content_text) if s.content_text else {}
        except:
            step_data_cache[s.id] = {}

    for attempt in attempts:
        try:
            answers_data = attempt.answers
            if isinstance(answers_data, str):
                answers_data = json.loads(answers_data)
            
            sid = attempt.step_id
            s_content = step_data_cache.get(sid, {})
            
            def process_q_error(qid, val):
                err = get_answer_error_score(sid, s_content, qid, val)
                if err is not None:
                    key = (sid, str(qid))
                    error_stats[key]["step_id"] = sid
                    error_stats[key]["lesson_id"] = attempt.lesson_id
                    error_stats[key]["total"] += 1
                    error_stats[key]["wrong"] += err

            if isinstance(answers_data, list):
                for ans in answers_data:
                    if isinstance(ans, list) and len(ans) >= 2:
                        process_q_error(ans[0], ans[1])
            elif isinstance(answers_data, dict):
                for q_id, val in answers_data.items():
                    process_q_error(q_id, val)
        except:
            continue

    # Calculate error rates and sort
    error_list = []
    for (step_id, q_id), stats in error_stats.items():
        if stats["total"] > 0:
            error_rate = (stats["wrong"] / stats["total"]) * 100
            
            # Filter negligible errors unless specifically viewing a lesson
            if not lesson_id and error_rate < 5.0:
                continue
                
            error_list.append({
                "step_id": step_id,
                "lesson_id": stats.get("lesson_id"),
                "question_id": q_id,
                "total_attempts": stats["total"],
                "wrong_answers": stats["wrong"],
                "error_rate": round(error_rate, 1),
                "question_text": "Question " + str(q_id),
                "question_type": "unknown",
                "lesson_title": "Internal",
                "step_title": "Quiz"
            })
            
    error_list.sort(key=lambda x: (-x["error_rate"], -x["total_attempts"]))
    error_list = error_list[:limit]
    
    # Enrichment
    step_ids = list(set(item["step_id"] for item in error_list))
    steps = db.query(Step).filter(Step.id.in_(step_ids)).all()
    step_map = {s.id: s for s in steps}
    
    lesson_ids = list(set(s.lesson_id for s in steps))
    lessons = db.query(Lesson).filter(Lesson.id.in_(lesson_ids)).all()
    lesson_map = {l.id: l.title for l in lessons}

    for item in error_list:
        step = step_map.get(item["step_id"])
        if step:
            item["step_title"] = step.title
            l_title = lesson_map.get(step.lesson_id, "Unknown Lesson")
            item["lesson_title"] = l_title
            
            # Since we filtered out quiz_total above, all items are individual questions
            try:
                content = json.loads(step.content_text) if step.content_text else {}
                questions = content.get("questions", [])
                
                # Find the actual question text in the step content
                found = False
                for q in questions:
                    if str(q.get("id")) == str(item["question_id"]):
                        # Try multiple possible fields for text
                        q_text = q.get("question_text") or q.get("text") or q.get("content")
                        if q_text:
                            item["question_text"] = q_text
                            item["question_type"] = q.get("question_type") or "unknown"
                            found = True
                        break
                
                if not found and item["question_text"].startswith("Question "):
                    # Fallback to make non-titled questions more descriptive
                    item["question_text"] = f"{item['question_text']} (in {step.title})"
            except:
                pass

    return {
        "course_id": course_id,
        "group_id": group_id,
        "total_attempts_analyzed": len(attempts),
        "questions": error_list
    }


@router.get("/students/all")
async def get_all_students_analytics(
    course_id: Optional[int] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить аналитику по всем доступным студентам
    
    Args:
        course_id: Опционально - ID курса для фильтрации последнего урока
    """
    
    # Проверка прав доступа
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Базовый запрос студентов
    students_query = db.query(UserInDB).filter(UserInDB.role == "student", UserInDB.is_active == True)
    
    # Фильтрация по ролям
    if current_user.role == "teacher":
        # Учитель видит студентов из своих групп и записанных на свои курсы
        teacher_groups = db.query(Group.id).filter(Group.teacher_id == current_user.id).subquery()
        teacher_courses = db.query(Course.id).filter(Course.teacher_id == current_user.id).subquery()
        
        group_students = db.query(GroupStudent.student_id).filter(GroupStudent.group_id.in_(teacher_groups)).subquery()
        course_students = db.query(Enrollment.user_id).filter(Enrollment.course_id.in_(teacher_courses)).subquery()
        
        students_query = students_query.filter(
            or_(
                UserInDB.id.in_(group_students),
                UserInDB.id.in_(course_students)
            )
        )
    
    elif current_user.role == "curator":
        # Куратор видит студентов из своих групп
        curator_groups = db.query(Group.id).filter(Group.curator_id == current_user.id).subquery()
        group_students = db.query(GroupStudent.student_id).filter(GroupStudent.group_id.in_(curator_groups)).subquery()
        
        students_query = students_query.filter(UserInDB.id.in_(group_students))
    
    # Админ видит всех студентов (без дополнительной фильтрации)
    
    students = students_query.all()
    
    students_analytics = []
    for student in students:
        # Получаем группы студента
        student_groups = db.query(Group).join(GroupStudent).filter(
            GroupStudent.student_id == student.id
        ).all()
        
        # Получаем ВСЕ курсы где есть прогресс студента для подсчета общего прогресса
        all_courses_query = db.query(Course).join(
            Module, Module.course_id == Course.id
        ).join(
            Lesson, Lesson.module_id == Module.id
        ).join(
            Step, Step.lesson_id == Lesson.id
        ).join(
            StepProgress, StepProgress.step_id == Step.id
        ).filter(
            StepProgress.user_id == student.id
        )
        
        all_courses_with_progress = all_courses_query.distinct().all()
        
        # Если нет прогресса, пробуем через Enrollment для общего прогресса
        if not all_courses_with_progress:
            enrollment_query = db.query(Course).join(Enrollment).filter(
                Enrollment.user_id == student.id,
                Course.is_active == True
            )
            all_courses_with_progress = enrollment_query.all()
        
        active_courses = all_courses_with_progress
        
        # Получаем курсы для фильтрации last_lesson (если указан course_id)
        last_lesson_courses = active_courses
        if course_id:
            last_lesson_courses = [c for c in active_courses if c.id == course_id]
        
        # Подсчитываем общий прогресс
        total_steps = 0
        completed_steps = 0
        total_assignments = 0
        completed_assignments = 0
        total_assignment_score = 0
        total_max_score = 0
        
        for course in active_courses:
            # Подсчет шагов
            course_steps = db.query(Step).join(Lesson).join(Module).filter(
                Module.course_id == course.id
            ).count()
            total_steps += course_steps
            
            # Правильный подсчет завершенных шагов через JOIN (как в детальном прогрессе)
            course_completed_steps = db.query(StepProgress).join(
                Step, StepProgress.step_id == Step.id
            ).join(
                Lesson, Step.lesson_id == Lesson.id
            ).join(
                Module, Lesson.module_id == Module.id
            ).filter(
                StepProgress.user_id == student.id,
                Module.course_id == course.id,
                StepProgress.status == "completed"
            ).count()
            completed_steps += course_completed_steps
            
            # Подсчет заданий
            course_assignments = db.query(Assignment).join(Lesson).join(Module).filter(
                Module.course_id == course.id
            ).all()
            total_assignments += len(course_assignments)
            
            for assignment in course_assignments:
                submission = db.query(AssignmentSubmission).filter(
                    AssignmentSubmission.assignment_id == assignment.id,
                    AssignmentSubmission.user_id == student.id
                ).first()
                
                if submission and submission.is_graded:
                    completed_assignments += 1
                    total_assignment_score += submission.score or 0
                    total_max_score += assignment.max_score or 0
        
        # Вычисляем проценты
        completion_percentage = (completed_steps / total_steps * 100) if total_steps > 0 else 0
        assignment_score_percentage = (total_assignment_score / total_max_score * 100) if total_max_score > 0 else 0
        
        # Получаем информацию о последнем уроке (фильтруем по курсу если указан)
        last_lesson_info = None
        
        # Build query with optional course filter
        step_progress_query = db.query(StepProgress).join(
            Step, StepProgress.step_id == Step.id
        ).join(
            Lesson, Step.lesson_id == Lesson.id
        ).join(
            Module, Lesson.module_id == Module.id
        ).filter(
            StepProgress.user_id == student.id
        )
        
        # Filter by course if provided
        if course_id:
            step_progress_query = step_progress_query.filter(Module.course_id == course_id)
            
        last_step_progress = step_progress_query.order_by(StepProgress.visited_at.desc()).first()
        
        if last_step_progress:
            # Получаем информацию об уроке
            lesson = db.query(Lesson).join(Step).filter(
                Step.id == last_step_progress.step_id
            ).first()
            
            if lesson:
                # Считаем прогресс урока
                total_lesson_steps = db.query(Step).filter(
                    Step.lesson_id == lesson.id
                ).count()
                
                completed_lesson_steps = db.query(StepProgress).join(Step).filter(
                    StepProgress.user_id == student.id,
                    Step.lesson_id == lesson.id,
                    StepProgress.status == "completed"
                ).count()
                
                lesson_progress_percentage = (completed_lesson_steps / total_lesson_steps * 100) if total_lesson_steps > 0 else 0
                
                last_lesson_info = {
                    "lesson_title": lesson.title,
                    "lesson_progress_percentage": round(lesson_progress_percentage, 1),
                    "completed_steps": completed_lesson_steps,
                    "total_steps": total_lesson_steps
                }
        
        students_analytics.append({
            "student_id": student.id,
            "student_name": student.name,
            "student_email": student.email,
            "student_number": student.student_id,
            "groups": [{"id": g.id, "name": g.name} for g in student_groups],
            "active_courses_count": len(active_courses),
            "total_steps": total_steps,
            "completed_steps": completed_steps,
            "completion_percentage": round(completion_percentage, 1),
            "total_assignments": total_assignments,
            "completed_assignments": completed_assignments,
            "assignment_score_percentage": round(assignment_score_percentage, 1),
            "total_study_time_minutes": student.total_study_time_minutes,
            "daily_streak": student.daily_streak,
            "last_activity_date": student.last_activity_date,
            "last_lesson": last_lesson_info
        })
    
    return {
        "students": students_analytics,
        "total_students": len(students_analytics)
    }

@router.get("/groups")
async def get_groups_analytics(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить аналитику по всем доступным группам"""
    
    # Проверка прав доступа
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Базовый запрос групп
    groups_query = db.query(Group).filter(Group.is_active == True)
    
    # Фильтрация по ролям
    if current_user.role == "teacher":
        groups_query = groups_query.filter(Group.teacher_id == current_user.id)
    elif current_user.role == "curator":
        groups_query = groups_query.filter(Group.curator_id == current_user.id)
    elif current_user.role == "head_curator":
        groups_query = groups_query.filter(Group.is_special == False)
    
    # Админ видит все группы (без дополнительной фильтрации)
    
    groups = groups_query.all()
    
    groups_analytics = []
    for group in groups:
        # Получаем студентов группы
        students = db.query(UserInDB).join(GroupStudent).filter(
            GroupStudent.group_id == group.id,
            UserInDB.is_active == True
        ).all()
        
        # Подсчитываем средний прогресс группы
        total_completion = 0
        total_assignment_score = 0
        total_study_time = 0
        students_with_progress = 0
        
        for student in students:
            # Получаем курсы где есть прогресс студента (через StepProgress)
            courses_with_progress = db.query(Course).join(
                Module, Module.course_id == Course.id
            ).join(
                Lesson, Lesson.module_id == Module.id
            ).join(
                Step, Step.lesson_id == Lesson.id
            ).join(
                StepProgress, StepProgress.step_id == Step.id
            ).filter(
                StepProgress.user_id == student.id
            ).distinct().all()
            
            # Фолбэк на Enrollment если нет прогресса
            if not courses_with_progress:
                courses_with_progress = db.query(Course).join(Enrollment).filter(
                    Enrollment.user_id == student.id,
                    Enrollment.is_active == True,
                    Course.is_active == True
                ).all()
            
            active_courses = courses_with_progress
            
            if active_courses:
                student_total_steps = 0
                student_completed_steps = 0
                student_total_score = 0
                student_max_score = 0
                
                for course in active_courses:
                    # Подсчет шагов
                    course_steps = db.query(Step).join(Lesson).join(Module).filter(
                        Module.course_id == course.id
                    ).count()
                    student_total_steps += course_steps
                    
                    # Правильный подсчет завершенных шагов через JOIN
                    course_completed_steps = db.query(StepProgress).join(
                        Step, StepProgress.step_id == Step.id
                    ).join(
                        Lesson, Step.lesson_id == Lesson.id
                    ).join(
                        Module, Lesson.module_id == Module.id
                    ).filter(
                        StepProgress.user_id == student.id,
                        Module.course_id == course.id,
                        StepProgress.status == "completed"
                    ).count()
                    student_completed_steps += course_completed_steps
                    
                    # Подсчет заданий
                    assignments = db.query(Assignment).join(Lesson).join(Module).filter(
                        Module.course_id == course.id
                    ).all()
                    
                    for assignment in assignments:
                        submission = db.query(AssignmentSubmission).filter(
                            AssignmentSubmission.assignment_id == assignment.id,
                            AssignmentSubmission.user_id == student.id
                        ).first()
                        
                        if submission and submission.is_graded:
                            student_total_score += submission.score or 0
                            student_max_score += assignment.max_score or 0
                
                if student_total_steps > 0:
                    student_completion = student_completed_steps / student_total_steps * 100
                    total_completion += student_completion
                    students_with_progress += 1
                
                if student_max_score > 0:
                    total_assignment_score += student_total_score / student_max_score * 100
                
                total_study_time += student.total_study_time_minutes
        
        # Вычисляем средние значения
        avg_completion = (total_completion / students_with_progress) if students_with_progress > 0 else 0
        avg_assignment_score = (total_assignment_score / len(students)) if students else 0
        avg_study_time = (total_study_time / len(students)) if students else 0
        
        groups_analytics.append({
            "group_id": group.id,
            "group_name": group.name,
            "description": group.description,
            "teacher_name": group.teacher.name if group.teacher else None,
            "curator_name": group.curator.name if group.curator else None,
            "students_count": len(students),
            "average_completion_percentage": round(avg_completion, 1),
            "average_assignment_score_percentage": round(avg_assignment_score, 1),
            "average_study_time_minutes": round(avg_study_time, 0),
            "created_at": group.created_at
        })
    
    return {
        "groups": groups_analytics,
        "total_groups": len(groups_analytics)
    }

@router.get("/course/{course_id}/groups")
async def get_course_groups_analytics(
    course_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get analytics for groups in a specific course"""
    
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Check course access
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")
    
    # Get all active groups for this teacher/curator/admin
    # We want to see ALL groups even if they haven't started this specific course yet
    # Get active groups for this teacher/curator/admin that have explicit access to this course
    base_query = db.query(Group).join(
        CourseGroupAccess, Group.id == CourseGroupAccess.group_id
    ).filter(
        Group.is_active == True,
        CourseGroupAccess.course_id == course_id,
        CourseGroupAccess.is_active == True
    )
    
    if current_user.role == "teacher":
        base_query = base_query.filter(Group.teacher_id == current_user.id)
    elif current_user.role == "curator":
        base_query = base_query.filter(Group.curator_id == current_user.id)
    elif current_user.role == "head_curator":
        base_query = base_query.filter(Group.is_special == False)
    
    groups_with_students = base_query.distinct().all()
    if not groups_with_students:
        return {
            "course_id": course_id,
            "groups": [],
            "total_groups": 0
        }

    group_ids = [g.id for g in groups_with_students]

    # Get course structure for calculations
    total_steps_in_course = db.query(Step).join(Lesson).join(Module).filter(
        Module.course_id == course_id
    ).count()

    # Load assignments for the course once
    assignments = db.query(Assignment.id, Assignment.max_score).join(Lesson).join(Module).filter(
        Module.course_id == course_id
    ).all()
    assignment_ids = [a.id for a in assignments]
    assignment_max_score_map = {a.id: (a.max_score or 0) for a in assignments}

    # Load group -> student links once
    group_student_rows = db.query(GroupStudent.group_id, GroupStudent.student_id).filter(
        GroupStudent.group_id.in_(group_ids)
    ).all()

    group_student_ids_map = defaultdict(set)
    for gid, sid in group_student_rows:
        group_student_ids_map[gid].add(sid)

    all_student_ids = sorted({sid for _, sid in group_student_rows})
    if not all_student_ids:
        all_student_ids = []

    # Keep only active students
    students = db.query(UserInDB.id, UserInDB.total_study_time_minutes).filter(
        UserInDB.id.in_(all_student_ids) if all_student_ids else False,
        UserInDB.is_active == True,
        UserInDB.role == "student"
    ).all()
    student_time_map = {s.id: (s.total_study_time_minutes or 0) for s in students}
    active_student_ids = set(student_time_map.keys())

    for gid in list(group_student_ids_map.keys()):
        group_student_ids_map[gid] = group_student_ids_map[gid].intersection(active_student_ids)

    # Load completed steps per student in this course once
    completed_steps_rows = []
    if active_student_ids:
        completed_steps_rows = db.query(
            StepProgress.user_id,
            func.count(StepProgress.id).label("completed_steps")
        ).join(
            Step, StepProgress.step_id == Step.id
        ).join(
            Lesson, Step.lesson_id == Lesson.id
        ).join(
            Module, Lesson.module_id == Module.id
        ).filter(
            StepProgress.user_id.in_(list(active_student_ids)),
            Module.course_id == course_id,
            StepProgress.status == "completed"
        ).group_by(
            StepProgress.user_id
        ).all()
    completed_steps_map = {row.user_id: row.completed_steps for row in completed_steps_rows}

    # Load graded submissions once and keep latest per (student, assignment)
    latest_submission_map = {}
    if assignment_ids and active_student_ids:
        graded_submissions = db.query(AssignmentSubmission).filter(
            AssignmentSubmission.assignment_id.in_(assignment_ids),
            AssignmentSubmission.user_id.in_(list(active_student_ids)),
            AssignmentSubmission.is_graded == True
        ).all()

        for submission in graded_submissions:
            key = (submission.user_id, submission.assignment_id)
            current_date = submission.submitted_at or submission.created_at
            existing_submission = latest_submission_map.get(key)

            if not existing_submission:
                latest_submission_map[key] = submission
                continue

            existing_date = existing_submission.submitted_at or existing_submission.created_at
            if current_date and (not existing_date or current_date > existing_date):
                latest_submission_map[key] = submission

    student_assignment_pct_map = {}
    if assignment_ids and active_student_ids:
        for student_id in active_student_ids:
            student_score = 0
            student_max = 0
            for assignment_id in assignment_ids:
                submission = latest_submission_map.get((student_id, assignment_id))
                if not submission:
                    continue
                student_score += submission.score or 0
                student_max += assignment_max_score_map.get(assignment_id, 0)

            if student_max > 0:
                student_assignment_pct_map[student_id] = (student_score / student_max) * 100

    groups_analytics = []
    for group in groups_with_students:
        student_ids = list(group_student_ids_map.get(group.id, set()))
        student_count = len(student_ids)

        if student_count == 0:
            groups_analytics.append({
                "group_id": group.id,
                "group_name": group.name,
                "description": group.description,
                "teacher_name": group.teacher.name if group.teacher else None,
                "curator_name": group.curator.name if group.curator else None,
                "students_count": 0,
                "students_with_progress": 0,
                "average_completion_percentage": 0,
                "average_assignment_score_percentage": 0,
                "average_study_time_minutes": 0,
                "created_at": group.created_at
            })
            continue

        total_completion = 0
        total_assignment_score = 0
        total_study_time = 0
        students_with_progress = 0

        for student_id in student_ids:
            completed_steps = completed_steps_map.get(student_id, 0)
            if completed_steps > 0:
                students_with_progress += 1

            if total_steps_in_course > 0:
                total_completion += (completed_steps / total_steps_in_course) * 100

            total_assignment_score += student_assignment_pct_map.get(student_id, 0)
            total_study_time += student_time_map.get(student_id, 0)

        avg_completion = total_completion / student_count
        avg_assignment_score = total_assignment_score / student_count
        avg_study_time = total_study_time / student_count

        groups_analytics.append({
            "group_id": group.id,
            "group_name": group.name,
            "description": group.description,
            "teacher_name": group.teacher.name if group.teacher else None,
            "curator_name": group.curator.name if group.curator else None,
            "students_count": student_count,
            "students_with_progress": students_with_progress,
            "average_completion_percentage": round(avg_completion, 1),
            "average_assignment_score_percentage": round(avg_assignment_score, 1),
            "average_study_time_minutes": round(avg_study_time, 0),
            "created_at": group.created_at
        })
    
    return {
        "course_id": course_id,
        "groups": groups_analytics,
        "total_groups": len(groups_analytics)
    }

@router.get("/group/{group_id}/students")
async def get_group_students_analytics(
    group_id: int,
    course_id: Optional[int] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить аналитику по студентам конкретной группы
    
    Args:
        group_id: ID группы
        course_id: Опционально - ID курса для фильтрации прогресса и последнего урока
    """
    
    # Проверка прав доступа
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Проверяем доступ к группе
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    
    # Проверка прав доступа к группе
    if current_user.role == "teacher" and group.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied to this group")
    elif current_user.role == "curator" and group.curator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied to this group")
    
    # Получаем студентов группы
    students = db.query(UserInDB).join(GroupStudent).filter(
        GroupStudent.group_id == group_id,
        UserInDB.is_active == True
    ).all()
    
    students_analytics = []
    for student in students:
        # Получаем курсы где есть прогресс студента (через StepProgress - source of truth)
        courses_query = db.query(Course).join(
            Module, Module.course_id == Course.id
        ).join(
            Lesson, Lesson.module_id == Module.id
        ).join(
            Step, Step.lesson_id == Lesson.id
        ).join(
            StepProgress, StepProgress.step_id == Step.id
        ).filter(
            StepProgress.user_id == student.id
        )
        
        if course_id:
            courses_query = courses_query.filter(Course.id == course_id)
            
        courses_with_progress = courses_query.distinct().all()
        
        # Фолбэк на Enrollment если нет прогресса
        if not courses_with_progress:
            enrollment_query = db.query(Course).join(Enrollment).filter(
                Enrollment.user_id == student.id,
                Enrollment.is_active == True,
                Course.is_active == True
            )
            if course_id:
                enrollment_query = enrollment_query.filter(Course.id == course_id)
            courses_with_progress = enrollment_query.all()
        
        active_courses = courses_with_progress
        
        # Получаем группы студента для отображения
        student_groups = db.query(Group).join(GroupStudent).filter(
            GroupStudent.student_id == student.id
        ).all()
        
        # Подсчитываем прогресс
        total_steps = 0
        completed_steps = 0
        total_assignments = 0
        completed_assignments = 0
        total_assignment_score = 0
        total_max_score = 0
        
        for course in active_courses:
            course_steps = db.query(Step).join(Lesson).join(Module).filter(
                Module.course_id == course.id
            ).count()
            total_steps += course_steps
            
            # Правильный подсчет завершенных шагов через JOIN
            course_completed_steps = db.query(StepProgress).join(
                Step, StepProgress.step_id == Step.id
            ).join(
                Lesson, Step.lesson_id == Lesson.id
            ).join(
                Module, Lesson.module_id == Module.id
            ).filter(
                StepProgress.user_id == student.id,
                Module.course_id == course.id,
                StepProgress.status == "completed"
            ).count()
            completed_steps += course_completed_steps
            
            course_assignments = db.query(Assignment).join(Lesson).join(Module).filter(
                Module.course_id == course.id
            ).all()
            total_assignments += len(course_assignments)
            
            for assignment in course_assignments:
                submission = db.query(AssignmentSubmission).filter(
                    AssignmentSubmission.assignment_id == assignment.id,
                    AssignmentSubmission.user_id == student.id
                ).first()
                
                if submission and submission.is_graded:
                    completed_assignments += 1
                    total_assignment_score += submission.score or 0
                    total_max_score += assignment.max_score or 0
        
        completion_percentage = (completed_steps / total_steps * 100) if total_steps > 0 else 0
        assignment_score_percentage = (total_assignment_score / total_max_score * 100) if total_max_score > 0 else 0
        
        # Получаем информацию о последнем уроке (фильтруем по курсу если указан)
        last_lesson_info = None
        
        # Build query with optional course filter
        step_progress_query = db.query(StepProgress).join(
            Step, StepProgress.step_id == Step.id
        ).join(
            Lesson, Step.lesson_id == Lesson.id
        ).join(
            Module, Lesson.module_id == Module.id
        ).filter(
            StepProgress.user_id == student.id
        )
        
        # Filter by course if provided
        if course_id:
            step_progress_query = step_progress_query.filter(Module.course_id == course_id)
        
        last_step_progress = step_progress_query.order_by(StepProgress.visited_at.desc()).first()
        
        if last_step_progress:
            # Получаем информацию об уроке
            lesson = db.query(Lesson).join(Step).filter(
                Step.id == last_step_progress.step_id
            ).first()
            
            if lesson:
                # Считаем прогресс урока
                total_lesson_steps = db.query(Step).filter(
                    Step.lesson_id == lesson.id
                ).count()
                
                completed_lesson_steps = db.query(StepProgress).join(Step).filter(
                    StepProgress.user_id == student.id,
                    Step.lesson_id == lesson.id,
                    StepProgress.status == "completed"
                ).count()
                
                lesson_progress_percentage = (completed_lesson_steps / total_lesson_steps * 100) if total_lesson_steps > 0 else 0
                
                last_lesson_info = {
                    "lesson_title": lesson.title,
                    "lesson_progress_percentage": round(lesson_progress_percentage, 1),
                    "completed_steps": completed_lesson_steps,
                    "total_steps": total_lesson_steps
                }
        
        students_analytics.append({
            "student_id": student.id,
            "student_name": student.name,
            "student_email": student.email,
            "student_number": student.student_id,
            "groups": [{"id": g.id, "name": g.name} for g in student_groups],
            "active_courses_count": len(active_courses),
            "total_steps": total_steps,
            "completed_steps": completed_steps,
            "completion_percentage": round(completion_percentage, 1),
            "total_assignments": total_assignments,
            "completed_assignments": completed_assignments,
            "assignment_score_percentage": round(assignment_score_percentage, 1),
            "total_study_time_minutes": student.total_study_time_minutes,
            "daily_streak": student.daily_streak,
            "last_activity_date": student.last_activity_date,
            "last_lesson": last_lesson_info
        })
    
    return {
        "group_info": {
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "teacher_name": group.teacher.name if group.teacher else None,
            "curator_name": group.curator.name if group.curator else None
        },
        "students": students_analytics,
        "total_students": len(students_analytics)
    }

@router.get("/student/{student_id}/progress-history")
async def get_student_progress_history(
    student_id: int,
    course_id: Optional[int] = None,
    days: int = Query(30, description="Number of days to look back"),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить историю прогресса студента"""
    
    # Проверка прав доступа
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Проверяем доступ к студенту (аналогично другим эндпоинтам)
    student = db.query(UserInDB).filter(
        UserInDB.id == student_id, 
        UserInDB.role == "student"
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    # Проверка прав доступа к студенту
    if current_user.role == "teacher":
        teacher_groups = db.query(Group.id).filter(Group.teacher_id == current_user.id).subquery()
        teacher_courses = db.query(Course.id).filter(Course.teacher_id == current_user.id).subquery()
        
        group_access = db.query(GroupStudent).filter(
            GroupStudent.student_id == student_id,
            GroupStudent.group_id.in_(teacher_groups)
        ).first()
        
        course_access = db.query(Enrollment).filter(
            Enrollment.user_id == student_id,
            Enrollment.course_id.in_(teacher_courses)
        ).first()
        
        if not group_access and not course_access:
            raise HTTPException(status_code=403, detail="Access denied to this student")
    
    elif current_user.role == "curator":
        curator_groups = db.query(Group.id).filter(Group.curator_id == current_user.id).subquery()
        group_access = db.query(GroupStudent).filter(
            GroupStudent.student_id == student_id,
            GroupStudent.group_id.in_(curator_groups)
        ).first()
        
        if not group_access:
            raise HTTPException(status_code=403, detail="Access denied to this student")
    
    # Получаем историю прогресса
    start_date = date.today() - timedelta(days=days)
    
    snapshots_query = db.query(ProgressSnapshot).filter(
        ProgressSnapshot.user_id == student_id,
        ProgressSnapshot.snapshot_date >= start_date
    ).order_by(ProgressSnapshot.snapshot_date)
    
    if course_id:
        snapshots_query = snapshots_query.filter(ProgressSnapshot.course_id == course_id)
    
    snapshots = snapshots_query.all()
    
    # Форматируем данные для графика
    history_data = []
    for snapshot in snapshots:
        history_data.append({
            "date": snapshot.snapshot_date.isoformat(),
            "completion_percentage": snapshot.completion_percentage,
            "completed_steps": snapshot.completed_steps,
            "total_steps": snapshot.total_steps,
            "total_time_spent_minutes": snapshot.total_time_spent_minutes,
            "assignments_completed": snapshot.assignments_completed,
            "total_assignments": snapshot.total_assignments,
            "assignment_score_percentage": snapshot.assignment_score_percentage
        })
    
    return {
        "student_info": {
            "id": student.id,
            "name": student.name,
            "student_id": student.student_id
        },
        "course_id": course_id,
        "period_days": days,
        "history": history_data
    }

async def generate_student_pdf_report(student_data: dict, progress_data: dict) -> bytes:
    """Генерация PDF отчета для студента"""
    try:
        from reportlab.lib.pagesizes import letter, A4
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.lib import colors
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4)
        styles = getSampleStyleSheet()
        story = []
        
        # Заголовок отчета
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=18,
            spaceAfter=30,
            alignment=1  # Center alignment
        )
        
        story.append(Paragraph("Отчет о прогрессе студента", title_style))
        story.append(Spacer(1, 12))
        
        # Информация о студенте
        student_info = [
            ['Имя:', student_data.get('student_name', 'N/A')],
            ['Email:', student_data.get('student_email', 'N/A')],
            ['Номер студента:', student_data.get('student_number', 'N/A')],
            ['Общий прогресс:', f"{student_data.get('completion_percentage', 0)}%"],
            ['Время обучения:', f"{student_data.get('total_study_time_minutes', 0)} мин"],
            ['Дневная серия:', f"{student_data.get('daily_streak', 0)} дней"],
        ]
        
        student_table = Table(student_info, colWidths=[2*inch, 3*inch])
        student_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, -1), colors.lightgrey),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
            ('BACKGROUND', (1, 0), (1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ]))
        
        story.append(Paragraph("Информация о студенте", styles['Heading2']))
        story.append(student_table)
        story.append(Spacer(1, 12))
        
        # Прогресс по курсам
        if progress_data and 'courses' in progress_data:
            story.append(Paragraph("Прогресс по курсам", styles['Heading2']))
            
            for course in progress_data['courses']:
                story.append(Paragraph(f"Курс: {course.get('course_title', 'N/A')}", styles['Heading3']))
                
                course_info = [
                    ['Преподаватель:', course.get('teacher_name', 'N/A')],
                    ['Модули:', str(len(course.get('modules', [])))],
                ]
                
                course_table = Table(course_info, colWidths=[2*inch, 3*inch])
                course_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (0, -1), colors.lightblue),
                    ('GRID', (0, 0), (-1, -1), 1, colors.black),
                    ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                    ('FONTSIZE', (0, 0), (-1, -1), 9),
                ]))
                
                story.append(course_table)
                story.append(Spacer(1, 12))
        
        # Статистика заданий
        story.append(Paragraph("Статистика выполнения заданий", styles['Heading2']))
        assignment_info = [
            ['Всего заданий:', str(student_data.get('total_assignments', 0))],
            ['Выполнено:', str(student_data.get('completed_assignments', 0))],
            ['Средний балл:', f"{student_data.get('assignment_score_percentage', 0)}%"],
        ]
        
        assignment_table = Table(assignment_info, colWidths=[2*inch, 3*inch])
        assignment_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, -1), colors.lightgreen),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
        ]))
        
        story.append(assignment_table)
        story.append(Spacer(1, 12))
        
        # Дата генерации отчета
        story.append(Paragraph(f"Отчет сгенерирован: {datetime.now().strftime('%d.%m.%Y %H:%M')}", styles['Normal']))
        
        doc.build(story)
        buffer.seek(0)
        return buffer.getvalue()
        
    except ImportError:
        # Если reportlab не установлен, возвращаем простой текстовый отчет
        report_text = f"""
ОТЧЕТ О ПРОГРЕССЕ СТУДЕНТА

Имя: {student_data.get('student_name', 'N/A')}
Email: {student_data.get('student_email', 'N/A')}
Номер студента: {student_data.get('student_number', 'N/A')}

ПРОГРЕСС:
- Общий прогресс: {student_data.get('completion_percentage', 0)}%
- Выполнено шагов: {student_data.get('completed_steps', 0)} из {student_data.get('total_steps', 0)}
- Время обучения: {student_data.get('total_study_time_minutes', 0)} минут
- Дневная серия: {student_data.get('daily_streak', 0)} дней

ЗАДАНИЯ:
- Всего заданий: {student_data.get('total_assignments', 0)}
- Выполнено: {student_data.get('completed_assignments', 0)}
- Средний балл: {student_data.get('assignment_score_percentage', 0)}%

Отчет сгенерирован: {datetime.now().strftime('%d.%m.%Y %H:%M')}
        """
        return report_text.encode('utf-8')

@router.post("/export/student/{student_id}")
async def export_student_report(
    student_id: int,
    course_id: Optional[int] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Экспорт PDF отчета по студенту"""
    
    # Проверка прав доступа
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Получаем данные студента (используем существующий эндпоинт)
    try:
        # Получаем базовые данные студента из эндпоинта all students
        all_students_data = get_all_students_analytics(current_user, db)
        student_data = None
        
        for student in all_students_data['students']:
            if student['student_id'] == student_id:
                student_data = student
                break
        
        if not student_data:
            raise HTTPException(status_code=404, detail="Student not found or access denied")
        
        # Получаем детальные данные прогресса
        progress_data = get_detailed_student_analytics(student_id, course_id, current_user, db)
        
        # Генерируем PDF
        pdf_content = generate_student_pdf_report(student_data, progress_data)
        
        # Формируем имя файла
        filename = f"student_report_{student_data.get('student_number', student_id)}_{datetime.now().strftime('%Y%m%d')}.pdf"
        
        return Response(
            content=pdf_content,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate report: {str(e)}")

@router.post("/export/group/{group_id}")
async def export_group_report(
    group_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Экспорт PDF отчета по группе"""
    
    # Проверка прав доступа
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        # Получаем данные группы
        group_data = get_group_students_analytics(group_id, current_user, db)
        
        # Генерируем PDF отчет для группы
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.lib.units import inch
            from reportlab.lib import colors
            
            buffer = BytesIO()
            doc = SimpleDocTemplate(buffer, pagesize=A4)
            styles = getSampleStyleSheet()
            story = []
            
            # Заголовок
            story.append(Paragraph(f"Отчет по группе: {group_data['group_info']['name']}", styles['Title']))
            story.append(Spacer(1, 12))
            
            # Информация о группе
            group_info = [
                ['Название группы:', group_data['group_info']['name']],
                ['Описание:', group_data['group_info']['description'] or 'N/A'],
                ['Преподаватель:', group_data['group_info']['teacher_name'] or 'N/A'],
                ['Куратор:', group_data['group_info']['curator_name'] or 'N/A'],
                ['Количество студентов:', str(group_data['total_students'])],
            ]
            
            group_table = Table(group_info, colWidths=[2*inch, 4*inch])
            group_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (0, -1), colors.lightgrey),
                ('GRID', (0, 0), (-1, -1), 1, colors.black),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
            ]))
            
            story.append(Paragraph("Информация о группе", styles['Heading2']))
            story.append(group_table)
            story.append(Spacer(1, 12))
            
            # Таблица студентов
            if group_data['students']:
                story.append(Paragraph("Студенты группы", styles['Heading2']))
                
                student_data = [['Имя', 'Email', 'Прогресс %', 'Время (мин)', 'Задания']]
                
                for student in group_data['students']:
                    student_data.append([
                        student['student_name'],
                        student['student_email'],
                        f"{student['completion_percentage']}%",
                        str(student['total_study_time_minutes']),
                        f"{student['completed_assignments']}/{student['total_assignments']}"
                    ])
                
                students_table = Table(student_data, colWidths=[1.5*inch, 2*inch, 1*inch, 1*inch, 1*inch])
                students_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, 0), 10),
                    ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                    ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
                    ('GRID', (0, 0), (-1, -1), 1, colors.black)
                ]))
                
                story.append(students_table)
            
            story.append(Spacer(1, 12))
            story.append(Paragraph(f"Отчет сгенерирован: {datetime.now().strftime('%d.%m.%Y %H:%M')}", styles['Normal']))
            
            doc.build(story)
            buffer.seek(0)
            pdf_content = buffer.getvalue()
            
        except ImportError:
            # Fallback к текстовому отчету
            report_text = f"""
ОТЧЕТ ПО ГРУППЕ: {group_data['group_info']['name']}

ИНФОРМАЦИЯ О ГРУППЕ:
- Описание: {group_data['group_info']['description'] or 'N/A'}
- Преподаватель: {group_data['group_info']['teacher_name'] or 'N/A'}
- Куратор: {group_data['group_info']['curator_name'] or 'N/A'}
- Количество студентов: {group_data['total_students']}

СТУДЕНТЫ:
"""
            for student in group_data['students']:
                report_text += f"""
- {student['student_name']} ({student['student_email']})
  Прогресс: {student['completion_percentage']}%
  Время обучения: {student['total_study_time_minutes']} мин
  Задания: {student['completed_assignments']}/{student['total_assignments']}
"""
            
            report_text += f"\nОтчет сгенерирован: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
            pdf_content = report_text.encode('utf-8')
        
        filename = f"group_report_{group_data['group_info']['name']}_{datetime.now().strftime('%Y%m%d')}.pdf"
        
        return Response(
            content=pdf_content,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate group report: {str(e)}")

@router.post("/export/all-students")
async def export_all_students_report(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Экспорт PDF отчета по всем доступным студентам"""
    
    # Проверка прав доступа
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        # Получаем данные всех студентов (дублируем логику из get_all_students_analytics)
        students_query = db.query(UserInDB).filter(UserInDB.role == "student", UserInDB.is_active == True)
        
        # Фильтрация по ролям
        if current_user.role == "teacher":
            teacher_groups = db.query(Group.id).filter(Group.teacher_id == current_user.id).subquery()
            teacher_courses = db.query(Course.id).filter(Course.teacher_id == current_user.id).subquery()
            
            group_students = db.query(GroupStudent.student_id).filter(GroupStudent.group_id.in_(teacher_groups)).subquery()
            course_students = db.query(Enrollment.user_id).filter(Enrollment.course_id.in_(teacher_courses)).subquery()
            
            students_query = students_query.filter(
                or_(
                    UserInDB.id.in_(group_students),
                    UserInDB.id.in_(course_students)
                )
            )
        
        elif current_user.role == "curator":
            curator_groups = db.query(Group.id).filter(Group.curator_id == current_user.id).subquery()
            group_students = db.query(GroupStudent.student_id).filter(GroupStudent.group_id.in_(curator_groups)).subquery()
            
            students_query = students_query.filter(UserInDB.id.in_(group_students))
        
        students = students_query.all()
        
        students_analytics = []
        for student in students:
            # Получаем группы студента
            student_groups = db.query(Group).join(GroupStudent).filter(
                GroupStudent.student_id == student.id
            ).all()
            
            # Получаем ВСЕ курсы где есть прогресс студента (не через Enrollment!)
            # Используем StepProgress чтобы найти курсы где студент действительно учится
            courses_with_progress = db.query(Course).join(
                Module, Module.course_id == Course.id
            ).join(
                Lesson, Lesson.module_id == Module.id
            ).join(
                Step, Step.lesson_id == Lesson.id
            ).join(
                StepProgress, StepProgress.step_id == Step.id
            ).filter(
                StepProgress.user_id == student.id
            ).distinct().all()
            
            # Если нет прогресса, пробуем через Enrollment
            if not courses_with_progress:
                courses_with_progress = db.query(Course).join(Enrollment).filter(
                    Enrollment.user_id == student.id,
                    Course.is_active == True
                ).all()
            
            active_courses = courses_with_progress
            
            # Подсчитываем общий прогресс
            total_steps = 0
            completed_steps = 0
            total_assignments = 0
            completed_assignments = 0
            total_assignment_score = 0
            total_max_score = 0
            
            for course in active_courses:
                # Подсчет шагов
                course_steps = db.query(Step).join(Lesson).join(Module).filter(
                    Module.course_id == course.id
                ).count()
                total_steps += course_steps
                
                # Правильный подсчет завершенных шагов через JOIN (как в детальном прогрессе)
                course_completed_steps = db.query(StepProgress).join(
                    Step, StepProgress.step_id == Step.id
                ).join(
                    Lesson, Step.lesson_id == Lesson.id
                ).join(
                    Module, Lesson.module_id == Module.id
                ).filter(
                    StepProgress.user_id == student.id,
                    Module.course_id == course.id,
                    StepProgress.status == "completed"
                ).count()
                completed_steps += course_completed_steps
                
                # Подсчет заданий
                course_assignments = db.query(Assignment).join(Lesson).join(Module).filter(
                    Module.course_id == course.id
                ).all()
                total_assignments += len(course_assignments)
                
                for assignment in course_assignments:
                    submission = db.query(AssignmentSubmission).filter(
                        AssignmentSubmission.assignment_id == assignment.id,
                        AssignmentSubmission.user_id == student.id
                    ).first()
                    
                    if submission and submission.is_graded:
                        completed_assignments += 1
                        total_assignment_score += submission.score or 0
                        total_max_score += assignment.max_score or 0
            
            # Вычисляем проценты
            completion_percentage = (completed_steps / total_steps * 100) if total_steps > 0 else 0
            assignment_score_percentage = (total_assignment_score / total_max_score * 100) if total_max_score > 0 else 0
            
            # Получаем информацию о последнем уроке
            last_lesson_info = None
            last_step_progress = db.query(StepProgress).filter(
                StepProgress.user_id == student.id
            ).order_by(StepProgress.visited_at.desc()).first()
            
            if last_step_progress:
                # Получаем информацию об уроке
                lesson = db.query(Lesson).join(Step).filter(
                    Step.id == last_step_progress.step_id
                ).first()
                
                if lesson:
                    # Считаем прогресс урока
                    total_lesson_steps = db.query(Step).filter(
                        Step.lesson_id == lesson.id
                    ).count()
                    
                    completed_lesson_steps = db.query(StepProgress).join(Step).filter(
                        StepProgress.user_id == student.id,
                        Step.lesson_id == lesson.id,
                        StepProgress.status == "completed"
                    ).count()
                    
                    lesson_progress_percentage = (completed_lesson_steps / total_lesson_steps * 100) if total_lesson_steps > 0 else 0
                    
                    last_lesson_info = {
                        "lesson_title": lesson.title,
                        "lesson_progress_percentage": round(lesson_progress_percentage, 1),
                        "completed_steps": completed_lesson_steps,
                        "total_steps": total_lesson_steps
                    }
            
            students_analytics.append({
                "student_id": student.id,
                "student_name": student.name,
                "student_email": student.email,
                "student_number": student.student_id,
                "groups": [{"id": g.id, "name": g.name} for g in student_groups],
                "active_courses_count": len(active_courses),
                "total_steps": total_steps,
                "completed_steps": completed_steps,
                "completion_percentage": round(completion_percentage, 1),
                "total_assignments": total_assignments,
                "completed_assignments": completed_assignments,
                "assignment_score_percentage": round(assignment_score_percentage, 1),
                "total_study_time_minutes": student.total_study_time_minutes,
                "daily_streak": student.daily_streak,
                "last_activity_date": student.last_activity_date,
                "last_lesson": last_lesson_info
            })
        
        all_students_data = {
            "students": students_analytics,
            "total_students": len(students_analytics)
        }
        
        # Debug logging
        print(f"DEBUG: Total students found: {len(students_analytics)}")
        print(f"DEBUG: Students data: {students_analytics[:2] if students_analytics else 'No students'}")
        
        # Проверяем, есть ли данные
        if len(students_analytics) == 0:
            # Если нет студентов, возвращаем пустой отчет с сообщением
            report_text = f"""
NO STUDENTS FOUND

Your role: {current_user.role}
User ID: {current_user.id}

No students are accessible with your current permissions.

Report generated: {datetime.now().strftime('%d.%m.%Y %H:%M')}
"""
            return Response(
                content=report_text.encode('utf-8'),
                media_type="text/plain",
                headers={"Content-Disposition": f"attachment; filename=no_students_{datetime.now().strftime('%Y%m%d')}.txt"}
            )
        
        # Генерируем PDF отчет
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.lib.units import inch
            from reportlab.lib import colors
            
            buffer = BytesIO()
            doc = SimpleDocTemplate(buffer, pagesize=A4)
            styles = getSampleStyleSheet()
            story = []
            
            # Title (English to avoid Cyrillic encoding issues)
            story.append(Paragraph("All Students Report", styles['Title']))
            story.append(Spacer(1, 12))
            
            # Overall Statistics
            total_students = all_students_data['total_students']
            avg_completion = sum(s['completion_percentage'] for s in all_students_data['students']) / total_students if total_students > 0 else 0
            total_study_time = sum(s['total_study_time_minutes'] for s in all_students_data['students'])
            
            summary_info = [
                ['Total Students:', str(total_students)],
                ['Average Progress:', f"{avg_completion:.1f}%"],
                ['Total Study Time:', f"{total_study_time} min ({total_study_time//60} h)"],
            ]
            
            summary_table = Table(summary_info, colWidths=[2*inch, 3*inch])
            summary_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (0, -1), colors.lightblue),
                ('GRID', (0, 0), (-1, -1), 1, colors.black),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
            ]))
            
            story.append(Paragraph("Overall Statistics", styles['Heading2']))
            story.append(summary_table)
            story.append(Spacer(1, 12))
            
            # Students Table
            if all_students_data['students']:
                story.append(Paragraph("Detailed Student Information", styles['Heading2']))
                
                student_data = [['Name', 'Groups', 'Progress %', 'Courses', 'Time (h)']]
                
                for student in all_students_data['students']:
                    groups_str = ', '.join([g['name'] for g in student['groups']]) if student['groups'] else 'No group'
                    student_data.append([
                        student['student_name'],
                        groups_str[:20] + '...' if len(groups_str) > 20 else groups_str,
                        f"{student['completion_percentage']}%",
                        str(student['active_courses_count']),
                        str(student['total_study_time_minutes'] // 60)
                    ])
                
                students_table = Table(student_data, colWidths=[1.5*inch, 1.5*inch, 1*inch, 0.8*inch, 1*inch])
                students_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 8),
                    ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                    ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
                    ('GRID', (0, 0), (-1, -1), 1, colors.black)
                ]))
                
                story.append(students_table)
            
            story.append(Spacer(1, 12))
            story.append(Paragraph(f"Report generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles['Normal']))
            
            doc.build(story)
            buffer.seek(0)
            pdf_content = buffer.getvalue()
            
        except ImportError:
            # Fallback to text report
            report_text = f"""
ALL STUDENTS REPORT

OVERALL STATISTICS:
- Total Students: {all_students_data['total_students']}
- Average Progress: {sum(s['completion_percentage'] for s in all_students_data['students']) / all_students_data['total_students'] if all_students_data['total_students'] > 0 else 0:.1f}%
- Total Study Time: {sum(s['total_study_time_minutes'] for s in all_students_data['students'])} min

STUDENTS:
"""
            for student in all_students_data['students']:
                groups_str = ', '.join([g['name'] for g in student['groups']]) if student['groups'] else 'No group'
                report_text += f"""
- {student['student_name']} ({student['student_email']})
  Groups: {groups_str}
  Progress: {student['completion_percentage']}%
  Active Courses: {student['active_courses_count']}
  Study Time: {student['total_study_time_minutes']} min
"""
            
            report_text += f"\nReport generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            pdf_content = report_text.encode('utf-8')
        
        filename = f"all_students_report_{datetime.now().strftime('%Y%m%d')}.pdf"
        
        return Response(
            content=pdf_content,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate all students report: {str(e)}")

# =============================================================================
# DETAILED STEP-BY-STEP PROGRESS TRACKING
# =============================================================================

@router.get("/student/{student_id}/detailed-progress")
async def get_student_detailed_progress(
    student_id: int,
    course_id: Optional[int] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Get detailed step-by-step progress for a student
    Shows each step, completion time, and order of completion
    Properly validates curator access via group membership
    """
    
    # Check role-based access
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Validate student access (now properly checks curator permissions)
    if current_user.role != "admin" and not check_student_access(student_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this student")
    
    try:
        # Получаем информацию о студенте
        student = db.query(UserInDB).filter(UserInDB.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="Student not found")
        
        # Базовый запрос для прогресса по шагам
        query = db.query(
            StepProgress,
            Step,
            Lesson,
            Module,
            Course
        ).join(Step, StepProgress.step_id == Step.id)\
         .join(Lesson, Step.lesson_id == Lesson.id)\
         .join(Module, Lesson.module_id == Module.id)\
         .join(Course, Module.course_id == Course.id)\
         .filter(StepProgress.user_id == student_id)
        
        if course_id:
            query = query.filter(Course.id == course_id)
        
        # Получаем все записи прогресса
        progress_records = query.order_by(StepProgress.visited_at.desc()).all()
        
        # Группируем по курсам
        courses_progress = {}
        
        for step_progress, step, lesson, module, course in progress_records:
            course_key = course.id
            
            if course_key not in courses_progress:
                courses_progress[course_key] = {
                    "course_info": {
                        "id": course.id,
                        "title": course.title,
                        "description": course.description
                    },
                    "modules": {}
                }
            
            module_key = module.id
            if module_key not in courses_progress[course_key]["modules"]:
                courses_progress[course_key]["modules"][module_key] = {
                    "module_info": {
                        "id": module.id,
                        "title": module.title,
                        "order_index": module.order_index
                    },
                    "lessons": {}
                }
            
            lesson_key = lesson.id
            if lesson_key not in courses_progress[course_key]["modules"][module_key]["lessons"]:
                courses_progress[course_key]["modules"][module_key]["lessons"][lesson_key] = {
                    "lesson_info": {
                        "id": lesson.id,
                        "title": lesson.title,
                        "order_index": lesson.order_index
                    },
                    "steps": []
                }
            
            # Добавляем информацию о шаге
            step_info = {
                "step_id": step.id,
                "step_title": step.title,
                "step_order": step.order_index,
                "content_type": step.content_type,
                "progress": {
                    "status": step_progress.status,
                    "started_at": step_progress.started_at.isoformat() if step_progress.started_at else None,
                    "visited_at": step_progress.visited_at.isoformat() if step_progress.visited_at else None,
                    "completed_at": step_progress.completed_at.isoformat() if step_progress.completed_at else None,
                    "time_spent_minutes": step_progress.time_spent_minutes,
                    "attempts": 1  # Можно расширить для отслеживания попыток
                }
            }
            
            courses_progress[course_key]["modules"][module_key]["lessons"][lesson_key]["steps"].append(step_info)
        
        # Получаем общую статистику
        total_steps_query = db.query(func.count(Step.id)).join(Lesson).join(Module)
        completed_steps_query = db.query(func.count(StepProgress.id)).join(Step).join(Lesson).join(Module).filter(
            StepProgress.user_id == student_id,
            StepProgress.status == 'completed'
        )
        
        if course_id:
            total_steps_query = total_steps_query.filter(Module.course_id == course_id)
            completed_steps_query = completed_steps_query.filter(Module.course_id == course_id)
        
        total_steps = total_steps_query.scalar() or 0
        completed_steps = completed_steps_query.scalar() or 0
        
        # Получаем временную статистику
        first_activity = db.query(func.min(StepProgress.visited_at)).filter(
            StepProgress.user_id == student_id
        ).scalar()
        
        last_activity = db.query(func.max(StepProgress.visited_at)).filter(
            StepProgress.user_id == student_id
        ).scalar()
        
        total_study_time = db.query(func.sum(StepProgress.time_spent_minutes)).filter(
            StepProgress.user_id == student_id
        ).scalar() or 0
        
        # Получаем активность по дням (последние 30 дней)
        from datetime import datetime, timedelta
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        
        daily_activity = db.query(
            func.date(StepProgress.visited_at).label('date'),
            func.count(StepProgress.id).label('steps_completed'),
            func.sum(StepProgress.time_spent_minutes).label('time_spent')
        ).filter(
            StepProgress.user_id == student_id,
            StepProgress.visited_at >= thirty_days_ago
        ).group_by(func.date(StepProgress.visited_at)).all()
        
        # 1. Получаем сложные темы и вопросы
        difficult_questions = []
        difficult_topics_map = {} # lesson_id -> {title, error_count}
        
        # Получаем все завершенные попытки квизов студента в этом курсе
        quiz_attempts = db.query(QuizAttempt, Step, Lesson).join(
            Step, QuizAttempt.step_id == Step.id
        ).join(
            Lesson, Step.lesson_id == Lesson.id
        ).filter(
            QuizAttempt.user_id == student_id,
            QuizAttempt.is_draft == False
        )
        
        if course_id:
            quiz_attempts = quiz_attempts.filter(QuizAttempt.course_id == course_id)
            
        quiz_attempts = quiz_attempts.order_by(QuizAttempt.created_at.desc()).all()
        
        seen_question_ids = set()
        lesson_question_counts = {}  # Track count per lesson for diversity
        
        for attempt, step, lesson in quiz_attempts:
            if attempt.score_percentage < 100:
                # Добавляем в сложные темы
                if lesson.id not in difficult_topics_map:
                    difficult_topics_map[lesson.id] = {
                        "id": lesson.id,
                        "title": lesson.title,
                        "error_count": 0
                    }
                difficult_topics_map[lesson.id]["error_count"] += 1
                
                # Извлекаем конкретные ошибки из ответов, если возможно
                try:
                    import json
                    step_content = json.loads(step.content_text) if step.content_text else {}
                    questions = step_content.get("questions", [])
                    user_answers = json.loads(attempt.answers) if attempt.answers else {}
                    
                    # Если answers в формате списка [[id, val], ...]
                    if isinstance(user_answers, list):
                        user_answers = {str(k): v for k, v in user_answers}
                    
                    for q in questions:
                        q_id = q.get("id")
                        if not q_id or q_id in seen_question_ids:
                            continue
                            
                        # Проверка на лимит (30) и разнообразие (макс 5 на урок)
                        current_lesson_count = lesson_question_counts.get(lesson.id, 0)
                        
                        if len(difficult_questions) < 30 and current_lesson_count < 5:
                            difficult_questions.append({
                                "id": q_id,
                                "text": q.get("question_text", "Unknown Question"),
                                "type": q.get("question_type"),
                                "lesson_id": lesson.id,
                                "lesson_title": lesson.title,
                                "step_id": step.id
                            })
                            seen_question_ids.add(q_id)
                            lesson_question_counts[lesson.id] = current_lesson_count + 1
                except:
                    pass

        # Сортируем сложные темы по количеству ошибок
        difficult_topics = sorted(difficult_topics_map.values(), key=lambda x: x["error_count"], reverse=True)[:5]

        # 2. Формируем историю активности
        activity_history = []
        
        # Шаги (посещения и завершения)
        for sp, step, lesson, module, course in progress_records:
            if sp.visited_at:
                activity_history.append({
                    "type": "step_visited",
                    "title": f"Visited: {step.title}",
                    "context": f"{lesson.title} • {course.title}",
                    "timestamp": sp.visited_at.isoformat(),
                    "date": sp.visited_at.date()
                })
            if sp.completed_at:
                activity_history.append({
                    "type": "step_completed",
                    "title": f"Completed: {step.title}",
                    "context": f"{lesson.title} • {course.title}",
                    "timestamp": sp.completed_at.isoformat(),
                    "date": sp.completed_at.date()
                })
        
        # Квизы (попытки)
        for attempt, step, lesson in quiz_attempts:
            activity_history.append({
                "type": "quiz_attempt",
                "title": f"Quiz: {attempt.quiz_title or step.title}",
                "context": f"Score: {attempt.score_percentage}% • {lesson.title}",
                "timestamp": attempt.created_at.isoformat(),
                "date": attempt.created_at.date()
            })
            
        # Сортируем историю по времени
        activity_history.sort(key=lambda x: x["timestamp"], reverse=True)
        # Ограничиваем последние 30 событий
        activity_history = activity_history[:30]

        # =====================================================================
        # HOMEWORK (ASSIGNMENTS)
        # =====================================================================
        
        # 1. Получаем список групп студента
        from src.schemas.models import GroupStudent, Assignment, AssignmentSubmission, CourseGroupAccess
        student_group_ids = [gs.group_id for gs in db.query(GroupStudent).filter(GroupStudent.student_id == student_id).all()]
        
        # 2. Получаем ID уроков в курсе (если course_id указан)
        course_lesson_ids = []
        if course_id:
            course_lesson_ids = [l.id for l in db.query(Lesson.id).join(Module).filter(Module.course_id == course_id).all()]
        
        # 3. Запрос на все задания, доступные студенту
        # Это задания, привязанные к урокам курса ИЛИ к группам студента
        hw_query = db.query(Assignment).filter(Assignment.is_active == True)
        
        if course_id:
            # Если указан курс, берем задания этого курса + задания групп этого студента, которые имеют доступ к этому курсу
            group_ids_with_course = [cga.group_id for cga in db.query(CourseGroupAccess).filter(
                CourseGroupAccess.course_id == course_id,
                CourseGroupAccess.group_id.in_(student_group_ids) if student_group_ids else False
            ).all()]
            
            hw_query = hw_query.filter(
                (Assignment.lesson_id.in_(course_lesson_ids)) | 
                (Assignment.group_id.in_(group_ids_with_course))
            )
        else:
            # Если курс не указан, берем все задания всех групп студента
            hw_query = hw_query.filter(Assignment.group_id.in_(student_group_ids) if student_group_ids else False)

        assignments = hw_query.all()
        
        # 4. Получаем все субмишны студента для этих заданий
        submission_map = {
            s.assignment_id: s 
            for s in db.query(AssignmentSubmission).filter(
                AssignmentSubmission.user_id == student_id,
                AssignmentSubmission.assignment_id.in_([a.id for a in assignments]) if assignments else False
            ).all()
        }
        
        homework_data = []
        for a in assignments:
            submission = submission_map.get(a.id)
            homework_data.append({
                "id": a.id,
                "title": a.title,
                "due_date": a.due_date.isoformat() if a.due_date else None,
                "status": "submitted" if submission else "pending",
                "score": submission.score if submission else None,
                "max_score": a.max_score,
                "submitted_at": submission.submitted_at.isoformat() if submission and hasattr(submission, 'submitted_at') else (submission.created_at.isoformat() if submission else None),
                "is_graded": submission.is_graded if submission else False
            })

        return {
            "student_info": {
                "id": student.id,
                "name": student.name,
                "email": student.email,
                "student_id": getattr(student, 'student_id', None)
            },
            "total_stats": {
                "total_steps": total_steps,
                "completed_steps": completed_steps,
                "completion_percentage": (completed_steps / total_steps * 100) if total_steps > 0 else 0,
                "total_study_time": total_study_time,
                "first_activity": first_activity.isoformat() if first_activity else None,
                "last_activity": last_activity.isoformat() if last_activity else None,
                "study_period_days": (last_activity - first_activity).days if first_activity and last_activity else 0
            },
            "daily_activity": [
                {
                    "date": activity.date.isoformat(),
                    "steps_completed": activity.steps_completed,
                    "time_spent_minutes": activity.time_spent or 0
                }
                for activity in daily_activity
            ],
            "courses": courses_progress,
            "difficult_topics": difficult_topics,
            "difficult_questions": difficult_questions,
            "activity_history": activity_history,
            "homework": homework_data
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get detailed progress: {str(e)}")

@router.get("/student/{student_id}/learning-path")
async def get_student_learning_path(
    student_id: int,
    course_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Get student learning path - chronological order of step completion
    Properly validates curator access via group membership
    """
    
    # Check role-based access
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Validate student access (now properly checks curator permissions)
    if current_user.role != "admin" and not check_student_access(student_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this student")
    
    try:
        # Получаем хронологический путь обучения
        learning_path = db.query(
            StepProgress,
            Step,
            Lesson,
            Module
        ).join(Step, StepProgress.step_id == Step.id)\
         .join(Lesson, Step.lesson_id == Lesson.id)\
         .join(Module, Lesson.module_id == Module.id)\
         .filter(
            StepProgress.user_id == student_id,
            Module.course_id == course_id
        ).order_by(StepProgress.visited_at.asc()).all()
        
        path_data = []
        for i, (step_progress, step, lesson, module) in enumerate(learning_path):
            # Вычисляем время между шагами
            time_since_previous = None
            if i > 0 and learning_path[i-1][0].visited_at and step_progress.visited_at:
                time_diff = step_progress.visited_at - learning_path[i-1][0].visited_at
                time_since_previous = int(time_diff.total_seconds() / 60)  # в минутах
            
            path_data.append({
                "sequence_number": i + 1,
                "step_info": {
                    "id": step.id,
                    "title": step.title,
                    "content_type": step.content_type,
                    "order_index": step.order_index
                },
                "lesson_info": {
                    "id": lesson.id,
                    "title": lesson.title,
                    "order_index": lesson.order_index
                },
                "module_info": {
                    "id": module.id,
                    "title": module.title,
                    "order_index": module.order_index
                },
                "progress_info": {
                    "visited_at": step_progress.visited_at.isoformat() if step_progress.visited_at else None,
                    "completed_at": step_progress.completed_at.isoformat() if step_progress.completed_at else None,
                    "time_spent_minutes": step_progress.time_spent_minutes,
                    "status": step_progress.status,
                    "time_since_previous_step_minutes": time_since_previous
                }
            })
        
        return {
            "student_id": student_id,
            "course_id": course_id,
            "total_steps_completed": len(path_data),
            "learning_path": path_data
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get learning path: {str(e)}")

@router.get("/export-excel")
async def export_analytics_to_excel(
    course_id: int,
    group_id: Optional[int] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Export analytics data to Excel file with charts
    
    Args:
        course_id: ID of the course to export
        group_id: Optional group ID to filter students
    
    Returns:
        Excel file (.xlsx) with detailed analytics and charts
    """
    
    # Check permissions
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Check course access
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")
    
    try:
        # Get course info
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Get course overview
        course_overview_data = None
        try:
            # Reuse existing logic from get_course_analytics_overview
            students_with_progress = db.query(UserInDB).join(
                StepProgress, StepProgress.user_id == UserInDB.id
            ).join(
                Step, StepProgress.step_id == Step.id
            ).join(
                Lesson, Step.lesson_id == Lesson.id
            ).join(
                Module, Lesson.module_id == Module.id
            ).filter(
                Module.course_id == course_id,
                UserInDB.role == "student",
                UserInDB.is_active == True
            ).distinct().all()
            
            enrolled_students_ids = db.query(Enrollment.user_id).filter(
                Enrollment.course_id == course_id,
                Enrollment.is_active == True
            ).subquery()
            
            enrolled_no_progress = db.query(UserInDB).filter(
                UserInDB.id.in_(enrolled_students_ids),
                UserInDB.role == "student",
                UserInDB.is_active == True
            ).all()
            
            enrolled_students_set = {s.id: s for s in students_with_progress}
            for student in enrolled_no_progress:
                if student.id not in enrolled_students_set:
                    enrolled_students_set[student.id] = student
            
            enrolled_students = list(enrolled_students_set.values())
            
            # Get course structure
            modules = db.query(Module).filter(Module.course_id == course_id).all()
            total_lessons = sum(len(db.query(Lesson).filter(Lesson.module_id == m.id).all()) for m in modules)
            total_steps = sum(
                len(db.query(Step).filter(Step.lesson_id == l.id).all())
                for m in modules
                for l in db.query(Lesson).filter(Lesson.module_id == m.id).all()
            )
            
            # Calculate engagement metrics
            total_time = sum(s.total_study_time_minutes for s in enrolled_students)
            total_completed = db.query(StepProgress).join(
                Step, StepProgress.step_id == Step.id
            ).join(
                Lesson, Step.lesson_id == Lesson.id
            ).join(
                Module, Lesson.module_id == Module.id
            ).filter(
                Module.course_id == course_id,
                StepProgress.status == "completed"
            ).count()
            
            avg_completion = 0
            if enrolled_students and total_steps > 0:
                total_completion = 0
                for student in enrolled_students:
                    completed = db.query(StepProgress).join(
                        Step, StepProgress.step_id == Step.id
                    ).join(
                        Lesson, Step.lesson_id == Lesson.id
                    ).join(
                        Module, Lesson.module_id == Module.id
                    ).filter(
                        StepProgress.user_id == student.id,
                        Module.course_id == course_id,
                        StepProgress.status == "completed"
                    ).count()
                    total_completion += (completed / total_steps) * 100
                avg_completion = total_completion / len(enrolled_students)
            
            course_overview_data = {
                "course_info": {
                    "id": course.id,
                    "title": course.title,
                    "teacher_name": course.teacher.name if course.teacher else "N/A"
                },
                "structure": {
                    "total_modules": len(modules),
                    "total_lessons": total_lessons,
                    "total_steps": total_steps
                },
                "engagement": {
                    "total_enrolled_students": len(enrolled_students),
                    "total_time_spent_minutes": total_time,
                    "total_completed_steps": total_completed,
                    "average_completion_rate": avg_completion
                }
            }
        except Exception as e:
            print(f"Warning: Could not get course overview: {e}")
        
        # Get students data
        if group_id:
            # Filter by group
            group = db.query(Group).filter(Group.id == group_id).first()
            if not group:
                raise HTTPException(status_code=404, detail="Group not found")
            
            # Check group access
            if current_user.role == "teacher" and group.teacher_id != current_user.id:
                raise HTTPException(status_code=403, detail="Access denied to this group")
            elif current_user.role == "curator" and group.curator_id != current_user.id:
                raise HTTPException(status_code=403, detail="Access denied to this group")
            
            students = db.query(UserInDB).join(GroupStudent).filter(
                GroupStudent.group_id == group_id,
                UserInDB.is_active == True
            ).all()
            
            title_suffix = f" - {group.name}"
        else:
            students = enrolled_students
            title_suffix = ""
        
        # Prepare students data
        students_data = []
        for student in students:
            # Get courses with progress
            courses_with_progress = db.query(Course).join(
                Module, Module.course_id == Course.id
            ).join(
                Lesson, Lesson.module_id == Module.id
            ).join(
                Step, Step.lesson_id == Lesson.id
            ).join(
                StepProgress, StepProgress.step_id == Step.id
            ).filter(
                StepProgress.user_id == student.id
            ).distinct().all()
            
            if not courses_with_progress:
                courses_with_progress = db.query(Course).join(Enrollment).filter(
                    Enrollment.user_id == student.id,
                    Enrollment.is_active == True,
                    Course.is_active == True
                ).all()
            
            # Get student groups
            student_groups = db.query(Group).join(GroupStudent).filter(
                GroupStudent.student_id == student.id
            ).all()
            
            # Calculate metrics
            total_steps = 0
            completed_steps = 0
            total_assignments = 0
            completed_assignments = 0
            total_score = 0
            max_score = 0
            
            for course in courses_with_progress:
                course_steps = db.query(Step).join(Lesson).join(Module).filter(
                    Module.course_id == course.id
                ).count()
                total_steps += course_steps
                
                course_completed = db.query(StepProgress).join(
                    Step, StepProgress.step_id == Step.id
                ).join(
                    Lesson, Step.lesson_id == Lesson.id
                ).join(
                    Module, Lesson.module_id == Module.id
                ).filter(
                    StepProgress.user_id == student.id,
                    Module.course_id == course.id,
                    StepProgress.status == "completed"
                ).count()
                completed_steps += course_completed
                
                assignments = db.query(Assignment).join(Lesson).join(Module).filter(
                    Module.course_id == course.id
                ).all()
                total_assignments += len(assignments)
                
                for assignment in assignments:
                    submission = db.query(AssignmentSubmission).filter(
                        AssignmentSubmission.assignment_id == assignment.id,
                        AssignmentSubmission.user_id == student.id
                    ).first()
                    
                    if submission and submission.is_graded:
                        completed_assignments += 1
                        total_score += submission.score or 0
                        max_score += assignment.max_score or 0
            
            completion_pct = (completed_steps / total_steps * 100) if total_steps > 0 else 0
            score_pct = (total_score / max_score * 100) if max_score > 0 else 0
            
            # Получаем информацию о последнем уроке
            last_lesson_info = None
            last_step_progress = db.query(StepProgress).filter(
                StepProgress.user_id == student.id
            ).order_by(StepProgress.visited_at.desc()).first()
            
            if last_step_progress:
                # Получаем информацию об уроке
                lesson = db.query(Lesson).join(Step).filter(
                    Step.id == last_step_progress.step_id
                ).first()
                
                if lesson:
                    # Считаем прогресс урока
                    total_lesson_steps = db.query(Step).filter(
                        Step.lesson_id == lesson.id
                    ).count()
                    
                    completed_lesson_steps = db.query(StepProgress).join(Step).filter(
                        StepProgress.user_id == student.id,
                        Step.lesson_id == lesson.id,
                        StepProgress.status == "completed"
                    ).count()
                    
                    lesson_progress_percentage = (completed_lesson_steps / total_lesson_steps * 100) if total_lesson_steps > 0 else 0
                    
                    last_lesson_info = {
                        "lesson_title": lesson.title,
                        "lesson_progress_percentage": round(lesson_progress_percentage, 1),
                        "completed_steps": completed_lesson_steps,
                        "total_steps": total_lesson_steps
                    }
            
            students_data.append({
                "student_id": student.id,
                "student_name": student.name,
                "student_email": student.email,
                "student_number": student.student_id,
                "groups": [{"id": g.id, "name": g.name} for g in student_groups],
                "active_courses_count": len(courses_with_progress),
                "total_steps": total_steps,
                "completed_steps": completed_steps,
                "completion_percentage": completion_pct,
                "total_assignments": total_assignments,
                "completed_assignments": completed_assignments,
                "assignment_score_percentage": score_pct,
                "total_study_time_minutes": student.total_study_time_minutes,
                "daily_streak": student.daily_streak,
                "last_activity_date": student.last_activity_date,
                "last_lesson": last_lesson_info
            })
        
        # Get groups data if no specific group filter
        groups_data = None
        if not group_id:
            try:
                groups_with_students = db.query(Group).join(
                    GroupStudent, GroupStudent.group_id == Group.id
                ).join(
                    UserInDB, GroupStudent.student_id == UserInDB.id
                ).join(
                    StepProgress, StepProgress.user_id == UserInDB.id
                ).join(
                    Step, StepProgress.step_id == Step.id
                ).join(
                    Lesson, Step.lesson_id == Lesson.id
                ).join(
                    Module, Lesson.module_id == Module.id
                ).filter(
                    Module.course_id == course_id,
                    Group.is_active == True
                ).distinct().all()
                
                groups_data = []
                for group in groups_with_students:
                    group_students = db.query(UserInDB).join(GroupStudent).filter(
                        GroupStudent.group_id == group.id,
                        UserInDB.is_active == True
                    ).all()
                    
                    total_completion = 0
                    total_score = 0
                    total_time = 0
                    
                    for st in group_students:
                        completed = db.query(StepProgress).join(
                            Step, StepProgress.step_id == Step.id
                        ).join(
                            Lesson, Step.lesson_id == Lesson.id
                        ).join(
                            Module, Lesson.module_id == Module.id
                        ).filter(
                            StepProgress.user_id == st.id,
                            Module.course_id == course_id,
                            StepProgress.status == "completed"
                        ).count()
                        
                        if total_steps > 0:
                            total_completion += (completed / total_steps) * 100
                        
                        total_time += st.total_study_time_minutes
                    
                    avg_completion = total_completion / len(group_students) if group_students else 0
                    avg_time = total_time / len(group_students) if group_students else 0
                    
                    groups_data.append({
                        "group_id": group.id,
                        "group_name": group.name,
                        "description": group.description,
                        "teacher_name": group.teacher.name if group.teacher else None,
                        "curator_name": group.curator.name if group.curator else None,
                        "students_count": len(group_students),
                        "average_completion_percentage": avg_completion,
                        "average_assignment_score_percentage": 0,
                        "average_study_time_minutes": avg_time,
                        "created_at": group.created_at
                    })
            except Exception as e:
                print(f"Warning: Could not get groups data: {e}")
        
        # Format data for Excel export
        formatted_students_data = []
        for student_dict in students_data:
            last_lesson = student_dict.get("last_lesson")
            last_lesson_str = "N/A"
            if last_lesson:
                last_lesson_str = f"{last_lesson['lesson_title']} ({last_lesson['lesson_progress_percentage']}%)"
            
            formatted_students_data.append({
                "student_id": student_dict.get("student_id"),
                "student_name": student_dict.get("student_name", "N/A"),
                "email": student_dict.get("student_email", "N/A"),
                "groups": [g["name"] for g in student_dict.get("groups", [])],
                "progress_percentage": student_dict.get("completion_percentage", 0),
                "completed_steps": student_dict.get("completed_steps", 0),
                "total_steps": student_dict.get("total_steps", 0),
                "assignments_completed": student_dict.get("completed_assignments", 0),
                "total_assignments": student_dict.get("total_assignments", 0),
                "average_score": student_dict.get("assignment_score_percentage", 0),
                "total_study_time": student_dict.get("total_study_time_minutes", 0),
                "current_streak": student_dict.get("daily_streak", 0),
                "last_activity": str(student_dict.get("last_activity_date", "Never")),
                "current_lesson": last_lesson_str
            })
        
        # Format course overview
        formatted_course_overview = None
        if course_overview_data:
            formatted_course_overview = {
                "course_name": course.title,
                "total_students": len(students),
                "average_progress": course_overview_data.get("engagement", {}).get("average_completion_rate", 0),
                "total_modules": course_overview_data.get("structure", {}).get("total_modules", 0),
                "total_lessons": course_overview_data.get("structure", {}).get("total_lessons", 0),
                "total_steps": course_overview_data.get("structure", {}).get("total_steps", 0),
                "total_assignments": 0,  # Can add this if needed
                "active_students": len([s for s in formatted_students_data if s["progress_percentage"] > 0]),
                "students_above_50": len([s for s in formatted_students_data if s["progress_percentage"] >= 50]),
                "students_above_80": len([s for s in formatted_students_data if s["progress_percentage"] >= 80]),
                "average_study_time": sum(s["total_study_time"] for s in formatted_students_data) / len(formatted_students_data) if formatted_students_data else 0
            }
        
        # Format groups data
        formatted_groups_data = None
        if groups_data:
            formatted_groups_data = []
            for group_dict in groups_data:
                formatted_groups_data.append({
                    "group_name": group_dict.get("group_name", "N/A"),
                    "student_count": group_dict.get("students_count", 0),
                    "average_progress": group_dict.get("average_completion_percentage", 0),
                    "teacher_name": group_dict.get("teacher_name"),
                    "curator_name": group_dict.get("curator_name"),
                    "active_students": len([s for s in formatted_students_data if any(g in [grp for grp in s["groups"]] for g in [group_dict.get("group_name")])])
                })
        
        # Create Excel file
        excel_service = get_excel_export_service()
        
        excel_buffer = excel_service.create_analytics_workbook(
            course_name=course.title,
            students_data=formatted_students_data,
            course_overview=formatted_course_overview,
            groups_data=formatted_groups_data
        )
        
        # Generate filename
        filename = f"Analytics_{course.title.replace(' ', '_')}{title_suffix.replace(' ', '_')}_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
        
        # Return as streaming response
        return StreamingResponse(
            excel_buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to export to Excel: {str(e)}")

def merge_sat_pair(pair: dict) -> Optional[dict]:
    """Helper to merge mathTest and verbalTest into a single entry for the frontend"""
    m = pair.get("mathTest")
    v = pair.get("verbalTest")
    
    if not m and not v:
        return None
        
    combined = {
        "testId": m.get("testId") if m else v.get("testId") if v else "latest",
        "testName": pair.get("weekPeriod") or (m.get("testName") if m and v else m.get("testName") if m else v.get("testName") if v else "Latest SAT Results"),
        "completedAt": m.get("completedAt") if m else v.get("completedAt") if v else None,
        "questions": [],
        "score": (pair.get("combinedScore") or 0) if "combinedScore" in pair else 0,
        "percentage": 0,
        "correctCount": 0,
        "totalQuestions": 0,
        "math_score": 0,
        "math_total": 0,
        "math_pct": 0,
        "verbal_score": 0,
        "verbal_total": 0,
        "verbal_pct": 0
    }
    
    if m:
        combined["questions"].extend(m.get("questions", []))
        if not combined["score"] and m.get("score"):
            combined["score"] += m["score"]
        
        m_correct = m.get("correctCount") or 0
        m_total = m.get("totalQuestions") or 0
        combined["correctCount"] += m_correct
        combined["totalQuestions"] += m_total
        combined["math_score"] = m_correct
        combined["math_total"] = m_total
        combined["math_pct"] = round((m_correct / m_total * 100), 2) if m_total > 0 else 0
    
    if v:
        combined["questions"].extend(v.get("questions", []))
        if v.get("score"):
            combined["score"] += v["score"]
            
        v_correct = v.get("correctCount") or 0
        v_total = v.get("totalQuestions") or 0
        combined["correctCount"] += v_correct
        combined["totalQuestions"] += v_total
        combined["verbal_score"] = v_correct
        combined["verbal_total"] = v_total
        combined["verbal_pct"] = round((v_correct / v_total * 100), 2) if v_total > 0 else 0
    
    # Calculate aggregate percentage
    if combined["totalQuestions"] > 0:
        combined["percentage"] = round((combined["correctCount"] / combined["totalQuestions"]) * 100, 2)
        
    return combined

@router.get("/student/{student_id}/sat-scores")
async def get_student_sat_scores(
    student_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get SAT scores for a student from external platform"""
    
    # Check permissions
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Verify student exists
    student = db.query(UserInDB).filter(
        UserInDB.id == student_id, 
        UserInDB.role == "student"
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    # Check access rights based on role using centralized permission check
    if current_user.role != "admin" and not check_student_access(student_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this student")
    
    # External API Call
    api_key = os.getenv("MASTEREDU_API_KEY")
    url = f"https://api.mastereducation.kz/api/lms/students/{student.email}/test-results"
    
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json"
    }
    
    logger = logging.getLogger(__name__)
    
    async with httpx.AsyncClient() as client:
        try:
            logger.info(f"Fetching SAT scores for {student.email}")
            response = await client.get(url, headers=headers, timeout=30.0)
            
            if response.status_code == 404:
                return {"testResults": []}
            
            if response.status_code != 200:
                logger.error(f"SAT API Error: {response.status_code} {response.text}")
                # Return empty list on error to avoid breaking the page
                return {"testResults": [], "error": "External API error"}
            
            data = response.json()
            
            # Normalize for frontend if using the new format
            # Can be at top level or in testPairs list
            if ("mathTest" in data or "verbalTest" in data or "testPairs" in data) and "testResults" not in data:
                data["testResults"] = []
                
                # Case 1: Wrapped in testPairs (array of pairs)
                if "testPairs" in data:
                    for pair in data["testPairs"]:
                        combined = merge_sat_pair(pair)
                        if combined:
                            data["testResults"].append(combined)
                
                # Case 2: Top level (single pair)
                elif "mathTest" in data or "verbalTest" in data:
                    combined = merge_sat_pair(data)
                    if combined:
                        data["testResults"].append(combined)
            
            return data
            
        except Exception as e:
            logger.error(f"Error fetching SAT scores: {str(e)}")
            return {"testResults": [], "error": str(e)}

@router.get("/course/{course_id}/progress-history")
async def get_course_progress_history(
    course_id: int,
    group_id: Optional[int] = Query(None),
    range_type: str = Query("all", alias="range"),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Get cumulative progress history for the course (all time).
    Optionally filtered by group.
    """
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    # 1. Determine the set of student IDs to consider
    student_query = db.query(UserInDB.id).filter(UserInDB.role == "student")
    
    if group_id:
        # Filter students by group
        student_query = student_query.join(GroupStudent).filter(GroupStudent.group_id == group_id)
        # Verify user has access to this group if needed (skip for now as check_course_access covers generic access)
    else:
        # Filter students by course enrollment
        student_query = student_query.join(Enrollment).filter(
            Enrollment.course_id == course_id,
            Enrollment.is_active == True
        )
            
    student_ids = [s[0] for s in student_query.all()]
    total_students = len(student_ids)

    if total_students == 0:
        return []

    # 2. Get Total Steps count for the course
    # Count steps in all lessons of all modules of the course
    total_steps = db.query(Step).join(Lesson).join(Module).filter(
        Module.course_id == course_id
    ).count()

    if total_steps == 0:
        return []

    # 3. Get All Completed Step Progress records for these students in this course
    # filtered by 'completed' status and having a 'completed_at' date
    progress_query = db.query(
        func.date(StepProgress.completed_at).label('date'),
        func.count(StepProgress.id).label('completed_count'),
        func.count(func.distinct(StepProgress.user_id)).label('active_students')
    ).filter(
        StepProgress.course_id == course_id,
        StepProgress.step_id != None,
        StepProgress.status == 'completed',
        StepProgress.completed_at != None,
        StepProgress.user_id.in_(student_ids)
    ).group_by(
        func.date(StepProgress.completed_at)
    ).order_by(
        func.date(StepProgress.completed_at)
    )

    daily_stats = progress_query.all()

    # 4. Calculate Cumulative Progress
    # We need to fill in missing gaps if we want a smooth line, 
    # but for "All Time" usually we just take the data points we have.
    # To make it look like a "history" we accumulate the completions.

    history = []
    cumulative_completions = 0
    max_possible_completions = total_students * total_steps

    # If data is sparse, we might want to iterate from start date to end date
    # But for now let's just use the days with activity
    
    for day_stat in daily_stats:
        date_str = str(day_stat.date)
        daily_completions = day_stat.completed_count
        active_students = day_stat.active_students
        
        cumulative_completions += daily_completions
        
        # Calculate percentage of TOTAL course completion (all students * all steps)
        # This represents "How much of the total course volume has been consumed by the group"
        progress_percentage = (cumulative_completions / max_possible_completions) * 100 if max_possible_completions > 0 else 0
        
        history.append({
            "date": date_str,
            "progress": round(progress_percentage, 2),
            "active_students": active_students,
            "daily_completions": daily_completions
        })

    return history

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Query
from sqlalchemy.orm import Session, joinedload, noload
from sqlalchemy import func, desc, and_
from typing import List, Optional
import os
import uuid
from datetime import datetime
import aiofiles
import json

from src.config import get_db
from src.schemas.models import (
    Course, Module, Lesson, Step, LessonMaterial, Enrollment, StudentProgress,
    CourseSchema, CourseCreateSchema, ModuleSchema, ModuleCreateSchema,
    LessonSchema, LessonCreateSchema, StepSchema, StepCreateSchema,
    LessonMaterialSchema, UserInDB, QuizData,
    CourseGroupAccess, CourseGroupAccessSchema, Group, GroupStudent,
    Assignment, AssignmentLinkedLesson,
    LegacyLessonSchema, ManualLessonUnlock,  # Keep for migration period
    CourseTeacherAccess, CourseTeacherAccessSchema
)
from src.routes.auth import get_current_user_dependency
from src.utils.permissions import require_teacher_or_admin, require_admin, check_course_access
from src.utils.course_access import (
    calc_program_week_from_start_date,
    get_special_lesson_cap_for_student_course,
    student_has_nonspecial_group_access_to_course,
    lesson_blocked_by_special_cap,
)
from src.services.azure_openai_service import AzureOpenAIService
from src.services import storage_service
from src.services import video_ingest
from src.services.cache_service import cached
from src.utils.duration_calculator import update_course_duration

router = APIRouter()

# =============================================================================
# COURSE MANAGEMENT
# =============================================================================

@router.get("/", response_model=List[CourseSchema])
@cached(
    namespace="courses:list",
    ttl=120,
    key_args=("skip", "limit", "teacher_id", "is_active"),
)
def get_courses(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, le=1000),
    teacher_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get list of courses based on user role and permissions"""
    # Base query – role-specific filters applied below
    query = db.query(Course)
    
    if current_user.role == "student":
        # Students see courses they are enrolled in OR courses their group has access to
        
        # Get enrolled course IDs
        enrolled_course_ids = db.query(Enrollment.course_id).filter(
            Enrollment.user_id == current_user.id,
            Enrollment.is_active == True
        ).subquery()
        
        # Get group access course IDs
        student_group_ids = db.query(GroupStudent.group_id).filter(
            GroupStudent.student_id == current_user.id
        ).subquery()
        
        group_course_ids = db.query(CourseGroupAccess.course_id).filter(
            CourseGroupAccess.group_id.in_(student_group_ids),
            CourseGroupAccess.is_active == True
        ).subquery()
        
        # Combine both sets of course IDs
        from sqlalchemy import union
        combined_course_ids = db.query(union(
            enrolled_course_ids.select(),
            group_course_ids.select()
        ).alias('course_id')).subquery()
        
        query = query.filter(Course.id.in_(combined_course_ids), Course.is_active == True)
        
    elif current_user.role == "teacher":
        # Teachers see their own courses AND courses their groups have access to AND courses with direct access
        from src.schemas.models import Group, CourseGroupAccess, CourseTeacherAccess
        
        # Get teacher's own courses
        own_courses = db.query(Course.id).filter(Course.teacher_id == current_user.id).subquery()
        
        # Get courses accessible via groups
        teacher_groups = db.query(Group.id).filter(Group.teacher_id == current_user.id).subquery()
        group_courses = db.query(CourseGroupAccess.course_id).filter(
            CourseGroupAccess.group_id.in_(teacher_groups),
            CourseGroupAccess.is_active == True
        ).subquery()
        
        # Get courses with direct access
        direct_access_courses = db.query(CourseTeacherAccess.course_id).filter(
            CourseTeacherAccess.teacher_id == current_user.id,
            CourseTeacherAccess.is_active == True
        ).subquery()
        
        # Combine all
        from sqlalchemy import union
        combined_course_ids = db.query(union(
            own_courses.select(),
            group_courses.select(),
            direct_access_courses.select()
        ).alias('course_id')).subquery()
        
        query = query.filter(Course.id.in_(combined_course_ids))
        
    elif current_user.role == "curator":
        # Curators see courses their groups have access to
        from src.schemas.models import Group, CourseGroupAccess
        
        curator_groups = db.query(Group.id).filter(Group.curator_id == current_user.id).subquery()
        group_courses = db.query(CourseGroupAccess.course_id).filter(
            CourseGroupAccess.group_id.in_(curator_groups),
            CourseGroupAccess.is_active == True
        ).subquery()
        
        query = query.filter(Course.id.in_(group_courses), Course.is_active == True)

    elif current_user.role == "head_teacher":
        from src.courses.models import CourseHeadTeacher

        managed_course_ids = [
            row[0]
            for row in db.query(CourseHeadTeacher.course_id).filter(
                CourseHeadTeacher.head_teacher_id == current_user.id
            ).all()
        ]
        if not managed_course_ids:
            return []
        query = query.filter(Course.id.in_(managed_course_ids))

    # Apply filters
    if teacher_id is not None:
        query = query.filter(Course.teacher_id == teacher_id)
    if is_active is not None:
        query = query.filter(Course.is_active == is_active)
    
    courses = query.offset(skip).limit(limit).all()
    
    if not courses:
        return []

    # Batch fetch teachers
    teacher_ids = list(set(c.teacher_id for c in courses if c.teacher_id))
    teachers = {
        t.id: t.name 
        for t in db.query(UserInDB).filter(UserInDB.id.in_(teacher_ids)).all()
    } if teacher_ids else {}

    # Batch fetch module counts
    course_ids = [c.id for c in courses]
    module_counts = dict(
        db.query(Module.course_id, func.count(Module.id))
        .filter(Module.course_id.in_(course_ids))
        .group_by(Module.course_id)
        .all()
    )
    
    # Enrich with teacher names and module counts
    courses_data = []
    for course in courses:
        course_data = CourseSchema.from_orm(course)
        course_data.teacher_name = teachers.get(course.teacher_id, "Unknown")
        course_data.total_modules = module_counts.get(course.id, 0)
        # Map is_active to user-friendly status for frontend (draft/active)
        course_data.status = 'active' if course.is_active else 'draft'
        courses_data.append(course_data)
    
    return courses_data

@router.get("/my-courses", response_model=List[CourseSchema])
@cached(namespace="courses:my", ttl=120)
def get_my_courses(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get student's enrolled courses with basic info"""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can access this endpoint")
    
    # Get group access course IDs
    student_group_ids = db.query(GroupStudent.group_id).filter(
        GroupStudent.student_id == current_user.id
    ) # .all() will be called later to get values
    
    # Execute to get list of IDs
    student_group_id_list = [g[0] for g in student_group_ids.all()]
    
    group_course_ids = []
    if student_group_id_list:
        group_courses = db.query(CourseGroupAccess.course_id).filter(
            CourseGroupAccess.group_id.in_(student_group_id_list),
            CourseGroupAccess.is_active == True
        ).all()
        group_course_ids = [c[0] for c in group_courses]
    
    # Get enrolled course IDs
    enrolled_courses = db.query(Enrollment.course_id).filter(
        Enrollment.user_id == current_user.id,
        Enrollment.is_active == True
    ).all()
    enrolled_course_ids = [c[0] for c in enrolled_courses]
    
    # Combine both sets of course IDs
    all_course_ids = list(set(group_course_ids + enrolled_course_ids))
    
    if not all_course_ids:
        return []

    courses = db.query(Course).filter(
        Course.id.in_(all_course_ids), 
        Course.is_active == True
    ).all()

    # Batch fetch teachers
    teacher_ids = list(set(c.teacher_id for c in courses if c.teacher_id))
    teachers = {
        t.id: t.name 
        for t in db.query(UserInDB).filter(UserInDB.id.in_(teacher_ids)).all()
    } if teacher_ids else {}

    # Batch fetch module counts
    course_ids = [c.id for c in courses]
    module_counts = dict(
        db.query(Module.course_id, func.count(Module.id))
        .filter(Module.course_id.in_(course_ids))
        .group_by(Module.course_id)
        .all()
    )

    # Enrich with teacher names and module counts
    courses_data = []
    for course in courses:
        course_data = CourseSchema.from_orm(course)
        course_data.teacher_name = teachers.get(course.teacher_id, "Unknown")
        course_data.total_modules = module_counts.get(course.id, 0)
        course_data.status = 'active' if course.is_active else 'draft'
        courses_data.append(course_data)
    
    return courses_data

@router.post("/", response_model=CourseSchema)
def create_course(
    course_data: CourseCreateSchema,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Create new course (teachers and admins only)"""
    # If admin is creating course, allow assigning teacher explicitly
    teacher_id = current_user.id
    if current_user.role == "admin" and getattr(course_data, 'teacher_id', None):
        teacher_id = course_data.teacher_id
    
    new_course = Course(
        title=course_data.title,
        description=course_data.description,
        cover_image_url=course_data.cover_image_url,
        teacher_id=teacher_id,
        estimated_duration_minutes=course_data.estimated_duration_minutes,
        release_schedule=getattr(course_data, "release_schedule", None) or "all",
        course_type=getattr(course_data, "course_type", None) or "general_english",
    )
    
    db.add(new_course)
    db.commit()
    db.refresh(new_course)
    
    # Create response with teacher name
    teacher = db.query(UserInDB).filter(UserInDB.id == teacher_id).first()
    course_response = CourseSchema.from_orm(new_course)
    course_response.teacher_name = teacher.name if teacher else "Unknown"
    course_response.total_modules = 0
    course_response.status = 'active' if new_course.is_active else 'draft'
    
    return course_response

@router.get("/{course_id}", response_model=CourseSchema)
@cached(namespace="courses:detail", ttl=300, key_args=("course_id",))
def get_course(
    course_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get course details"""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Check access permissions
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")
    
    # Get teacher info and module count
    teacher = db.query(UserInDB).filter(UserInDB.id == course.teacher_id).first()
    module_count = db.query(Module).filter(Module.course_id == course.id).count()
    
    # Auto-recalculate duration if it's 0 or not set
    if course.estimated_duration_minutes == 0:
        update_course_duration(course.id, db)
        db.refresh(course)
    
    course_response = CourseSchema.from_orm(course)
    course_response.teacher_name = teacher.name if teacher else "Unknown"
    course_response.total_modules = module_count
    course_response.status = 'active' if course.is_active else 'draft'
    
    return course_response

@router.put("/{course_id}", response_model=CourseSchema)
def update_course(
    course_id: int,
    course_data: CourseCreateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Update course (only course teacher or admin)"""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Check permissions
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Update course fields
    course.title = course_data.title
    course.description = course_data.description
    course.cover_image_url = course_data.cover_image_url
    course.estimated_duration_minutes = course_data.estimated_duration_minutes
    if hasattr(course_data, "release_schedule") and course_data.release_schedule:
        course.release_schedule = course_data.release_schedule
    if hasattr(course_data, "course_type") and course_data.course_type:
        course.course_type = course_data.course_type

    db.commit()
    db.refresh(course)
    
    # Return with teacher info
    teacher = db.query(UserInDB).filter(UserInDB.id == course.teacher_id).first()
    module_count = db.query(Module).filter(Module.course_id == course.id).count()
    
    course_response = CourseSchema.from_orm(course)
    course_response.teacher_name = teacher.name if teacher else "Unknown"
    course_response.total_modules = module_count
    
    return course_response

@router.post("/{course_id}/recalculate-duration")
def recalculate_course_duration(
    course_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Recalculate and update course duration based on all steps"""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Check permissions
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Calculate and update duration
    new_duration = update_course_duration(course_id, db)
    
    return {
        "detail": "Course duration recalculated successfully",
        "course_id": course_id,
        "estimated_duration_minutes": new_duration
    }

@router.post("/{course_id}/publish")
def publish_course(
    course_id: int,
    current_user: UserInDB = Depends(require_admin()),
    db: Session = Depends(get_db)
):
    """Publish course from draft to active (admin only)"""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Check if course is already active
    if course.is_active:
        raise HTTPException(status_code=400, detail="Course is already published")
    
    # Publish the course
    course.is_active = True
    course.updated_at = datetime.utcnow()
    
    db.commit()
    db.refresh(course)
    
    return {
        "detail": "Course published successfully",
        "course_id": course.id,
        "is_active": course.is_active,
        "status": "active"
    }

@router.post("/{course_id}/unpublish")
def unpublish_course(
    course_id: int,
    current_user: UserInDB = Depends(require_admin()),
    db: Session = Depends(get_db)
):
    """Unpublish course from active to draft (admin only)"""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Check if course is already in draft
    if not course.is_active:
        raise HTTPException(status_code=400, detail="Course is already in draft")
    
    # Unpublish the course
    course.is_active = False
    course.updated_at = datetime.utcnow()
    
    db.commit()
    db.refresh(course)
    
    return {
        "detail": "Course unpublished successfully",
        "course_id": course.id,
        "is_active": course.is_active,
        "status": "draft"
    }

@router.delete("/{course_id}")
def delete_course(
    course_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Delete course (only course teacher or admin)"""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Check permissions
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Hard delete - completely remove the course
    # SQLAlchemy will handle cascade deletes for related records (modules, lessons, steps, etc.)
    db.delete(course)
    db.commit()
    
    return {"detail": "Course deleted successfully"}

# =============================================================================
# MODULE MANAGEMENT
# =============================================================================

@router.get("/{course_id}/modules", response_model=List[ModuleSchema])
@cached(
    namespace="courses:modules",
    ttl=60,
    key_args=("course_id", "include_lessons", "student_id"),
)
def get_course_modules(
    course_id: int,
    include_lessons: bool = Query(False, description="Include lessons for each module"),
    student_id: Optional[int] = Query(None, description="Get progress for specific student (teacher/admin only)"),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get all modules for a course"""
    # Check course access
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")

    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    target_user_id = current_user.id
    if student_id:
        if current_user.role not in ["teacher", "admin", "head_curator", "head_teacher"]:
            raise HTTPException(status_code=403, detail="Only teachers, admins, and head curators can view other students' progress")
        target_user_id = student_id

    # Weekly release: modules unlocked by week based on group start_date
    unlocked_module_ids: Optional[set] = None
    if course.release_schedule == "weekly" and (current_user.role == "student" or student_id):
        student_group_ids = db.query(GroupStudent.group_id).filter(
            GroupStudent.student_id == target_user_id
        ).all()
        student_group_ids = [g[0] for g in student_group_ids]
        groups_with_course = []
        if student_group_ids:
            groups_with_course = [
                r[0] for r in db.query(CourseGroupAccess.group_id).filter(
                    CourseGroupAccess.course_id == course_id,
                    CourseGroupAccess.group_id.in_(student_group_ids),
                    CourseGroupAccess.is_active == True
                ).all()
            ]
        current_program_week: Optional[int] = None
        for gid in groups_with_course:
            group = db.query(Group).filter(Group.id == gid).first()
            if group and group.schedule_config:
                w = calc_program_week_from_start_date(group.schedule_config.get("start_date"))
                if w is not None and (current_program_week is None or w > current_program_week):
                    current_program_week = w
        if current_program_week is not None:
            unlocked_module_ids = set()
            mods = db.query(Module).filter(Module.course_id == course_id).all()
            for m in mods:
                module_week = m.week_number if m.week_number is not None else (m.order_index + 1)
                if module_week <= current_program_week:
                    unlocked_module_ids.add(m.id)

    # Get modules with lesson counts in a single query using subquery
    from sqlalchemy import func
    
    lesson_counts = db.query(
        Lesson.module_id,
        func.count(Lesson.id).label('lesson_count')
    ).filter(
        Lesson.module_id.in_(
            db.query(Module.id).filter(Module.course_id == course_id)
        )
    ).group_by(Lesson.module_id).all()
    
    # Create a map of module_id to lesson_count
    lesson_count_map = {module_id: count for module_id, count in lesson_counts}
    
    # Fetch progress
    completed_step_ids = set()
    completed_lesson_ids = set()
    
    # Only fetch progress if target user is a student (or we are viewing as student)
    # We check if target_user_id corresponds to a student role, or just fetch if requested
    # For simplicity, if student_id is passed, we assume we want progress.
    # If current_user is student, we always fetch their progress.
    
    should_fetch_progress = False
    if current_user.role == "student":
        should_fetch_progress = True
    elif student_id:
        should_fetch_progress = True
        
    if should_fetch_progress:
        # Get completed steps
        from src.schemas.models import StepProgress
        completed_steps = db.query(StepProgress.step_id).filter(
            StepProgress.user_id == target_user_id,
            StepProgress.course_id == course_id,
            StepProgress.status == "completed"
        ).all()
        completed_step_ids = {s[0] for s in completed_steps}
        
        # Get completed lessons (from StudentProgress)
        completed_lessons = db.query(StudentProgress.lesson_id).filter(
            StudentProgress.user_id == target_user_id,
            StudentProgress.course_id == course_id,
            StudentProgress.status == "completed",
            StudentProgress.lesson_id.isnot(None)
        ).all()
        completed_lesson_ids = {l[0] for l in completed_lessons}
    
    # Use joinedload to control lesson loading
    
    if include_lessons:
        # Optimization: Don't join steps here to avoid fetching heavy content (text, video_url)
        # We will fetch lightweight step data separately
        modules = db.query(Module).options(
            joinedload(Module.lessons)
        ).filter(
            Module.course_id == course_id
        ).order_by(Module.order_index).all()
    else:
        modules = db.query(Module).filter(
            Module.course_id == course_id
        ).order_by(Module.order_index).all()
    
    # Optimization: Pre-fetch steps and calculate redirects if needed
    steps_by_lesson = {}
    unlocked_by_redirect_ids = set()
    unlocked_by_assignment_ids = set()
    manually_unlocked_lesson_ids = set()

    special_cap: Optional[int] = None
    if should_fetch_progress:
        special_cap = get_special_lesson_cap_for_student_course(target_user_id, course_id, db)

    if include_lessons and modules:
        # Fetch all steps for these lessons in one lightweight query
        all_lesson_ids = [l.id for m in modules for l in m.lessons]
        
        if all_lesson_ids:
            lightweight_steps = db.query(
                Step.id, 
                Step.lesson_id, 
                Step.title, 
                Step.content_type, 
                Step.order_index,
                Step.created_at,
                Step.is_optional,
            ).filter(
                Step.lesson_id.in_(all_lesson_ids)
            ).all()
            
            for s in lightweight_steps:
                if s.lesson_id not in steps_by_lesson:
                    steps_by_lesson[s.lesson_id] = []
                steps_by_lesson[s.lesson_id].append(s)

        if should_fetch_progress:
            # Get active assignments for the target student's groups that are linked to lessons
            from src.schemas.models import AssignmentLinkedLesson
            student_group_ids = db.query(GroupStudent.group_id).filter(
                GroupStudent.student_id == target_user_id
            ).subquery()

            if student_has_nonspecial_group_access_to_course(target_user_id, course_id, db):
                assigned_lesson_ids = db.query(AssignmentLinkedLesson.lesson_id).join(
                    Assignment, Assignment.id == AssignmentLinkedLesson.assignment_id
                ).filter(
                    Assignment.group_id.in_(student_group_ids),
                    Assignment.is_active == True,
                    (Assignment.is_hidden == False) | (Assignment.is_hidden == None)
                ).all()
                unlocked_by_assignment_ids = {a[0] for a in assigned_lesson_ids}
            else:
                unlocked_by_assignment_ids = set()

            # Get manual unlocks for the user (individual and group-level)
            manual_unlocks = db.query(ManualLessonUnlock.lesson_id).filter(
                (ManualLessonUnlock.user_id == target_user_id) |
                (ManualLessonUnlock.group_id.in_(student_group_ids))
            ).all()
            manually_unlocked_lesson_ids = {m[0] for m in manual_unlocks}
            
            # If current user is student, calculate redirects
            # (Redirects logic depends on whether we are calculating for students)
            for mod in modules:
                for les in mod.lessons:
                    if les.next_lesson_id:
                        is_completed_db = les.id in completed_lesson_ids
                        
                        # Check steps completion — optional steps excluded
                        lesson_steps = steps_by_lesson.get(les.id, [])
                        required = [s for s in lesson_steps if not getattr(s, 'is_optional', False)]
                        check = required if required else lesson_steps
                        all_steps_done = check and all(s.id in completed_step_ids for s in check)
                        
                        if is_completed_db or all_steps_done:
                            unlocked_by_redirect_ids.add(les.next_lesson_id)

    # Enrich with lesson counts
    modules_data = []
    for module in modules:
        # Create module data without lessons first
        module_dict = {
            "id": module.id,
            "course_id": module.course_id,
            "title": module.title,
            "description": module.description,
            "order_index": module.order_index,
            "total_lessons": lesson_count_map.get(module.id, 0),
            "created_at": module.created_at,
            "week_number": getattr(module, "week_number", None),
        }
        
        # Include lessons if requested
        if include_lessons:
            # Use already loaded lessons from joinedload
            lessons = sorted(module.lessons, key=lambda x: x.order_index)
            
            lessons_data = []
            for lesson_idx, lesson in enumerate(lessons):
                # Get steps for this lesson from our lightweight map
                # Note: s is a Row/KeyedTuple, not an ORM object, so we access by index or name
                raw_steps = steps_by_lesson.get(lesson.id, [])
                steps = sorted(raw_steps, key=lambda x: x.order_index)
                
                lesson_schema = LessonSchema.from_orm(lesson)
                lesson_schema.steps = []
                
                # Check if all REQUIRED (non-optional) steps are completed
                required_steps = [s for s in steps if not getattr(s, 'is_optional', False)]
                check_steps = required_steps if required_steps else steps
                all_steps_completed = True if check_steps else False
                
                for step in steps:
                    # Manually construct StepSchema from lightweight data
                    # We set heavy fields to None
                    step_schema = StepSchema(
                        id=step.id,
                        lesson_id=step.lesson_id,
                        title=step.title,
                        content_type=step.content_type,
                        order_index=step.order_index,
                        created_at=step.created_at,
                        video_url=None,
                        content_text=None,
                        original_image_url=None,
                        attachments=None,
                        is_completed=step.id in completed_step_ids
                    )
                    
                    lesson_schema.steps.append(step_schema)
                    
                    # Optional steps don't block lesson completion
                    if not getattr(step, 'is_optional', False) and not step_schema.is_completed:
                        all_steps_completed = False
                
                lesson_schema.total_steps = len(steps)
                
                # Determine lesson completion
                if lesson.id in completed_lesson_ids:
                    lesson_schema.is_completed = True
                elif steps and all_steps_completed:
                    lesson_schema.is_completed = True
                else:
                    lesson_schema.is_completed = False
                
                # Convert to dict early to add is_accessible field
                lesson_dict = lesson_schema.model_dump()
                
                # SEQUENTIAL ACCESS LOGIC: Determine if lesson is accessible
                # If viewing for a specific student OR current user is a student
                if current_user.role == "student" or student_id:
                    # Check if lesson is marked as initially unlocked by admin
                    if lesson.is_initially_unlocked:
                        lesson_dict["is_accessible"] = True
                    # Check if explicitly unlocked by redirect, assignment, or manual
                    elif (lesson.id in unlocked_by_redirect_ids or 
                          lesson.id in unlocked_by_assignment_ids or
                          lesson.id in manually_unlocked_lesson_ids):
                        lesson_dict["is_accessible"] = True
                    # Weekly release: if module is locked by schedule, lesson is not accessible
                    elif unlocked_module_ids is not None and module.id not in unlocked_module_ids:
                        lesson_dict["is_accessible"] = False
                    elif special_cap is not None and lesson_idx >= special_cap:
                        lesson_dict["is_accessible"] = False
                    # First lesson is always accessible
                    elif lesson_idx == 0:
                        # Check if this is the first module
                        is_first_module = module.order_index == min(m.order_index for m in modules)
                        lesson_dict["is_accessible"] = is_first_module or len(lessons_data) > 0
                        
                        # If not first module, check if previous module is completed
                        if not is_first_module:
                            # Get previous module
                            prev_modules = [m for m in modules if m.order_index < module.order_index]
                            if prev_modules:
                                prev_module = max(prev_modules, key=lambda m: m.order_index)
                                prev_module_lessons = sorted(prev_module.lessons, key=lambda x: x.order_index)
                                
                                # Check if all lessons in previous module are completed
                                prev_module_completed = all(
                                    l.id in completed_lesson_ids or 
                                    (l.steps and all(s.id in completed_step_ids for s in l.steps))
                                    for l in prev_module_lessons
                                )
                                lesson_dict["is_accessible"] = prev_module_completed
                            else:
                                lesson_dict["is_accessible"] = True
                    else:
                        # For non-first lessons, check if previous lesson is completed
                        previous_lesson_dict = lessons_data[lesson_idx - 1]
                        is_prev_completed = previous_lesson_dict.get("is_completed", False)
                        
                        # CRITICAL FIX: If previous lesson has a next_lesson_id that points elsewhere,
                        # DO NOT unlock this lesson linearly.
                        prev_next_id = previous_lesson_dict.get("next_lesson_id")
                        if prev_next_id and prev_next_id != lesson.id:
                            lesson_dict["is_accessible"] = False
                        else:
                            lesson_dict["is_accessible"] = is_prev_completed
                else:
                    # Teachers and admins can access all lessons
                    lesson_dict["is_accessible"] = True
                
                lessons_data.append(lesson_dict)
            
            # Add lessons to module data
            module_dict["lessons"] = lessons_data
            
            # Check if all lessons in module are completed
            if lessons_data and all(l["is_completed"] for l in lessons_data):
                module_dict["is_completed"] = True
            else:
                module_dict["is_completed"] = False
        
        modules_data.append(module_dict)
    
    return modules_data

@router.post("/{course_id}/modules", response_model=ModuleSchema)
def create_module(
    course_id: int,
    module_data: ModuleCreateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Create new module in course"""
    # Check course exists and permissions
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    new_module = Module(
        course_id=course_id,
        title=module_data.title,
        description=module_data.description,
        order_index=module_data.order_index,
        week_number=getattr(module_data, "week_number", None)
    )
    
    db.add(new_module)
    db.commit()
    db.refresh(new_module)
    
    module_response = ModuleSchema.from_orm(new_module)
    module_response.total_lessons = 0
    
    return module_response

@router.put("/{course_id}/modules/{module_id}", response_model=ModuleSchema)
def update_module(
    course_id: int,
    module_id: int,
    module_data: ModuleCreateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Update module"""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    module = db.query(Module).filter(
        Module.id == module_id,
        Module.course_id == course_id
    ).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    # Check permissions
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    module.title = module_data.title
    module.description = module_data.description
    module.order_index = module_data.order_index
    if hasattr(module_data, "week_number"):
        module.week_number = module_data.week_number

    db.commit()
    db.refresh(module)
    
    lesson_count = db.query(Lesson).filter(Lesson.module_id == module.id).count()
    module_response = ModuleSchema.from_orm(module)
    module_response.total_lessons = lesson_count
    
    return module_response

@router.delete("/{course_id}/modules/{module_id}")
def delete_module(
    course_id: int,
    module_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Delete module and all its lessons"""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    module = db.query(Module).filter(
        Module.id == module_id,
        Module.course_id == course_id
    ).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    # Check permissions
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get all lessons in this module
    lessons = db.query(Lesson).filter(Lesson.module_id == module_id).all()
    lesson_ids = [lesson.id for lesson in lessons]
    
    if lesson_ids:
        # Remove references from other lessons' next_lesson_id
        lessons_pointing_to_module_lessons = db.query(Lesson).filter(
            Lesson.next_lesson_id.in_(lesson_ids)
        ).all()
        
        for pointing_lesson in lessons_pointing_to_module_lessons:
            pointing_lesson.next_lesson_id = None
        
        # Get all steps for all lessons
        steps = db.query(Step).filter(Step.lesson_id.in_(lesson_ids)).all()
        step_ids = [step.id for step in steps]
        
        # Delete step progress records
        from src.schemas.models import AssignmentLinkedLesson, StepProgress
        if step_ids:
            step_progress_records = db.query(StepProgress).filter(
                StepProgress.step_id.in_(step_ids)
            ).all()
            for record in step_progress_records:
                db.delete(record)
        
        # Delete assignment links
        assignment_links = db.query(AssignmentLinkedLesson).filter(
            AssignmentLinkedLesson.lesson_id.in_(lesson_ids)
        ).all()
        for link in assignment_links:
            db.delete(link)
        
        # Delete student progress records
        student_progress_records = db.query(StudentProgress).filter(
            StudentProgress.lesson_id.in_(lesson_ids)
        ).all()
        for record in student_progress_records:
            db.delete(record)
    
    # Now delete the module (will cascade delete lessons and steps)
    db.delete(module)
    db.commit()
    
    return {"detail": "Module deleted successfully"}

# =============================================================================
# LESSON MANAGEMENT
# =============================================================================

@router.get("/{course_id}/modules/{module_id}/lessons", response_model=List[LessonSchema])
@cached(namespace="courses:module-lessons", ttl=120, key_args=("course_id", "module_id"))
def get_module_lessons(
    course_id: int,
    module_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get all lessons for a module"""
    # Check course access
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")
    
    # Get lessons with steps
    
    lessons = db.query(Lesson).options(
        joinedload(Lesson.steps)
    ).filter(
        Lesson.module_id == module_id
    ).order_by(Lesson.order_index).all()
    
    lessons_data = []
    for lesson in lessons:
        # Get steps for this lesson
        steps = sorted(lesson.steps, key=lambda x: x.order_index) if lesson.steps else []
        
        lesson_schema = LessonSchema.from_orm(lesson)
        lesson_schema.steps = [StepSchema.from_orm(step) for step in steps]
        lesson_schema.total_steps = len(steps)
        
        lessons_data.append(lesson_schema)
    
    return lessons_data

@router.get("/{course_id}/lessons", response_model=List[LessonSchema])
@cached(
    namespace="courses:lessons-list",
    ttl=120,
    key_args=("course_id", "lightweight"),
)
def get_course_lessons(
    course_id: int,
    lightweight: bool = False,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get all lessons for a course with module information.
    
    Set lightweight=true to skip loading steps (faster for dropdowns/selectors).
    """
    # Check course access
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")
    
    # Get all lessons for the course with module information
    query = db.query(Lesson).join(Module).filter(
        Module.course_id == course_id
    )
    
    if not lightweight:
        query = query.options(joinedload(Lesson.steps))
    
    lessons = query.order_by(Module.order_index, Lesson.order_index).all()
    
    lessons_data = []
    for lesson in lessons:
        lesson_schema = LessonSchema.from_orm(lesson)
        
        if not lightweight:
            # Get steps for this lesson
            steps = sorted(lesson.steps, key=lambda x: x.order_index) if lesson.steps else []
            lesson_schema.steps = [StepSchema.from_orm(step) for step in steps]
            lesson_schema.total_steps = len(steps)
        else:
            lesson_schema.steps = []
            lesson_schema.total_steps = 0
        
        lessons_data.append(lesson_schema)
    
    return lessons_data



@router.post("/{course_id}/modules/{module_id}/lessons", response_model=LessonSchema)
def create_lesson(
    course_id: int,
    module_id: int,
    lesson_data: LessonCreateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Create new lesson in module"""
    # Check course and module exist
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    module = db.query(Module).filter(
        Module.id == module_id,
        Module.course_id == course_id
    ).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    # Check permissions
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Calculate order_index if not provided or if it's 0
    if lesson_data.order_index == 0:
        # Get the highest order_index in this module and add 1
        max_order = db.query(func.max(Lesson.order_index)).filter(
            Lesson.module_id == module_id
        ).scalar() or 0
        calculated_order_index = max_order + 1
    else:
        calculated_order_index = lesson_data.order_index
    
    new_lesson = Lesson(
        module_id=module_id,
        title=lesson_data.title,
        description=lesson_data.description,
        duration_minutes=lesson_data.duration_minutes,
        order_index=calculated_order_index,
        next_lesson_id=lesson_data.next_lesson_id,
        is_initially_unlocked=lesson_data.is_initially_unlocked
    )
    
    db.add(new_lesson)
    db.commit()
    db.refresh(new_lesson)
    
    lesson_schema = LessonSchema.from_orm(new_lesson)
    lesson_schema.steps = []
    lesson_schema.total_steps = 0
    
    return lesson_schema

@router.get("/lessons/{lesson_id}", response_model=LessonSchema)
@cached(namespace="courses:lesson", ttl=300, key_args=("lesson_id",))
def get_lesson(
    lesson_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get lesson details with course access check"""
    
    lesson = db.query(Lesson).options(
        noload(Lesson.steps)
    ).filter(Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    # Get course_id through module
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    # Check course access
    if not check_course_access(module.course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this lesson")
    
    # Efficiently count steps without loading them
    total_steps = db.query(Step).filter(Step.lesson_id == lesson_id).count()
    
    lesson_schema = LessonSchema.from_orm(lesson)
    lesson_schema.course_id = module.course_id
    lesson_schema.steps = []
    lesson_schema.total_steps = total_steps
    
    return lesson_schema



@router.get("/lessons/{lesson_id}/check-access")
def check_lesson_access(
    lesson_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Check if current user can access a specific lesson (for students - sequential access)"""
    
    lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    # Get module and course
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    course_id = module.course_id
    course = db.query(Course).filter(Course.id == course_id).first()

    # Check basic course access
    if not check_course_access(course_id, current_user, db):
        return {
            "accessible": False,
            "reason": "You do not have access to this course"
        }
    
    # Teachers and admins can access any lesson
    if current_user.role != "student":
        return {"accessible": True}

    student_group_ids_list = [r[0] for r in db.query(GroupStudent.group_id).filter(
        GroupStudent.student_id == current_user.id
    ).all()]
    student_group_ids_subquery = db.query(GroupStudent.group_id).filter(
        GroupStudent.student_id == current_user.id
    ).subquery()
    
    # Check if lesson is marked as initially unlocked by admin
    if lesson.is_initially_unlocked:
        return {"accessible": True}
    
    # Check if this lesson is assigned as homework (priority access) - optimized lookup
    from src.schemas.models import StudentProgress, StepProgress, Assignment, AssignmentLinkedLesson

    if student_has_nonspecial_group_access_to_course(current_user.id, course_id, db):
        is_assigned = db.query(AssignmentLinkedLesson).join(
            Assignment, Assignment.id == AssignmentLinkedLesson.assignment_id
        ).filter(
            AssignmentLinkedLesson.lesson_id == lesson_id,
            Assignment.group_id.in_(student_group_ids_subquery),
            Assignment.is_active == True,
            (Assignment.is_hidden == False) | (Assignment.is_hidden == None)
        ).first()

        if is_assigned:
            print(f"DEBUG: Lesson {lesson_id} reached via active assignment")
            return {"accessible": True}
    
    # Check if manually unlocked by teacher/admin
    is_manually_unlocked = db.query(ManualLessonUnlock).filter(
        ManualLessonUnlock.lesson_id == lesson_id,
        (ManualLessonUnlock.user_id == current_user.id) |
        (ManualLessonUnlock.group_id.in_(student_group_ids_subquery))
    ).first()

    if is_manually_unlocked:
        print(f"DEBUG: Lesson {lesson_id} reached via manual unlock")
        return {"accessible": True}

    # Weekly release: if course uses weekly schedule and module is locked, deny access
    if course and course.release_schedule == "weekly":
        current_program_week = None
        if student_group_ids_list:
            groups_with_course = [r[0] for r in db.query(CourseGroupAccess.group_id).filter(
                CourseGroupAccess.course_id == course_id,
                CourseGroupAccess.group_id.in_(student_group_ids_list),
                CourseGroupAccess.is_active == True
            ).all()]
            for gid in groups_with_course:
                group = db.query(Group).filter(Group.id == gid).first()
                if group and group.schedule_config:
                    w = calc_program_week_from_start_date(group.schedule_config.get("start_date"))
                    if w is not None and (current_program_week is None or w > current_program_week):
                        current_program_week = w
        if current_program_week is not None:
            module_week = module.week_number if getattr(module, "week_number", None) is not None else (module.order_index + 1)
            if module_week > current_program_week:
                return {
                    "accessible": False,
                    "reason": f"This module opens in week {module_week}. Current week: {current_program_week}."
                }

    cap_blocked, cap_reason = lesson_blocked_by_special_cap(
        current_user.id, course_id, lesson_id, db
    )
    if cap_blocked:
        return {
            "accessible": False,
            "reason": cap_reason or "Lesson not available for your group",
        }

    # First, find ALL lessons that redirect to this one
    redirect_sources = db.query(Lesson).filter(Lesson.next_lesson_id == lesson_id).all()
    
    if redirect_sources:
        # Check if ANY redirect source is completed
        for source in redirect_sources:
            # Check StudentProgress
            sp = db.query(StudentProgress).filter(
                StudentProgress.user_id == current_user.id,
                StudentProgress.lesson_id == source.id,
                StudentProgress.status == 'completed'
            ).first()
            
            if sp:
                print(f"DEBUG: Lesson {lesson_id} unlocked by redirect from completed lesson {source.id} ({source.title}) via StudentProgress")
                return {"accessible": True}
                
            # Check steps completion
            source_steps = db.query(Step).filter(Step.lesson_id == source.id).all()
            
            # Get completed steps for this student
            completed_steps_count = db.query(StepProgress).filter(
                StepProgress.user_id == current_user.id,
                StepProgress.step_id.in_([s.id for s in source_steps]),
                StepProgress.status == "completed"
            ).count()
            
            if source_steps and completed_steps_count == len(source_steps):
                print(f"DEBUG: Lesson {lesson_id} unlocked by redirect from lesson {source.id} ({source.title}) - all steps completed")
                return {"accessible": True}
    else:
        print(f"DEBUG: No lesson explicitly redirects to {lesson_id}")
    
    # Get all completed steps for this student in this course
    completed_steps = db.query(StepProgress.step_id).filter(
        StepProgress.user_id == current_user.id,
        StepProgress.course_id == course_id,
        StepProgress.status == "completed"
    ).all()
    completed_step_ids = {s[0] for s in completed_steps}
    
    # Check if target lesson is already completed (allow re-visiting)
    # Check 1: StudentProgress
    is_already_completed = db.query(StudentProgress).filter(
        StudentProgress.user_id == current_user.id,
        StudentProgress.lesson_id == lesson_id,
        StudentProgress.status == 'completed'
    ).first()
    
    if is_already_completed:
        return {"accessible": True}

    # Check 2: All steps completed
    target_lesson_steps = db.query(Step).filter(Step.lesson_id == lesson_id).all()
    if target_lesson_steps and all(s.id in completed_step_ids for s in target_lesson_steps):
         return {"accessible": True}

    print(f"DEBUG: Checking access for lesson {lesson_id} ({lesson.title})")
    print(f"DEBUG: Module {module.id} ({module.title})")
    
    # Get all modules in this course
    all_modules = db.query(Module).filter(
        Module.course_id == course_id
    ).order_by(Module.order_index).all()
    
    # Find current module and lesson position
    current_module_idx = None
    current_lesson_idx = None
    
    for mod_idx, mod in enumerate(all_modules):
        module_lessons = sorted(mod.lessons, key=lambda x: x.order_index)
        for les_idx, les in enumerate(module_lessons):
            if les.id == lesson_id:
                current_module_idx = mod_idx
                current_lesson_idx = les_idx
                print(f"DEBUG: Found target lesson at module idx {mod_idx}, lesson idx {les_idx}")
                break
        if current_module_idx is not None:
            break
    
    # First lesson in first module is always accessible
    if current_module_idx == 0 and current_lesson_idx == 0:
        return {"accessible": True}
    
    # Check if previous lesson is completed
    if current_lesson_idx > 0:
        # Previous lesson in same module
        prev_lesson = sorted(module.lessons, key=lambda x: x.order_index)[current_lesson_idx - 1]
        prev_lesson_steps = db.query(Step).filter(Step.lesson_id == prev_lesson.id).all()
        
        # Check if previous lesson is completed
        if prev_lesson_steps:
            all_prev_steps_completed = all(s.id in completed_step_ids for s in prev_lesson_steps)
            if not all_prev_steps_completed:
                print(f"DEBUG: Blocking access. Prev lesson {prev_lesson.id} ({prev_lesson.title}) not completed")
                return {
                    "accessible": False,
                    "reason": f"Please complete the previous lesson: {prev_lesson.title} (Module {module.id}, Index {current_lesson_idx})"
                }
        
        # CRITICAL FIX: If previous lesson has a next_lesson_id that points elsewhere,
        # DO NOT unlock this lesson linearly.
        if prev_lesson.next_lesson_id and prev_lesson.next_lesson_id != lesson_id:
            print(f"DEBUG: Blocking access. Prev lesson {prev_lesson.id} redirects to {prev_lesson.next_lesson_id}, not {lesson_id}")
            return {
                "accessible": False,
                "reason": "This lesson is not in the sequential path."
            }
        
        return {"accessible": True}
    
    # First lesson in non-first module - check if previous module is completed
    if current_module_idx > 0:
        prev_module = all_modules[current_module_idx - 1]
        prev_module_lessons = sorted(prev_module.lessons, key=lambda x: x.order_index)
        
        # Check if all lessons in previous module are completed (optional steps excluded)
        for prev_lesson in prev_module_lessons:
            prev_lesson_steps = db.query(Step).filter(Step.lesson_id == prev_lesson.id).all()
            if prev_lesson_steps:
                required = [s for s in prev_lesson_steps if not s.is_optional]
                check = required if required else prev_lesson_steps
                all_steps_completed = all(s.id in completed_step_ids for s in check)
                if not all_steps_completed:
                    return {
                        "accessible": False,
                        "reason": f"Please complete all lessons in module: {prev_module.title}"
                    }
        
        return {"accessible": True}
    
    # async default to accessible (shouldn't reach here)
    return {"accessible": True}


@router.put("/lessons/{lesson_id}", response_model=LessonSchema)
def update_lesson(
    lesson_id: int,
    lesson_data: LessonCreateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Update lesson"""
    
    lesson = db.query(Lesson).options(
        joinedload(Lesson.steps)
    ).filter(Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    # Get course through module and check permissions
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found for this lesson")
    
    course = db.query(Course).filter(Course.id == module.course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found for this lesson")
    
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Update lesson fields
    lesson.title = lesson_data.title
    lesson.description = lesson_data.description
    lesson.duration_minutes = lesson_data.duration_minutes
    # Update explicit next lesson if provided (can be None)
    lesson.next_lesson_id = lesson_data.next_lesson_id
    # Always update order_index (can be 0 for first item)
    lesson.order_index = lesson_data.order_index
    # Update is_initially_unlocked flag
    lesson.is_initially_unlocked = lesson_data.is_initially_unlocked
    
    db.commit()
    db.refresh(lesson)
    
    # Get steps for this lesson
    steps = sorted(lesson.steps, key=lambda x: x.order_index) if lesson.steps else []
    
    lesson_schema = LessonSchema.from_orm(lesson)
    lesson_schema.steps = [StepSchema.from_orm(step) for step in steps]
    lesson_schema.total_steps = len(steps)
    
    return lesson_schema

@router.delete("/lessons/{lesson_id}")
def delete_lesson(
    lesson_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Delete lesson"""
    lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    # Get course through module and check permissions
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found for this lesson")
    
    course = db.query(Course).filter(Course.id == module.course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found for this lesson")
    
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # CRITICAL: Remove all references to this lesson from other lessons' next_lesson_id
    # This prevents foreign key constraint violation
    lessons_pointing_to_this = db.query(Lesson).filter(
        Lesson.next_lesson_id == lesson_id
    ).all()
    
    for pointing_lesson in lessons_pointing_to_this:
        pointing_lesson.next_lesson_id = None
    
    # Get all steps for this lesson
    steps = db.query(Step).filter(Step.lesson_id == lesson_id).all()
    step_ids = [step.id for step in steps]
    
    # Delete step progress records FIRST (before deleting steps)
    from src.schemas.models import AssignmentLinkedLesson, StepProgress
    if step_ids:
        step_progress_records = db.query(StepProgress).filter(
            StepProgress.step_id.in_(step_ids)
        ).all()
        for record in step_progress_records:
            db.delete(record)
    
    # Delete related assignment links
    assignment_links = db.query(AssignmentLinkedLesson).filter(
        AssignmentLinkedLesson.lesson_id == lesson_id
    ).all()
    for link in assignment_links:
        db.delete(link)
    
    # Delete related student progress records
    student_progress_records = db.query(StudentProgress).filter(
        StudentProgress.lesson_id == lesson_id
    ).all()
    for record in student_progress_records:
        db.delete(record)
    
    # Now delete the lesson (will cascade delete steps since we already deleted step_progress)
    db.delete(lesson)
    db.commit()
    
    return {"detail": "Lesson deleted successfully"}

# =============================================================================
# STEP MANAGEMENT
# =============================================================================

@router.get("/lessons/{lesson_id}/steps", response_model=List[StepSchema])
@cached(namespace="courses:lesson-steps", ttl=300, key_args=("lesson_id", "include_content"))
def get_lesson_steps(
    lesson_id: int,
    include_content: bool = Query(True, description="Include full step content (text, video, attachments)"),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get all steps for a lesson"""
    lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    # Get course through module and check access
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found for this lesson")
    
    if not check_course_access(module.course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this lesson")
    
    if include_content:
        steps = db.query(Step).filter(
            Step.lesson_id == lesson_id
        ).order_by(Step.order_index).all()
        return [StepSchema.from_orm(step) for step in steps]
    else:
        # Lightweight query - exclude heavy text fields
        steps_data = db.query(
            Step.id,
            Step.lesson_id,
            Step.title,
            Step.content_type,
            Step.order_index,
            Step.created_at,
            Step.is_optional,
        ).filter(
            Step.lesson_id == lesson_id
        ).order_by(Step.order_index).all()
        
        # Manually construct StepSchema with None for heavy fields
        return [
            StepSchema(
                id=s.id,
                lesson_id=s.lesson_id,
                title=s.title,
                content_type=s.content_type,
                order_index=s.order_index,
                created_at=s.created_at,
                is_optional=bool(s.is_optional),
                video_url=None,
                content_text=None,
                original_image_url=None,
                attachments=None,
                is_completed=False # Will be populated by frontend if needed, or separate call
            ) for s in steps_data
        ]

@router.post("/lessons/{lesson_id}/steps", response_model=StepSchema)
def create_step(
    lesson_id: int,
    step_data: StepCreateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Create new step in lesson"""
    lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    # Get course through module and check permissions
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found for this lesson")
    
    course = db.query(Course).filter(Course.id == module.course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found for this lesson")
    
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Calculate order_index if not provided or if it's 0
    if step_data.order_index == 0:
        # Get the highest order_index in this lesson and add 1
        max_order = db.query(func.max(Step.order_index)).filter(
            Step.lesson_id == lesson_id
        ).scalar() or 0
        calculated_order_index = max_order + 1
    else:
        calculated_order_index = step_data.order_index
    
    # Handle quiz data for quiz content type
    content_text = step_data.content_text
    if step_data.content_type == "quiz" and step_data.content_text:
        # For quiz type, content_text should contain quiz data JSON
        # This will be handled by the frontend
        pass
    
    new_step = Step(
        lesson_id=lesson_id,
        title=step_data.title,
        content_type=step_data.content_type,
        video_url=step_data.video_url,
        content_text=content_text,
        original_image_url=step_data.original_image_url,
        attachments=step_data.attachments,
        order_index=calculated_order_index,
        is_optional=step_data.is_optional or False
    )
    
    db.add(new_step)
    db.commit()
    db.refresh(new_step)

    # Update course duration
    update_course_duration(course.id, db)

    # Queue self-hosted HLS ingest if the step has a YouTube video (best-effort).
    video_ingest.safe_enqueue(db, new_step)

    return StepSchema.from_orm(new_step)

@router.get("/steps/{step_id}", response_model=StepSchema)
def get_step(
    step_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get step details"""
    step = db.query(Step).filter(Step.id == step_id).first()
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")
    
    # Get course through lesson and module, check access
    lesson = db.query(Lesson).filter(Lesson.id == step.lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found for this step")
    
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found for this step")
    
    if not check_course_access(module.course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this step")
    
    return StepSchema.from_orm(step)

@router.put("/steps/{step_id}", response_model=StepSchema)
def update_step(
    step_id: int,
    step_data: StepCreateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Update step"""
    step = db.query(Step).filter(Step.id == step_id).first()
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")
    
    # Get course through lesson and module, check permissions
    lesson = db.query(Lesson).filter(Lesson.id == step.lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found for this step")
    
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found for this step")
    
    course = db.query(Course).filter(Course.id == module.course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found for this step")
    
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Handle quiz data for quiz content type
    content_text = step_data.content_text
    if step_data.content_type == "quiz" and step_data.content_text:
        # For quiz type, content_text should contain quiz data JSON
        pass
    
    # Update step fields
    step.title = step_data.title
    step.content_type = step_data.content_type
    step.video_url = step_data.video_url
    step.content_text = content_text
    step.original_image_url = step_data.original_image_url
    
    # Update attachments if provided
    if step_data.attachments is not None:
        step.attachments = step_data.attachments
        
    if step_data.is_optional is not None:
        step.is_optional = step_data.is_optional
    
    # Only update order_index if it's not 0 (preserve existing order)
    if step_data.order_index != 0:
        step.order_index = step_data.order_index
    
    db.commit()
    db.refresh(step)

    # Update course duration
    update_course_duration(course.id, db)

    # Re-queue HLS ingest if the YouTube URL changed (idempotent; no-op otherwise).
    video_ingest.safe_enqueue(db, step)

    return StepSchema.from_orm(step)

@router.post("/lessons/{lesson_id}/reorder-steps")
def reorder_steps(
    lesson_id: int,
    step_orders: dict,  # Expected format: {"step_ids": [1, 3, 2, 4]}
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Reorder steps in a lesson by updating their order_index"""
    lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    # Get course through module, check permissions
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found for this lesson")
    
    course = db.query(Course).filter(Course.id == module.course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found for this lesson")
    
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get the new order of step IDs
    step_ids = step_orders.get("step_ids", [])
    if not step_ids:
        raise HTTPException(status_code=400, detail="step_ids is required")
    
    # Verify all steps belong to this lesson
    steps = db.query(Step).filter(Step.lesson_id == lesson_id).all()
    step_id_set = {step.id for step in steps}
    
    for step_id in step_ids:
        if step_id not in step_id_set:
            raise HTTPException(status_code=400, detail=f"Step {step_id} does not belong to lesson {lesson_id}")
    
    # Update order_index for each step
    for new_index, step_id in enumerate(step_ids, start=1):
        step = db.query(Step).filter(Step.id == step_id).first()
        if step:
            step.order_index = new_index
    
    db.commit()
    
    return {"message": "Steps reordered successfully", "step_ids": step_ids}

@router.post("/courses/{course_id}/lessons/{lesson_id}/split")
def split_lesson(
    course_id: int,
    lesson_id: int,
    split_data: dict,  # Expected format: {"after_step_index": <int>}
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Split a lesson into two at a given step index. Steps after the split point move to a new lesson."""
    # Validate lesson
    lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    course = db.query(Course).filter(Course.id == module.course_id).first()
    if not course or course.id != course_id:
        raise HTTPException(status_code=404, detail="Course not found or mismatch")
    
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    after_step_index = split_data.get("after_step_index")
    if after_step_index is None:
        raise HTTPException(status_code=400, detail="after_step_index is required")
    
    # Get all steps ordered
    steps = db.query(Step).filter(Step.lesson_id == lesson_id).order_by(Step.order_index).all()
    
    if after_step_index < 0 or after_step_index >= len(steps) - 1:
        raise HTTPException(status_code=400, detail="after_step_index must be >= 0 and < total_steps - 1")
    
    # Steps to keep in the original lesson (indices 0..after_step_index)
    steps_to_keep = steps[:after_step_index + 1]
    # Steps to move to the new lesson
    steps_to_move = steps[after_step_index + 1:]
    
    if not steps_to_move:
        raise HTTPException(status_code=400, detail="No steps to move after split point")
    
    # Shift subsequent lessons in the module to make room
    subsequent_lessons = db.query(Lesson).filter(
        Lesson.module_id == lesson.module_id,
        Lesson.order_index > lesson.order_index
    ).all()
    for subsequent in subsequent_lessons:
        subsequent.order_index += 1
    
    # Create new lesson
    new_lesson = Lesson(
        module_id=lesson.module_id,
        title=f"{lesson.title} (Part 2)",
        description=lesson.description,
        duration_minutes=0,
        order_index=lesson.order_index + 1,
        is_initially_unlocked=lesson.is_initially_unlocked,
    )
    db.add(new_lesson)
    db.flush()  # Get the new lesson ID
    
    # Update next_lesson_id pointers
    old_next = lesson.next_lesson_id
    lesson.next_lesson_id = new_lesson.id
    new_lesson.next_lesson_id = old_next
    
    # Move steps to the new lesson and renumber
    moved_step_ids = []
    for new_order, step in enumerate(steps_to_move, start=1):
        moved_step_ids.append(step.id)
        step.lesson_id = new_lesson.id
        step.order_index = new_order
    
    # Update StepProgress.lesson_id for moved steps
    if moved_step_ids:
        from src.schemas.models import StepProgress
        db.query(StepProgress).filter(
            StepProgress.step_id.in_(moved_step_ids)
        ).update(
            {StepProgress.lesson_id: new_lesson.id},
            synchronize_session='fetch'
        )
    
    db.commit()
    db.refresh(lesson)
    db.refresh(new_lesson)
    
    return {
        "message": "Lesson split successfully",
        "original_lesson_id": lesson.id,
        "new_lesson_id": new_lesson.id,
        "new_lesson_title": new_lesson.title,
        "steps_moved": len(steps_to_move),
    }

@router.delete("/steps/{step_id}")
def delete_step(
    step_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Delete step"""
    step = db.query(Step).filter(Step.id == step_id).first()
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")
    
    # Get course through lesson and module, check permissions
    lesson = db.query(Lesson).filter(Lesson.id == step.lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found for this step")
    
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found for this step")
    
    course = db.query(Course).filter(Course.id == module.course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found for this step")
    
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Delete related step progress records first
    from src.schemas.models import StepProgress
    step_progress_records = db.query(StepProgress).filter(StepProgress.step_id == step_id).all()
    for record in step_progress_records:
        db.delete(record)
    
    # Now delete the step
    db.delete(step)
    db.commit()
    
    # Update course duration
    update_course_duration(course.id, db)
    
    return {"detail": "Step deleted successfully"}

@router.post("/{course_id}/fix-lesson-order")
def fix_lesson_order(
    course_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Fix lesson order for a course by reassigning order_index values"""
    # Check course access
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get all modules for the course
    modules = db.query(Module).filter(Module.course_id == course_id).order_by(Module.order_index).all()
    
    fixed_count = 0
    for module in modules:
        # Get all lessons for this module
        lessons = db.query(Lesson).filter(Lesson.module_id == module.id).order_by(Lesson.id).all()
        
        # Reassign order_index values
        for index, lesson in enumerate(lessons):
            if lesson.order_index == 0 or lesson.order_index != index + 1:
                lesson.order_index = index + 1
                fixed_count += 1
    
    if fixed_count > 0:
        db.commit()
    
    return {"message": f"Fixed order for {fixed_count} lessons", "fixed_count": fixed_count}

# =============================================================================
# LESSON MATERIALS
# =============================================================================

@router.get("/lessons/{lesson_id}/materials", response_model=List[LessonMaterialSchema])
@cached(namespace="courses:lesson-materials", ttl=300, key_args=("lesson_id",))
def get_lesson_materials(
    lesson_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get all materials for a lesson"""
    lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    # Check access through course
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not check_course_access(module.course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied")
    
    materials = db.query(LessonMaterial).filter(
        LessonMaterial.lesson_id == lesson_id
    ).all()
    
    return [LessonMaterialSchema.from_orm(material) for material in materials]

# =============================================================================
# COURSE ENROLLMENT
# =============================================================================

@router.post("/{course_id}/enroll")
def enroll_student(
    course_id: int,
    student_id: Optional[int] = None,  # For admin/teacher to enroll specific student
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Enroll student in course"""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Determine who to enroll
    target_user_id = student_id if student_id and current_user.role in ["admin", "teacher"] else current_user.id
    
    # Check if already enrolled
    existing_enrollment = db.query(Enrollment).filter(
        Enrollment.user_id == target_user_id,
        Enrollment.course_id == course_id
    ).first()
    
    if existing_enrollment:
        if existing_enrollment.is_active:
            raise HTTPException(status_code=400, detail="Already enrolled in this course")
        else:
            # Reactivate enrollment
            existing_enrollment.is_active = True
            db.commit()
            return {"detail": "Enrollment reactivated"}
    
    # Create new enrollment
    enrollment = Enrollment(
        user_id=target_user_id,
        course_id=course_id,
        is_active=True
    )
    
    db.add(enrollment)
    db.commit()
    
    return {"detail": "Successfully enrolled in course"}

@router.post("/{course_id}/auto-enroll-students")
def auto_enroll_students(
    course_id: int,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Automatically enroll all students in teacher's groups to the course"""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Check if user is the course teacher or admin
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get all students in teacher's groups
    if current_user.role == "teacher":
        # Get all groups created by this teacher
        teacher_groups = db.query(Group).filter(Group.teacher_id == current_user.id).all()
        group_ids = [group.id for group in teacher_groups]
    else:
        # Admin can enroll all students
        group_ids = db.query(Group.id).all()
        group_ids = [g[0] for g in group_ids]
    
    if not group_ids:
        return {"detail": "No groups found for auto-enrollment"}
    
    # Get all students in these groups
    group_students = db.query(GroupStudent).filter(
        GroupStudent.group_id.in_(group_ids)
    ).all()
    
    enrolled_count = 0
    already_enrolled_count = 0
    
    for group_student in group_students:
        # Check if already enrolled
        existing_enrollment = db.query(Enrollment).filter(
            Enrollment.user_id == group_student.student_id,
            Enrollment.course_id == course_id
        ).first()
        
        if existing_enrollment:
            if not existing_enrollment.is_active:
                # Reactivate enrollment
                existing_enrollment.is_active = True
                enrolled_count += 1
            else:
                already_enrolled_count += 1
        else:
            # Create new enrollment
            enrollment = Enrollment(
                user_id=group_student.student_id,
                course_id=course_id,
                is_active=True
            )
            db.add(enrollment)
            enrolled_count += 1
    
    db.commit()
    
    return {
        "detail": f"Auto-enrollment completed",
        "enrolled_count": enrolled_count,
        "already_enrolled_count": already_enrolled_count,
        "total_students": len(group_students)
    }

@router.delete("/{course_id}/enroll")
def unenroll_student(
    course_id: int,
    student_id: Optional[int] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Remove student from course"""
    target_user_id = student_id if student_id and current_user.role in ["admin", "teacher"] else current_user.id
    
    enrollment = db.query(Enrollment).filter(
        Enrollment.user_id == target_user_id,
        Enrollment.course_id == course_id,
        Enrollment.is_active == True
    ).first()
    
    if not enrollment:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    
    # Soft delete enrollment
    enrollment.is_active = False
    db.commit()
    
    return {"detail": "Successfully unenrolled from course"}

# =============================================================================
# COURSE GROUP ACCESS MANAGEMENT
# =============================================================================

@router.get("/{course_id}/groups", response_model=List[CourseGroupAccessSchema])
def get_course_groups(
    course_id: int,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Get groups that have access to a course"""
    # Check if course exists and user has access
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get group access records
    group_access = db.query(CourseGroupAccess).filter(
        CourseGroupAccess.course_id == course_id,
        CourseGroupAccess.is_active == True
    ).all()
    
    # Enrich with group and user information
    result = []
    for access in group_access:
        group = db.query(Group).filter(Group.id == access.group_id).first()
        granted_by_user = db.query(UserInDB).filter(UserInDB.id == access.granted_by).first()
        
        # Count students in group using GroupStudent association table
        student_count = db.query(GroupStudent).filter(
            GroupStudent.group_id == access.group_id
        ).count()
        
        access_data = CourseGroupAccessSchema.from_orm(access)
        access_data.group_name = group.name if group else "Unknown Group"
        access_data.student_count = student_count
        access_data.granted_by_name = granted_by_user.name if granted_by_user else "Unknown"
        
        result.append(access_data)
    
    return result

@router.post("/{course_id}/grant-group-access/{group_id}")
def grant_course_access_to_group(
    course_id: int,
    group_id: int,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Grant access to a course for a specific group"""
    # Check if course exists and user has access
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Check if group exists
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    
    # Check if access already exists
    existing_access = db.query(CourseGroupAccess).filter(
        CourseGroupAccess.course_id == course_id,
        CourseGroupAccess.group_id == group_id,
        CourseGroupAccess.is_active == True
    ).first()
    
    if existing_access:
        return {
            "detail": f"Group '{group.name}' already has access to this course",
            "status": "already_granted",
            "access_id": existing_access.id
        }
    
    # Create new access record
    access = CourseGroupAccess(
        course_id=course_id,
        group_id=group_id,
        granted_by=current_user.id,
        is_active=True
    )
    
    db.add(access)
    db.commit()
    
    return {
        "detail": f"Access granted to group '{group.name}'",
        "status": "granted",
        "access_id": access.id
    }

@router.delete("/{course_id}/revoke-group-access/{group_id}")
def revoke_course_access_from_group(
    course_id: int,
    group_id: int,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Revoke access to a course for a specific group"""
    # Check if course exists and user has access
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Find and deactivate access record
    access = db.query(CourseGroupAccess).filter(
        CourseGroupAccess.course_id == course_id,
        CourseGroupAccess.group_id == group_id,
        CourseGroupAccess.is_active == True
    ).first()
    
    if not access:
        raise HTTPException(status_code=404, detail="Group access not found")
    
    # Get group name for response
    group = db.query(Group).filter(Group.id == group_id).first()
    group_name = group.name if group else "Unknown Group"
    
    # Soft delete access
    access.is_active = False
    db.commit()
    
    return {"detail": f"Access revoked from group '{group_name}'"}

@router.get("/{course_id}/group-access-status")
def get_course_group_access_status(
    course_id: int,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Get status of group access for a course"""
    # Check if course exists and user has access
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get all groups that have access to this course
    group_access = db.query(CourseGroupAccess).filter(
        CourseGroupAccess.course_id == course_id,
        CourseGroupAccess.is_active == True
    ).all()
    
    # Get group IDs that have access
    group_ids_with_access = [access.group_id for access in group_access]
    
    return {
        "course_id": course_id,
        "groups_with_access": group_ids_with_access,
        "total_groups_with_access": len(group_ids_with_access)
    }

# =============================================================================
# COURSE TEACHER ACCESS MANAGEMENT
# =============================================================================

@router.get("/{course_id}/teacher-access", response_model=List[CourseTeacherAccessSchema])
def get_course_teacher_access(
    course_id: int,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Get teachers who have direct access to a course"""
    # Check if course exists and user has access
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get direct access records
    access_records = db.query(CourseTeacherAccess).filter(
        CourseTeacherAccess.course_id == course_id,
        CourseTeacherAccess.is_active == True
    ).all()
    
    # Enrich with teacher and grant info
    result = []
    for access in access_records:
        teacher = db.query(UserInDB).filter(UserInDB.id == access.teacher_id).first()
        granted_by_user = db.query(UserInDB).filter(UserInDB.id == access.granted_by).first()
        
        access_data = CourseTeacherAccessSchema.from_orm(access)
        access_data.teacher_name = teacher.name if teacher else "Unknown Teacher"
        access_data.teacher_email = teacher.email if teacher else ""
        access_data.granted_by_name = granted_by_user.name if granted_by_user else "Unknown"
        
        result.append(access_data)
    
    return result

@router.post("/{course_id}/grant-teacher-access/{teacher_id}")
def grant_course_access_to_teacher(
    course_id: int,
    teacher_id: int,
    current_user: UserInDB = Depends(require_admin()),
    db: Session = Depends(get_db)
):
    """Grant direct access to a course for a specific teacher (Admin only)"""
    # Check if course exists
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Check if teacher exists (and is actually a teacher or admin)
    teacher = db.query(UserInDB).filter(UserInDB.id == teacher_id).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
    
    if teacher.role not in ["teacher", "admin", "head_teacher"]:
        raise HTTPException(status_code=400, detail="User is not a teacher")
    
    # Check if teacher is the course creator (already has access)
    if course.teacher_id == teacher_id:
        return {
            "detail": f"Teacher '{teacher.name}' is the creator of this course",
            "status": "creator"
        }

    # Check if access already exists
    existing_access = db.query(CourseTeacherAccess).filter(
        CourseTeacherAccess.course_id == course_id,
        CourseTeacherAccess.teacher_id == teacher_id,
        CourseTeacherAccess.is_active == True
    ).first()
    
    if existing_access:
        return {
            "detail": f"Teacher '{teacher.name}' already has direct access to this course",
            "status": "already_granted",
            "access_id": existing_access.id
        }
    
    # Create new access record
    access = CourseTeacherAccess(
        course_id=course_id,
        teacher_id=teacher_id,
        granted_by=current_user.id,
        is_active=True
    )
    
    db.add(access)
    db.commit()
    
    return {
        "detail": f"Access granted to teacher '{teacher.name}'",
        "status": "granted",
        "access_id": access.id
    }

@router.delete("/{course_id}/revoke-teacher-access/{teacher_id}")
def revoke_course_access_from_teacher(
    course_id: int,
    teacher_id: int,
    current_user: UserInDB = Depends(require_admin()),
    db: Session = Depends(get_db)
):
    """Revoke direct access to a course for a specific teacher (Admin only)"""
    # Check if course exists
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Find and deactivate access record
    access = db.query(CourseTeacherAccess).filter(
        CourseTeacherAccess.course_id == course_id,
        CourseTeacherAccess.teacher_id == teacher_id,
        CourseTeacherAccess.is_active == True
    ).first()
    
    if not access:
        raise HTTPException(status_code=404, detail="Teacher access not found")
    
    # Soft delete access
    access.is_active = False
    db.commit()
    
    return {"detail": "Access revoked successfully"}

@router.post("/analyze-sat-image")
async def analyze_sat_image(
    image: UploadFile = File(...),
    correct_answers: str = Form(None),
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """
    Analyze SAT question image or PDF using Gemini
    Upload an image/PDF of a SAT question and get structured question data
    Optionally provide correct answers to override AI detection
    """
    try:
        # Validate file type
        if not (image.content_type.startswith('image/') or image.content_type == 'application/pdf'):
            raise HTTPException(status_code=400, detail="File must be an image or PDF")
        
        # Generate unique filename
        file_extension = os.path.splitext(image.filename)[1] if image.filename else '.png'
        unique_filename = f"{uuid.uuid4()}{file_extension}"

        # Persist to storage (S3 or local) and keep the stable /uploads/sat_images/... url
        content = await image.read()
        stored_url = storage_service.save(f"sat_images/{unique_filename}", content, image.content_type)

        # The parser needs a local file path, so write a temp copy that works on any backend
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=file_extension, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        # Use Gemini Parser
        from src.services.parser import parser_service

        # Analyze the file
        try:
            questions = await parser_service.parse_file(tmp_path, mime_type=image.content_type, correct_answers=correct_answers)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        
        # If we got multiple questions, return the first one for now as the frontend expects a single result structure
        # OR update frontend to handle multiple. 
        # The user wants to upload a document with multiple questions.
        # But the current frontend `analyzeImageFile` seems to expect a single result structure with `question_text`, `options`, etc.
        # Let's see what the frontend expects.
        # Frontend: `const result = await apiClient.analyzeSatImage(file);`
        # Then: `const optionsArray = Array.isArray(result.options) ? result.options : [];`
        
        # If I return a list, the frontend might break if it expects a single object.
        # However, the user asked for "upload a PDF document of some SAT test + correct answers ... Gemini takes and converts them".
        # This implies bulk import.
        
        # I should probably return the list of questions.
        # But I need to check if I should change the endpoint signature or create a new one.
        # The current endpoint is `/analyze-sat-image`.
        # I'll return a wrapper object that can contain multiple questions.
        
        # If it's a single image, maybe it's just one question.
        # If it's a PDF, it's likely multiple.
        
        # Let's return:
        # {
        #   "success": True,
        #   "questions": questions, # List of parsed questions
        #   # For backward compatibility with single-image frontend logic (if any):
        #   ...questions[0] if questions else {}
        # }
        
        result = {
            "success": True,
            "questions": questions,
            "file_url": stored_url,
            "original_filename": image.filename
        }
        
        # Backward compatibility for single question (if the frontend uses these fields directly)
        if questions:
            q = questions[0]
            result.update({
                "question_text": q.get("question_text"),
                "options": q.get("options"),
                "correct_answer": q.get("options")[q.get("correct_answer")]["letter"] if q.get("options") and isinstance(q.get("correct_answer"), int) else "A",
                "explanation": q.get("explanation"),
                "content_text": q.get("content_text")
            })
        
        # Clean up the temporary file? 
        # Maybe keep it if it's needed for "original_image_url"
        # The user said "In some questions you need to upload images so let gemini mark such questions"
        # If Gemini says "needs_image", we might need to point to this file or crop it?
        # For now, we keep the file.
        
        return result
        
    except Exception as e:
        print(f"Error analyzing file: {e}")
        raise HTTPException(status_code=500, detail=f"Error analyzing file: {str(e)}")

@router.post("/{course_id}/add-summary-steps")
def add_summary_steps_to_course(
    course_id: int,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """
    Automatically add a summary step to all lessons in the course that don't have one.
    """
    # Verify course exists
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
        
    # Get all lessons
    lessons = db.query(Lesson).join(Module).filter(Module.course_id == course_id).all()
    
    added_count = 0
    
    for lesson in lessons:
        # Check if summary step already exists
        has_summary = db.query(Step).filter(
            Step.lesson_id == lesson.id,
            Step.content_type == 'summary'
        ).first()
        
        if not has_summary:
            # Get max order index
            max_order = db.query(func.max(Step.order_index)).filter(
                Step.lesson_id == lesson.id
            ).scalar() or 0
            
            # Create summary step
            summary_step = Step(
                lesson_id=lesson.id,
                title="Lesson Summary",
                content_type="summary",
                content_text="",
                order_index=max_order + 1
            )
            db.add(summary_step)
            added_count += 1
            
    db.commit()
    
    return {"message": f"Added summary steps to {added_count} lessons", "added_count": added_count}

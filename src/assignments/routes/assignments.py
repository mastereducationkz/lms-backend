from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import desc, and_, select
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone, timedelta
import json

from src.config import get_db
from src.schemas.models import (
    Assignment, AssignmentSubmission, Lesson, Module, Course, UserInDB, Enrollment,
    AssignmentSchema, AssignmentCreateSchema, AssignmentSubmissionSchema,
    SubmitAssignmentSchema, GradeSubmissionSchema, AssignmentLinkedLesson,
    AssignmentExtension, AssignmentExtensionSchema, GrantExtensionSchema,
    Event, EventGroup, Group, CourseGroupAccess,
)
from src.routes.auth import get_current_user_dependency
from src.utils.permissions import require_teacher_or_admin, check_course_access
from src.utils.assignment_checker import check_assignment_answers
from src.services.email_service import send_homework_notification
from src.schemas.models import GroupStudent
from src.services.event_service import EventService
from src.routes.gamification import award_points
from src.utils.course_access import student_can_see_homework_for_course


def _assignment_course_id(assignment: Assignment, db: Session) -> Optional[int]:
    if assignment.lesson_id:
        lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
        if lesson:
            mod = db.query(Module).filter(Module.id == lesson.module_id).first()
            if mod:
                return mod.course_id
    if assignment.group_id:
        cga = (
            db.query(CourseGroupAccess)
            .filter(
                CourseGroupAccess.group_id == assignment.group_id,
                CourseGroupAccess.is_active == True,
            )
            .first()
        )
        if cga:
            return cga.course_id
    return None


def assignment_visible_to_student(user_id: int, assignment: Assignment, db: Session) -> bool:
    cid = _assignment_course_id(assignment, db)
    if cid is None:
        return True
    if student_can_see_homework_for_course(user_id, cid, db):
        return True
    if assignment.group_id:
        g = db.query(Group).filter(Group.id == assignment.group_id).first()
        if g and g.is_special:
            return False
    if assignment.lesson_id:
        return False
    return True


def _to_enriched_schema(assignment: Assignment) -> AssignmentSchema:
    schema = AssignmentSchema.from_orm(assignment)
    if assignment.event:
        schema.event_start_datetime = assignment.event.start_datetime
    if assignment.group:
        schema.group_name = assignment.group.name
    return schema

router = APIRouter()

def to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert datetime to naive UTC for safe comparison with database timestamps."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

# =============================================================================
# ASSIGNMENT MANAGEMENT
# =============================================================================

@router.get("/", response_model=List[AssignmentSchema])
async def get_assignments(
    lesson_id: Optional[str] = None,
    course_id: Optional[int] = None,
    group_id: Optional[int] = None,
    assignment_type: Optional[str] = None,
    is_active: Optional[bool] = True,
    include_hidden: Optional[bool] = False,  # Teachers can see hidden assignments
    skip: int = Query(0, ge=0),
    limit: int = Query(100, le=1000),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get assignments based on filters and user permissions"""
    query = db.query(Assignment)
    
    # Filter hidden assignments (only teachers/admins can see hidden)
    # Note: is_hidden can be NULL for old records, so we check for both NULL and False
    if not include_hidden or current_user.role == "student":
        query = query.filter((Assignment.is_hidden == False) | (Assignment.is_hidden == None))
    
    # Apply filters
    if lesson_id:
        try:
            lesson_id_int = int(lesson_id)
            query = query.filter(Assignment.lesson_id == lesson_id_int)
        except (ValueError, TypeError):
            # If lesson_id is not a valid integer, ignore it
            pass
    if group_id:
        query = query.filter(Assignment.group_id == group_id)
        if current_user.role == "teacher":
            from src.schemas.models import Group
            g = db.query(Group).filter(Group.id == group_id).first()
            if not g or g.teacher_id != current_user.id:
                raise HTTPException(status_code=403, detail="Access denied to this group")
        elif current_user.role == "curator":
            from src.schemas.models import Group
            g = db.query(Group).filter(Group.id == group_id).first()
            if not g or g.curator_id != current_user.id:
                raise HTTPException(status_code=403, detail="Access denied to this group")
    if assignment_type:
        query = query.filter(Assignment.assignment_type == assignment_type)
    if is_active is not None:
        query = query.filter(Assignment.is_active == is_active)
    
    # Apply course filter and access control
    if course_id:
        # Get lessons in the course
        lesson_ids = db.query(Lesson.id).join(Module).filter(Module.course_id == course_id).subquery()
        query = query.filter(Assignment.lesson_id.in_(lesson_ids))
        
        # Check course access
        if not check_course_access(course_id, current_user, db):
            raise HTTPException(status_code=403, detail="Access denied to this course")
    elif current_user.role in ["teacher", "curator"]:
        # Teachers and Curators see assignments from their groups by default if no filters are provided
        from src.schemas.models import Group
        
        # Determine accessible groups
        if current_user.role == "teacher":
            user_groups = db.query(Group).filter(Group.teacher_id == current_user.id).all()
        else:
            user_groups = db.query(Group).filter(Group.curator_id == current_user.id).all()
            
        user_group_ids = [g.id for g in user_groups]
        
        # If no group_id/course_id filter is provided, restrict to their groups
        if not group_id and not course_id and current_user.role != "admin":
            if user_group_ids:
                query = query.filter(Assignment.group_id.in_(user_group_ids))
            else:
                # If they have no groups, they see no assignments (unless filtered specifically, but here we cover default)
                return []
    
    elif current_user.role == "student":
        # Students see only assignments from their enrolled courses and groups
        from src.schemas.models import Enrollment, GroupStudent
        
        # Get enrolled course IDs
        enrolled_course_ids = db.query(Course.id).join(Enrollment).filter(
            Enrollment.user_id == current_user.id,
            Enrollment.is_active == True
        ).subquery()
        
        # Get lesson IDs from enrolled courses
        lesson_ids = db.query(Lesson.id).join(Module).filter(
            Module.course_id.in_(select(enrolled_course_ids))
        ).subquery()
        
        # Get group IDs where student is a member
        group_ids_query = db.query(GroupStudent.group_id).filter(
            GroupStudent.student_id == current_user.id
        )
        group_ids_list = [g[0] for g in group_ids_query.all()]
        
        group_ids = group_ids_query.subquery()
        
        query = query.filter(
            (Assignment.lesson_id.in_(select(lesson_ids))) | (Assignment.group_id.in_(select(group_ids)))
        )
    elif current_user.role == "teacher":
        # Teachers see only assignments from their courses and groups
        teacher_course_ids = db.query(Course.id).filter(Course.teacher_id == current_user.id).subquery()
        lesson_ids = db.query(Lesson.id).join(Module).filter(
            Module.course_id.in_(teacher_course_ids)
        ).subquery()
        
        # Get group IDs where teacher is the teacher
        from src.schemas.models import Group
        teacher_group_ids = db.query(Group.id).filter(Group.teacher_id == current_user.id).subquery()
        
        query = query.filter(
            (Assignment.lesson_id.in_(lesson_ids)) | (Assignment.group_id.in_(teacher_group_ids))
        )
    
    query = query.order_by(
        Assignment.created_at.desc().nullslast(),
        Assignment.id.desc(),
    )
    assignments = query.options(joinedload(Assignment.event), joinedload(Assignment.group)).offset(skip).limit(limit).all()
    if current_user.role == "student":
        assignments = [a for a in assignments if assignment_visible_to_student(current_user.id, a, db)]
    return [_to_enriched_schema(a) for a in assignments]

@router.patch("/{assignment_id}/toggle-visibility", response_model=AssignmentSchema)
async def toggle_assignment_visibility(
    assignment_id: int,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Toggle assignment visibility (hide/show for all users)"""
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # Check permissions
    has_access = False
    
    if current_user.role == "admin":
        has_access = True
    elif assignment.lesson_id:
        lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
        if lesson:
            module = db.query(Module).filter(Module.id == lesson.module_id).first()
            if module and check_course_access(module.course_id, current_user, db):
                has_access = True
    elif assignment.group_id:
        from src.schemas.models import Group
        group = db.query(Group).filter(Group.id == assignment.group_id).first()
        if group and (
            group.teacher_id == current_user.id or group.curator_id == current_user.id
        ):
            has_access = True
    
    if not has_access:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Toggle visibility
    assignment.is_hidden = not assignment.is_hidden
    db.commit()
    db.refresh(assignment)
    
    return _to_enriched_schema(assignment)

@router.get("/assigned-lessons/{course_id}")
async def get_assigned_lessons_for_course(
    course_id: int,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """
    Get all lessons of a course that have already been assigned in homework.
    Returns a list of { lesson_id, assignment_id, assignment_title, group_id, group_name, created_at }.
    Helps teachers avoid assigning the same lesson twice.
    Excludes assignments from groups that have finished training (is_over=True).
    """
    from src.schemas.models import Group

    # Get all lesson IDs for this course
    course_lesson_ids = (
        db.query(Lesson.id)
        .join(Module)
        .filter(Module.course_id == course_id)
        .subquery()
    )

    # Only show assignments from groups the current user owns
    if current_user.role == "admin":
        my_group_ids = None  # admins see everything
    else:
        my_groups = db.query(Group).filter(
            (Group.teacher_id == current_user.id) | (Group.curator_id == current_user.id)
        ).all()
        my_group_ids = [g.id for g in my_groups]

    # Query AssignmentLinkedLesson joined with Assignment to get details
    query = (
        db.query(
            AssignmentLinkedLesson.lesson_id,
            Assignment.id.label("assignment_id"),
            Assignment.title.label("assignment_title"),
            Assignment.group_id,
            Assignment.created_at,
        )
        .join(Assignment, AssignmentLinkedLesson.assignment_id == Assignment.id)
        .join(Group, Assignment.group_id == Group.id)
        .filter(
            AssignmentLinkedLesson.lesson_id.in_(course_lesson_ids),
            Assignment.is_active == True,
            (Assignment.is_hidden == False) | (Assignment.is_hidden == None),
            Group.is_over == False,
        )
    )

    # Filter to only the current user's groups
    if my_group_ids is not None:
        query = query.filter(Assignment.group_id.in_(my_group_ids))

    linked = query.all()

    # Collect group names
    group_ids = list(set(r.group_id for r in linked if r.group_id))
    groups_map = {}
    if group_ids:
        groups_list = db.query(Group).filter(Group.id.in_(group_ids)).all()
        groups_map = {g.id: g.name for g in groups_list}

    result = []
    for row in linked:
        result.append({
            "lesson_id": row.lesson_id,
            "assignment_id": row.assignment_id,
            "assignment_title": row.assignment_title,
            "group_id": row.group_id,
            "group_name": groups_map.get(row.group_id, ""),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })

    return result


@router.post("/", response_model=AssignmentSchema)
async def create_assignment(
    assignment_data: AssignmentCreateSchema,
    lesson_id: Optional[int] = None,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Create new assignment"""
    import logging
    logger = logging.getLogger(__name__)
    logger.warning(f"Creating assignment with data: lesson_number_mapping={assignment_data.lesson_number_mapping}, event_mapping={assignment_data.event_mapping}")
    
    target_lesson_id = lesson_id
    
    # If lesson_id provided, check permissions
    if target_lesson_id:
        lesson = db.query(Lesson).filter(Lesson.id == target_lesson_id).first()
        if not lesson:
            raise HTTPException(status_code=404, detail="Lesson not found")
        
        # Get course through module
        module = db.query(Module).filter(Module.id == lesson.module_id).first()
        course = db.query(Course).filter(Course.id == module.course_id).first()
        
        # Check permissions
        if current_user.role != "admin" and course.teacher_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied")
    
    # If group_id provided, check permissions
    if assignment_data.group_id:
        from src.schemas.models import Group
        group = db.query(Group).filter(Group.id == assignment_data.group_id).first()
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")
        
        # Check permissions
        if current_user.role != "admin" and group.teacher_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied")
    
    # Validate assignment content based on type
    validate_assignment_content(assignment_data.assignment_type, assignment_data.content)
    
    # Validate due date
    # if assignment_data.due_date and to_naive_utc(assignment_data.due_date) < datetime.now(timezone.utc).replace(tzinfo=None).replace(tzinfo=None):
    #     raise HTTPException(status_code=400, detail="Due date cannot be in the past")
    
    if assignment_data.due_date:
        print(f"DEBUG: Assignment creation - Due Date: {assignment_data.due_date}, Now (UTC): {datetime.now(timezone.utc)}")
        
    # Determine target groups
    target_group_ids = []
    if assignment_data.group_ids:
        target_group_ids = assignment_data.group_ids
    elif assignment_data.group_id:
        target_group_ids = [assignment_data.group_id]
        
    created_assignments = []
    
    # Cache for resolved event IDs to avoid redundant materialization in same request
    _resolved_ids_cache = {}

    def resolve_eid(eid: Optional[int], db: Session) -> Optional[int]:
        if eid is None: return None
        if eid not in _resolved_ids_cache:
            _resolved_ids_cache[eid] = EventService.resolve_event_id(db, eid, user_id=current_user.id)
        return _resolved_ids_cache[eid]
    
    # If we have target groups, create an assignment for each group
    if target_group_ids:
        for gid in target_group_ids:
            # Check permissions for each group
            from src.schemas.models import Group
            group = db.query(Group).filter(Group.id == gid).first()
            if not group:
                continue # Skip invalid groups or raise error?
            
            if current_user.role != "admin" and group.teacher_id != current_user.id:
                raise HTTPException(status_code=403, detail=f"Access denied to group {gid}")
                
            # Determine settings for this group
            event_id_for_group = resolve_eid(assignment_data.event_id, db)
            if assignment_data.event_mapping and gid in assignment_data.event_mapping:
                event_id_for_group = resolve_eid(assignment_data.event_mapping[gid], db)
                
            # Determine lesson_number for this group
            lesson_number_for_group = None
            if assignment_data.lesson_number_mapping and gid in assignment_data.lesson_number_mapping:
                lesson_number_for_group = assignment_data.lesson_number_mapping[gid]
                
            # Determine due date for this group
            due_date_for_group = assignment_data.due_date
            if assignment_data.due_date_mapping and gid in assignment_data.due_date_mapping:
                due_date_for_group = assignment_data.due_date_mapping[gid]

            new_assignment = Assignment(
                lesson_id=target_lesson_id,
                group_id=gid,
                title=assignment_data.title,
                description=assignment_data.description,
                assignment_type=assignment_data.assignment_type,
                content=json.dumps(assignment_data.content),
                correct_answers=json.dumps(assignment_data.correct_answers) if assignment_data.correct_answers else None,
                max_score=assignment_data.max_score,
                time_limit_minutes=assignment_data.time_limit_minutes if hasattr(assignment_data, 'time_limit_minutes') else None,
                due_date=due_date_for_group,
                allowed_file_types=assignment_data.allowed_file_types,
                max_file_size_mb=assignment_data.max_file_size_mb,
                event_id=event_id_for_group,
                lesson_number=lesson_number_for_group,
                late_penalty_enabled=assignment_data.late_penalty_enabled,
                late_penalty_multiplier=assignment_data.late_penalty_multiplier
            )
            db.add(new_assignment)
            created_assignments.append(new_assignment)
            
    else:
        # Create single assignment (e.g. lesson-only or no group)
        new_assignment = Assignment(
            lesson_id=target_lesson_id,
            group_id=None,
            title=assignment_data.title,
            description=assignment_data.description,
            assignment_type=assignment_data.assignment_type,
            content=json.dumps(assignment_data.content),
            correct_answers=json.dumps(assignment_data.correct_answers) if assignment_data.correct_answers else None,
            max_score=assignment_data.max_score,
            time_limit_minutes=assignment_data.time_limit_minutes if hasattr(assignment_data, 'time_limit_minutes') else None,
            due_date=assignment_data.due_date,
            allowed_file_types=assignment_data.allowed_file_types,
            max_file_size_mb=assignment_data.max_file_size_mb,
            event_id=resolve_eid(assignment_data.event_id, db),
            late_penalty_enabled=assignment_data.late_penalty_enabled,
            late_penalty_multiplier=assignment_data.late_penalty_multiplier
        )
        db.add(new_assignment)
        created_assignments.append(new_assignment)
    
    db.commit()
    
    # Refresh all created assignments and sync linked lessons
    for a in created_assignments:
        db.refresh(a)
        sync_assignment_linked_lessons(a, db)
    
    # Return the first one to satisfy response model
    result_assignment = _to_enriched_schema(created_assignments[0])
    
    # Send email notifications
    try:
        # Collect student emails
        student_emails = []
        course_title = "Course"
        
        # If lesson_id (Course assignment)
        if target_lesson_id:
            lesson = db.query(Lesson).filter(Lesson.id == target_lesson_id).first()
            if lesson:
                module = db.query(Module).filter(Module.id == lesson.module_id).first()
                if module:
                    course = db.query(Course).filter(Course.id == module.course_id).first()
                    if course:
                        course_title = course.title
                        # Get enrolled students
                        course_title = course.title
                        # Get students via CourseGroupAccess (Groups linked to Course)
                        from src.schemas.models import CourseGroupAccess
                        
                        # 1. Find all groups that have access to this course
                        course_groups = db.query(CourseGroupAccess).filter(
                            CourseGroupAccess.course_id == course.id,
                            CourseGroupAccess.is_active == True
                        ).all()
                        
                        group_ids = [cg.group_id for cg in course_groups]
                        
                        if group_ids:
                            # 2. Get all students in these groups
                            group_students = db.query(GroupStudent).filter(
                                GroupStudent.group_id.in_(group_ids)
                            ).all()
                            
                            student_ids = [gs.student_id for gs in group_students]
                            
                            if student_ids:
                                users = db.query(UserInDB).filter(UserInDB.id.in_(student_ids)).all()
                                student_emails.extend([u.email for u in users])
        
        # If specific groups
        if target_group_ids:
            # Fallback: if course_title is still "Course", try to get it from the group
            if course_title == "Course" and target_group_ids:
                first_group_id = target_group_ids[0]
                # Try to find course via CourseGroupAccess
                from src.schemas.models import CourseGroupAccess, Group
                
                # Check for linked course first
                cga = db.query(CourseGroupAccess).filter(
                    CourseGroupAccess.group_id == first_group_id,
                    CourseGroupAccess.is_active == True
                ).first()
                
                if cga:
                    linked_course = db.query(Course).filter(Course.id == cga.course_id).first()
                    if linked_course:
                        course_title = linked_course.title
                
                # If still "Course", use Group Name
                if course_title == "Course":
                    group = db.query(Group).filter(Group.id == first_group_id).first()
                    if group:
                        course_title = group.name

            group_student_ids = db.query(GroupStudent.student_id).filter(
                GroupStudent.group_id.in_(target_group_ids)
            ).all()
            ids = [bg[0] for bg in group_student_ids]
            if ids:
                users = db.query(UserInDB).filter(UserInDB.id.in_(ids)).all()
                student_emails.extend([u.email for u in users])
        
        # Deduplicate
        student_emails = list(set(student_emails))
        
        if student_emails:
            # Format due date
            due_str = "No deadline"
            if assignment_data.due_date:
                due_str = assignment_data.due_date.strftime("%d %B %Y, %H:%M")
                
            send_homework_notification(
                student_emails,
                assignment_data.title,
                course_title,
                due_str,
                action="created",
                assignment_id=created_assignments[0].id,
            )
            
    except Exception as e:
        print(f"Failed to send email notifications: {e}")

    return result_assignment

@router.get("/{assignment_id}", response_model=AssignmentSchema)
async def get_assignment(
    assignment_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get assignment details"""
    
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    if current_user.role == "student" and not assignment_visible_to_student(current_user.id, assignment, db):
        raise HTTPException(status_code=403, detail="Access denied to this assignment")

    # Check access permissions
    has_access = False
    
    # Check course access if assignment is linked to lesson
    if assignment.lesson_id:
        lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
        if lesson:
            module = db.query(Module).filter(Module.id == lesson.module_id).first()
            if module and check_course_access(module.course_id, current_user, db):
                has_access = True
                print(f"Access granted via course: {module.course_id}")
    
    # Check group access if assignment is linked to group
    if assignment.group_id:
        from src.schemas.models import Group, GroupStudent
        
        if current_user.role == "admin":
            has_access = True
            print("Access granted via admin role")
        elif current_user.role == "teacher":
            group = db.query(Group).filter(Group.id == assignment.group_id).first()
            if group and group.teacher_id == current_user.id:
                has_access = True
                print(f"Access granted via teacher role for group: {assignment.group_id}")
        elif current_user.role == "student":
            group_member = db.query(GroupStudent).filter(
                GroupStudent.group_id == assignment.group_id,
                GroupStudent.student_id == current_user.id
            ).first()
            if group_member:
                has_access = True
                print(f"Access granted via student role for group: {assignment.group_id}")
    
    if not has_access:
        print(f"Access denied for user {current_user.id} to assignment {assignment_id}")
        raise HTTPException(status_code=403, detail="Access denied to this assignment")
    
    print(f"Access granted, returning assignment data")
    
    assignment_data = _to_enriched_schema(assignment)
    
    # Hide correct answers from students
    if current_user.role == "student":
        assignment_data.content = remove_correct_answers_from_content(assignment_data.content)
    
    return assignment_data

@router.put("/{assignment_id}", response_model=AssignmentSchema)
async def update_assignment(
    assignment_id: int,
    assignment_data: AssignmentCreateSchema,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Update assignment"""
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # Check permissions if assignment is linked to lesson
    if assignment.lesson_id:
        lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
        module = db.query(Module).filter(Module.id == lesson.module_id).first()
        course = db.query(Course).filter(Course.id == module.course_id).first()
        
        if current_user.role != "admin" and course.teacher_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied")
    
    # Check permissions if assignment is linked to group
    if assignment.group_id:
        from src.schemas.models import Group
        group = db.query(Group).filter(Group.id == assignment.group_id).first()
        if current_user.role != "admin":
            can_edit = (
                group.teacher_id == current_user.id or group.curator_id == current_user.id
            )
            if not can_edit:
                raise HTTPException(status_code=403, detail="Access denied")
    
    # Validate content
    validate_assignment_content(assignment_data.assignment_type, assignment_data.content)
    
    # Validate due date
    # if assignment_data.due_date and to_naive_utc(assignment_data.due_date) < datetime.now(timezone.utc).replace(tzinfo=None):
    #     raise HTTPException(status_code=400, detail="Due date cannot be in the past")
    
    # Resolve per-group mapping values for update flow.
    # AssignmentBuilder sends group-specific values in *_mapping fields.
    target_group_id = assignment_data.group_id or assignment.group_id

    resolved_event_id = assignment_data.event_id
    if target_group_id and assignment_data.event_mapping and target_group_id in assignment_data.event_mapping:
        resolved_event_id = assignment_data.event_mapping[target_group_id]

    resolved_due_date = assignment_data.due_date
    if target_group_id and assignment_data.due_date_mapping and target_group_id in assignment_data.due_date_mapping:
        resolved_due_date = assignment_data.due_date_mapping[target_group_id]

    resolved_lesson_number = assignment.lesson_number
    if target_group_id and assignment_data.lesson_number_mapping and target_group_id in assignment_data.lesson_number_mapping:
        resolved_lesson_number = assignment_data.lesson_number_mapping[target_group_id]

    # Update fields
    assignment.title = assignment_data.title
    assignment.description = assignment_data.description
    assignment.assignment_type = assignment_data.assignment_type
    assignment.content = json.dumps(assignment_data.content)
    assignment.correct_answers = json.dumps(assignment_data.correct_answers) if assignment_data.correct_answers else None
    assignment.max_score = assignment_data.max_score
    assignment.time_limit_minutes = assignment_data.time_limit_minutes
    assignment.due_date = resolved_due_date
    assignment.group_id = assignment_data.group_id
    assignment.event_id = EventService.resolve_event_id(db, resolved_event_id)
    assignment.lesson_number = resolved_lesson_number
    assignment.allowed_file_types = assignment_data.allowed_file_types
    assignment.max_file_size_mb = assignment_data.max_file_size_mb
    
    # Update late penalty settings
    assignment.late_penalty_enabled = assignment_data.late_penalty_enabled
    assignment.late_penalty_multiplier = assignment_data.late_penalty_multiplier
    
    db.commit()
    db.refresh(assignment)
    
    # Sync linked lessons for fast lookup
    sync_assignment_linked_lessons(assignment, db)
    
    return _to_enriched_schema(assignment)
    
    # Send email notification if significant changes (e.g. due date)
    try:
        # Collect emails
        student_emails = []
        course_title = "Course"
        
        if assignment.lesson_id:
            lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
            if lesson:
                module = db.query(Module).filter(Module.id == lesson.module_id).first()
                if module:
                    course = db.query(Course).filter(Course.id == module.course_id).first()
                    if course:
                        course_title = course.title
                        enrollments = db.query(Enrollment).filter(
                            Enrollment.course_id == course.id, 
                            Enrollment.is_active == True
                        ).all()
                        user_ids = [e.user_id for e in enrollments]
                        if user_ids:
                            users = db.query(UserInDB).filter(UserInDB.id.in_(user_ids)).all()
                            student_emails.extend([u.email for u in users])
                            
        if assignment.group_id:
             group_student_ids = db.query(GroupStudent.student_id).filter(
                GroupStudent.group_id == assignment.group_id
            ).all()
             ids = [bg[0] for bg in group_student_ids]
             if ids:
                users = db.query(UserInDB).filter(UserInDB.id.in_(ids)).all()
                student_emails.extend([u.email for u in users])
        
        student_emails = list(set(student_emails))
        
        if student_emails and assignment_data.due_date:
             due_str = assignment_data.due_date.strftime("%d %B %Y, %H:%M")
             send_homework_notification(
                student_emails,
                assignment.title,
                course_title,
                due_str,
                action="updated",
                assignment_id=assignment.id,
            )
            
    except Exception as e:
        print(f"Failed to send update notification: {e}")

    return result_assignment

@router.delete("/{assignment_id}")
async def delete_assignment(
    assignment_id: int,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Delete assignment"""
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # Check permissions
    if assignment.lesson_id:
        lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
        module = db.query(Module).filter(Module.id == lesson.module_id).first()
        course = db.query(Course).filter(Course.id == module.course_id).first()
        
        if current_user.role != "admin" and course.teacher_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied")
    
    # Soft delete
    assignment.is_active = False
    db.commit()
    
    return {"detail": "Assignment deleted successfully"}

# =============================================================================
# ASSIGNMENT SUBMISSIONS
# =============================================================================

@router.post("/{assignment_id}/submit", response_model=AssignmentSubmissionSchema)
async def submit_assignment(
    assignment_id: int,
    submission_data: SubmitAssignmentSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Submit assignment answers (students only)"""
    print(f"Submit assignment called: assignment_id={assignment_id}, user_id={current_user.id}")
    
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can submit assignments")
    
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    if not assignment_visible_to_student(current_user.id, assignment, db):
        raise HTTPException(status_code=403, detail="Access denied to this assignment")
    
    print(f"Assignment found: {assignment.title}, lesson_id={assignment.lesson_id}, group_id={assignment.group_id}")
    
    # Check if student has access to this assignment
    has_access = False
    
    # Check course access if assignment is linked to lesson
    if assignment.lesson_id:
        lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
        if lesson:
            module = db.query(Module).filter(Module.id == lesson.module_id).first()
            if module and check_course_access(module.course_id, current_user, db):
                has_access = True
                print(f"Access granted via course: {module.course_id}")
    
    # Check group access if assignment is linked to group
    if assignment.group_id:
        from src.schemas.models import GroupStudent
        group_member = db.query(GroupStudent).filter(
            GroupStudent.group_id == assignment.group_id,
            GroupStudent.student_id == current_user.id
        ).first()
        if group_member:
            has_access = True
            print(f"Access granted via group: {assignment.group_id}")
    
    if not has_access:
        raise HTTPException(status_code=403, detail="Access denied to this assignment")
    
    # Check if assignment is overdue (with extension support)
    is_late = False
    if assignment.due_date:
        # Check if student has an extension
        extension = db.query(AssignmentExtension).filter(
            AssignmentExtension.assignment_id == assignment_id,
            AssignmentExtension.student_id == current_user.id
        ).first()
        
        # Use extended deadline if exists, otherwise use original deadline
        effective_deadline = extension.extended_deadline if extension else assignment.due_date
        
        if to_naive_utc(effective_deadline) < datetime.now(timezone.utc).replace(tzinfo=None):
            is_late = True
            print(f"Submission is late! Effective deadline was: {effective_deadline}")
            # We allow late submissions but mark them as late
    
    # Check if already submitted (non-hidden submission exists)
    existing_submission = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.assignment_id == assignment_id,
        AssignmentSubmission.user_id == current_user.id,
        AssignmentSubmission.is_hidden == False  # Hidden submissions don't count - student can resubmit
    ).first()
    
    print(f"Checking for existing submissions: assignment_id={assignment_id}, user_id={current_user.id}")
    print(f"Existing submission found: {existing_submission is not None}")
    
    if existing_submission:
        print(f"Found existing submission: ID={existing_submission.id}, submitted_at={existing_submission.submitted_at}")
        print(f"Existing submission data: answers={existing_submission.answers}, file_url={existing_submission.file_url}")
        raise HTTPException(status_code=400, detail="Assignment already submitted")
    
    print(f"Creating submission with data: {submission_data}")
    print(f"Submission answers: {submission_data.answers}")
    print(f"Submission file_url: {submission_data.file_url}")
    print(f"Submission submitted_file_name: {submission_data.submitted_file_name}")
    
    # Auto-grade the assignment
    score = None
    # Skip auto-grading for multi_task assignments to allow manual grading
    if assignment.correct_answers and assignment.assignment_type != 'multi_task':
        try:
            correct_answers = json.loads(assignment.correct_answers)
            
            # For file_upload with answer_fields: compute auto-check, but don't auto-grade
            if assignment.assignment_type == 'file_upload' and correct_answers.get('answer_fields'):
                # Perform auto-check and store result in submission answers
                field_answers = submission_data.answers.get('field_answers', {})
                correct_fields = correct_answers.get('answer_fields', {})
                
                correct_count = 0
                total_count = len(correct_fields)
                check_details = {}
                
                for field_id, correct_val in correct_fields.items():
                    student_val = str(field_answers.get(str(field_id), '')).strip().lower()
                    correct_val_normalized = str(correct_val).strip().lower()
                    is_correct = student_val == correct_val_normalized
                    check_details[field_id] = is_correct
                    if is_correct:
                        correct_count += 1
                
                # Inject auto_check_result into submission answers
                submission_data.answers['auto_check_result'] = {
                    'correct_count': correct_count,
                    'total_count': total_count,
                    'details': check_details
                }
                print(f"Auto-check result: {correct_count}/{total_count} correct")
                # Don't auto-grade - teacher decides final score
                score = None
            else:
                score = check_assignment_answers(
                    assignment.assignment_type,
                    submission_data.answers,
                    correct_answers,
                    assignment.max_score
                )
            
            # Apply late penalty if enabled
            if score is not None and is_late and assignment.late_penalty_enabled:
                print(f"Applying late penalty: score {score} * {assignment.late_penalty_multiplier}")
                original_score = score
                score = int(score * assignment.late_penalty_multiplier)
                print(f"New score: {score}")
                
        except Exception as e:
            # If auto-grading fails, mark as ungraded
            score = None
            print(f"Auto-grading failed: {e}")
    
    # Handle auto-check for multi-task assignments
    if assignment.assignment_type == 'multi_task' and assignment.content:
        try:
            content = json.loads(assignment.content)
            tasks = content.get('tasks', [])
            
            # Frontend might send { "taskId": ... } (flat) or { "tasks": { "taskId": ... } } (nested)
            # We need to handle both cases
            task_answers = submission_data.answers.get('tasks')
            is_nested = True
            
            if task_answers is None:
                task_answers = submission_data.answers
                is_nested = False
            
            # Process each task for auto-check
            for task in tasks:
                task_id = task.get('id')
                task_type = task.get('task_type')
                task_content = task.get('content', {})
                answer_fields = task_content.get('answer_fields', [])
                
                # Only process tasks with answer_fields
                if answer_fields and task_id in task_answers:
                    task_answer = task_answers[task_id]
                    field_answers = task_answer.get('field_answers', {})
                    
                    correct_count = 0
                    total_count = len(answer_fields)
                    check_details = {}
                    
                    # Check each answer field
                    for field in answer_fields:
                        field_id = field.get('id')
                        correct_answer = field.get('correct_answer', '')
                        # Handle both string and numeric IDs in keys
                        student_answer = str(field_answers.get(field_id, field_answers.get(str(field_id), ''))).strip().lower()
                        correct_answer_normalized = str(correct_answer).strip().lower()
                        
                        is_correct = student_answer == correct_answer_normalized
                        check_details[field_id] = is_correct
                        if is_correct:
                            correct_count += 1
                    
                    # Store auto-check result for this task
                    # Note: modifying task_answer modifies the dict inside task_answers
                    if 'auto_check_result' not in task_answer:
                         task_answer['auto_check_result'] = {}
                         
                    task_answer['auto_check_result'] = {
                        'correct_count': correct_count,
                        'total_count': total_count,
                        'details': check_details
                    }
                    print(f"Multi-task auto-check for task {task_id}: {correct_count}/{total_count} correct")
            
            # If we had a nested 'tasks' key, ensure it's updated in the main dict
            if is_nested:
                submission_data.answers['tasks'] = task_answers
            
        except Exception as e:
            print(f"Auto-check for multi-task failed: {e}")
    
    # Create submission
    submission = AssignmentSubmission(
        assignment_id=assignment_id,
        user_id=current_user.id,
        answers=json.dumps(submission_data.answers),
        file_url=submission_data.file_url,
        submitted_file_name=submission_data.submitted_file_name,
        score=score,
        max_score=assignment.max_score,
        is_graded=score is not None,
        is_late=is_late,
        graded_at=datetime.now(timezone.utc).replace(tzinfo=None) if score is not None else None
    )
    
    db.add(submission)
    db.commit()
    db.refresh(submission)
    
    # Award points for completion
    try:
        award_points(db, current_user.id, 10, 'homework', f'Completed assignment: {assignment.title}')
    except Exception as e:
        print(f"Failed to award points: {e}") # Non-blocking error
    
    print(f"Submission created successfully: {submission.id}")
    
    # Update student progress (only if assignment is linked to a lesson)
    if assignment.lesson_id:
        update_student_progress(assignment, current_user.id, score, db)
    
    return AssignmentSubmissionSchema.from_orm(submission)



@router.get("/{assignment_id}/submissions", response_model=List[AssignmentSubmissionSchema])
async def get_assignment_submissions(
    assignment_id: int,
    user_id: Optional[int] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get submissions for assignment"""
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # Check permissions and determine which submissions to return
    allowed_student_ids = None  # None means all, list means filter by these IDs
    
    if current_user.role == "student":
        # Students can only see their own submissions
        user_id = current_user.id
    elif current_user.role == "admin":
        # Admins can see all submissions
        pass
    elif current_user.role in ["teacher", "curator"]:
        # Teachers/curators can see submissions from their students
        has_access = False
        
        if assignment.lesson_id:
            lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
            if lesson:
                module = db.query(Module).filter(Module.id == lesson.module_id).first()
                if module and check_course_access(module.course_id, current_user, db):
                    has_access = True
        
        if assignment.group_id:
            from src.schemas.models import Group
            group = db.query(Group).filter(Group.id == assignment.group_id).first()
            if group:
                if current_user.role == "teacher" and group.teacher_id == current_user.id:
                    has_access = True
                elif current_user.role == "curator" and group.curator_id == current_user.id:
                    has_access = True
        
        # For curators: filter to only show submissions from their group's students
        if current_user.role == "curator" and has_access:
            from src.schemas.models import Group, GroupStudent
            curator_groups = db.query(Group).filter(Group.curator_id == current_user.id).all()
            curator_group_ids = [g.id for g in curator_groups]
            
            student_ids = db.query(GroupStudent.student_id).filter(
                GroupStudent.group_id.in_(curator_group_ids)
            ).all()
            allowed_student_ids = [s[0] for s in student_ids]
        
        # For curators without direct access, check if students from their groups are in this assignment
        if current_user.role == "curator" and not has_access:
            from src.schemas.models import Group, GroupStudent
            curator_groups = db.query(Group).filter(Group.curator_id == current_user.id).all()
            curator_group_ids = [g.id for g in curator_groups]
            
            student_ids = db.query(GroupStudent.student_id).filter(
                GroupStudent.group_id.in_(curator_group_ids)
            ).all()
            allowed_student_ids = [s[0] for s in student_ids]
            
            if allowed_student_ids:
                has_access = True
        
        if not has_access:
            raise HTTPException(status_code=403, detail="Access denied")
    
    query = db.query(AssignmentSubmission).filter(AssignmentSubmission.assignment_id == assignment_id)
    
    if user_id:
        query = query.filter(AssignmentSubmission.user_id == user_id)
    elif allowed_student_ids is not None:
        query = query.filter(AssignmentSubmission.user_id.in_(allowed_student_ids))
    
    submissions = query.order_by(desc(AssignmentSubmission.submitted_at)).all()
    
    # Enhance submissions with user and grader names
    result = []
    for submission in submissions:
        submission_data = AssignmentSubmissionSchema.from_orm(submission)
        
        # Get user name
        user = db.query(UserInDB).filter(UserInDB.id == submission.user_id).first()
        if user:
            submission_data.user_name = user.name
        
        # Get grader name
        if submission.graded_by:
            grader = db.query(UserInDB).filter(UserInDB.id == submission.graded_by).first()
            if grader:
                submission_data.grader_name = grader.name
        
        result.append(submission_data)
    
    return result

@router.get("/{assignment_id}/submissions/{submission_id}", response_model=AssignmentSubmissionSchema)
async def get_submission(
    assignment_id: int,
    submission_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get a specific submission for an assignment"""
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    submission = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.id == submission_id,
        AssignmentSubmission.assignment_id == assignment_id
    ).first()
    
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    
    # Check permissions
    if current_user.role == "student":
        # Students can only see their own submissions
        if submission.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied")
    elif current_user.role == "admin":
        # Admins can see all submissions
        pass
    elif current_user.role in ["teacher", "curator"]:
        # Teachers/curators can see submissions from their students
        has_access = False
        
        if assignment.lesson_id:
            lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
            module = db.query(Module).filter(Module.id == lesson.module_id).first()
            
            if check_course_access(module.course_id, current_user, db):
                has_access = True
        
        if assignment.group_id:
            from src.schemas.models import Group
            group = db.query(Group).filter(Group.id == assignment.group_id).first()
            if group:
                # Teacher owns the group
                if current_user.role == "teacher" and group.teacher_id == current_user.id:
                    has_access = True
                # Curator owns the group
                if current_user.role == "curator" and group.curator_id == current_user.id:
                    has_access = True
        
        # Also check if the student is in any of the curator's groups
        if current_user.role == "curator" and not has_access:
            from src.schemas.models import Group, GroupStudent
            curator_groups = db.query(Group).filter(Group.curator_id == current_user.id).all()
            curator_group_ids = [g.id for g in curator_groups]
            
            # Check if the submission's student is in any of curator's groups
            student_in_curator_group = db.query(GroupStudent).filter(
                GroupStudent.group_id.in_(curator_group_ids),
                GroupStudent.student_id == submission.user_id
            ).first()
            
            if student_in_curator_group:
                has_access = True
        
        if not has_access:
            raise HTTPException(status_code=403, detail="Access denied")
    
    # Enhance submission with user and grader names
    submission_data = AssignmentSubmissionSchema.from_orm(submission)
    
    # Get user name
    user = db.query(UserInDB).filter(UserInDB.id == submission.user_id).first()
    if user:
        submission_data.user_name = user.name
    
    # Get grader name
    if submission.graded_by:
        grader = db.query(UserInDB).filter(UserInDB.id == submission.graded_by).first()
        if grader:
            submission_data.grader_name = grader.name
    
    return submission_data

@router.get("/submissions/my", response_model=List[AssignmentSubmissionSchema])
async def get_my_submissions(
    course_id: Optional[int] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, le=1000),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get current user's assignment submissions"""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can access this endpoint")
    
    query = db.query(AssignmentSubmission).filter(AssignmentSubmission.user_id == current_user.id)
    
    # Filter by course if specified
    if course_id:
        # Get assignment IDs for the course
        lesson_ids = db.query(Lesson.id).join(Module).filter(Module.course_id == course_id).subquery()
        assignment_ids = db.query(Assignment.id).filter(Assignment.lesson_id.in_(lesson_ids)).subquery()
        query = query.filter(AssignmentSubmission.assignment_id.in_(assignment_ids))
    
    submissions = query.order_by(desc(AssignmentSubmission.submitted_at)).offset(skip).limit(limit).all()
    return [AssignmentSubmissionSchema.from_orm(submission) for submission in submissions]

@router.get("/submissions/unseen-graded-count")
async def get_unseen_graded_count(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get count of graded submissions that student hasn't seen yet"""
    if current_user.role != "student":
        return {"count": 0}
    
    count = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.user_id == current_user.id,
        AssignmentSubmission.is_graded == True,
        AssignmentSubmission.seen_by_student == False
    ).count()
    
    return {"count": count}

@router.put("/submissions/{submission_id}/mark-seen")
async def mark_submission_seen(
    submission_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Mark a graded submission as seen by student"""
    submission = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.id == submission_id,
        AssignmentSubmission.user_id == current_user.id
    ).first()
    
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    
    submission.seen_by_student = True
    db.commit()
    
    # Notify student (self) to update badge
    try:
        from src.routes.socket_messages import emit_unseen_graded_update
        await emit_unseen_graded_update(current_user.id)
    except Exception as e:
        print(f"Failed to emit socket update: {e}")
    
    return {"success": True}

@router.put("/submissions/{submission_id}/allow-resubmit")
async def allow_resubmission(
    submission_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Allow a student to resubmit an assignment.
    This works by marking the current submission as hidden.
    """
    if current_user.role not in ["teacher", "admin"]:
        raise HTTPException(status_code=403, detail="Only teachers can allow resubmission")
    
    submission = db.query(AssignmentSubmission).filter(AssignmentSubmission.id == submission_id).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
        
    # Mark as hidden so it doesn't block new submissions
    submission.is_hidden = True
    submission.graded_at = None
    submission.is_graded = False # Optional: Reset graded status just in case
    
    db.commit()
    
    return {"message": "Resubmission allowed successfully"}
@router.put("/{assignment_id}/submissions/{submission_id}/grade", response_model=AssignmentSubmissionSchema)
async def grade_submission(
    assignment_id: int,
    submission_id: int,
    grade_data: GradeSubmissionSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Grade a submission (teachers, curators, and admins)"""
    # Check role
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Only teachers, curators, and admins can grade submissions")
    
    # Check if assignment exists
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # Check if submission exists
    submission = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.id == submission_id,
        AssignmentSubmission.assignment_id == assignment_id
    ).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    
    # Check permissions
    has_access = False
    
    if current_user.role == "admin":
        has_access = True
    
    # Check course access if assignment is linked to lesson
    if assignment.lesson_id and not has_access:
        lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
        if lesson:
            module = db.query(Module).filter(Module.id == lesson.module_id).first()
            if module and check_course_access(module.course_id, current_user, db):
                has_access = True
    
    # Check group access if assignment is linked to group
    if assignment.group_id and not has_access:
        from src.schemas.models import Group
        group = db.query(Group).filter(Group.id == assignment.group_id).first()
        if group:
            if current_user.role == "teacher" and group.teacher_id == current_user.id:
                has_access = True
            elif current_user.role == "curator" and group.curator_id == current_user.id:
                has_access = True
    
    # For curators: check if the student is in their group
    if current_user.role == "curator" and not has_access:
        from src.schemas.models import Group, GroupStudent
        curator_groups = db.query(Group).filter(Group.curator_id == current_user.id).all()
        curator_group_ids = [g.id for g in curator_groups]
        
        # Check if the student who submitted is in curator's group
        student_in_group = db.query(GroupStudent).filter(
            GroupStudent.group_id.in_(curator_group_ids),
            GroupStudent.student_id == submission.user_id
        ).first()
        
        if student_in_group:
            has_access = True
    
    if not has_access:
        raise HTTPException(status_code=403, detail="Access denied to this assignment")
    
    # Validate score
    if grade_data.score < 0 or grade_data.score > assignment.max_score:
        raise HTTPException(
            status_code=400, 
            detail=f"Score must be between 0 and {assignment.max_score}"
        )
    
    # Update submission
    submission.score = grade_data.score
    submission.feedback = grade_data.feedback
    submission.graded_by = current_user.id
    submission.is_graded = True
    submission.graded_at = datetime.now(timezone.utc).replace(tzinfo=None)
    
    db.commit()
    db.refresh(submission)
    
    # Notify student about graded submission
    try:
        from src.routes.socket_messages import emit_unseen_graded_update
        await emit_unseen_graded_update(submission.user_id)
    except Exception as e:
        print(f"Failed to emit socket update: {e}")
    
    # Send email notification to student
    try:
        from src.services.email_service import send_submission_graded_notification
        
        # Get student email
        student = db.query(UserInDB).filter(UserInDB.id == submission.user_id).first()
        if student and student.email:
            # Resolve course name
            course_name = "Course"
            
            # Try via lesson -> module -> course
            if assignment.lesson_id:
                lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
                if lesson:
                    module = db.query(Module).filter(Module.id == lesson.module_id).first()
                    if module:
                        course = db.query(Course).filter(Course.id == module.course_id).first()
                        if course:
                            course_name = course.title
            
            # Fallback: try via group -> CourseGroupAccess
            if course_name == "Course" and assignment.group_id:
                from src.schemas.models import CourseGroupAccess
                cga = db.query(CourseGroupAccess).filter(
                    CourseGroupAccess.group_id == assignment.group_id,
                    CourseGroupAccess.is_active == True
                ).first()
                if cga:
                    linked_course = db.query(Course).filter(Course.id == cga.course_id).first()
                    if linked_course:
                        course_name = linked_course.title
                
                # Final fallback: use group name
                if course_name == "Course":
                    group = db.query(Group).filter(Group.id == assignment.group_id).first()
                    if group:
                        course_name = group.name
            
            send_submission_graded_notification(
                student_email=student.email,
                assignment_title=assignment.title,
                course_name=course_name,
                score=grade_data.score,
                max_score=assignment.max_score,
                feedback=grade_data.feedback,
                assignment_id=assignment.id,
            )
    except Exception as e:
        print(f"Failed to send grading email notification: {e}")
    
    # Award points based on score
    try:
        # Calculate points based on score percentage
        # Award points proportional to the score received
        score_percentage = (grade_data.score / assignment.max_score) * 100 if assignment.max_score > 0 else 0
        
        # Base points for completing the assignment (minimum)
        base_points = 10
        
        # Bonus points based on score (up to 40 more points for perfect score)
        bonus_points = int((score_percentage / 100) * 40)
        
        total_points = base_points + bonus_points
        
        award_points(
            db, 
            submission.user_id, 
            total_points, 
            'assignment', 
            f'Graded assignment: {assignment.title} ({grade_data.score}/{assignment.max_score})'
        )
    except Exception as e:
        print(f"Failed to award points: {e}")
    
    # Enhance submission with names
    submission_data = AssignmentSubmissionSchema.from_orm(submission)
    
    # Get user name
    user = db.query(UserInDB).filter(UserInDB.id == submission.user_id).first()
    if user:
        submission_data.user_name = user.name
    
    # Get grader name
    submission_data.grader_name = current_user.name
    
    return submission_data

@router.patch("/{assignment_id}/submissions/{submission_id}/toggle-visibility", response_model=AssignmentSubmissionSchema)
async def toggle_submission_visibility(
    assignment_id: int,
    submission_id: int,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Toggle submission visibility (hide/unhide from students)"""
    # Check if assignment exists
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # Check if submission exists
    submission = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.id == submission_id,
        AssignmentSubmission.assignment_id == assignment_id
    ).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    
    # Check permissions
    has_access = False
    
    # Check course access if assignment is linked to lesson
    if assignment.lesson_id:
        lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
        if lesson:
            module = db.query(Module).filter(Module.id == lesson.module_id).first()
            if module and check_course_access(module.course_id, current_user, db):
                has_access = True
    
    # Check group access if assignment is linked to group
    if assignment.group_id:
        from src.schemas.models import Group
        group = db.query(Group).filter(Group.id == assignment.group_id).first()
        if current_user.role == "admin" or (group and group.teacher_id == current_user.id):
            has_access = True
    
    if not has_access:
        raise HTTPException(status_code=403, detail="Access denied to this assignment")
    
    # Toggle visibility
    submission.is_hidden = not submission.is_hidden
    
    db.commit()
    db.refresh(submission)
    
    # Enhance submission with names
    submission_data = AssignmentSubmissionSchema.from_orm(submission)
    
    # Get user name
    user = db.query(UserInDB).filter(UserInDB.id == submission.user_id).first()
    if user:
        submission_data.user_name = user.name
    
    # Get grader name
    if submission.graded_by:
        grader = db.query(UserInDB).filter(UserInDB.id == submission.graded_by).first()
        if grader:
            submission_data.grader_name = grader.name
    
    return submission_data

# =============================================================================
# ASSIGNMENT STATUS
# =============================================================================

@router.get("/{assignment_id}/student-progress", response_model=Dict[str, Any])
async def get_assignment_student_progress(
    assignment_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get student progress for an assignment (teachers, admins, and curators)"""
    if current_user.role not in ["teacher", "admin", "curator", "head_curator"]:
        raise HTTPException(status_code=403, detail="Only teachers, admins, and curators can access student progress")
    
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # Check permissions
    has_access = False
    
    # Check course access if assignment is linked to lesson
    if assignment.lesson_id:
        lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
        if lesson:
            module = db.query(Module).filter(Module.id == lesson.module_id).first()
            if module:
                 # Teacher/Admin course access check
                if current_user.role in ["teacher", "admin"]:
                    if check_course_access(module.course_id, current_user, db):
                        has_access = True
                # Curator access check: Must manage a group in this course
                elif current_user.role == "curator":
                    from src.schemas.models import Group
                    # Check if curator has any groups in this course
                    # We can iterate headers or do a query
                    curator_groups_count = db.query(Group).filter(
                        Group.course_id == module.course_id,
                        Group.curator_id == current_user.id
                    ).count()
                    if curator_groups_count > 0:
                        has_access = True

    
    # Check group access if assignment is linked to group
    if assignment.group_id:
        from src.schemas.models import Group
        group = db.query(Group).filter(Group.id == assignment.group_id).first()
        
        if current_user.role == "admin":
             has_access = True
        elif current_user.role == "teacher" and group and group.teacher_id == current_user.id:
             has_access = True
        elif current_user.role == "curator" and group and group.curator_id == current_user.id:
             has_access = True
    
    if not has_access:
        raise HTTPException(status_code=403, detail="Access denied to this assignment")
    
    # Get students who should have access to this assignment
    students = []
    assignment_sources = {}  # Track how each student got access to the assignment
    
    if assignment.lesson_id:
        # Course-based assignment - get enrolled students
        lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
        if lesson:
            module = db.query(Module).filter(Module.id == lesson.module_id).first()
            if module:
                enrolled_students = db.query(UserInDB).join(Enrollment).filter(
                    Enrollment.course_id == module.course_id,
                    Enrollment.is_active == True,
                    UserInDB.role == "student"
                ).all()
                
                for student in enrolled_students:
                    students.append(student)
                    assignment_sources[student.id] = "course"
        
    if assignment.group_id:
        # Group-based assignment - get group members
        from src.schemas.models import GroupStudent
        group_students = db.query(UserInDB).join(GroupStudent).filter(
            GroupStudent.group_id == assignment.group_id,
            UserInDB.role == "student"
        ).all()
        
        for student in group_students:
            if student.id in assignment_sources:
                # Student is both in course and group
                assignment_sources[student.id] = "both"
            else:
                students.append(student)
                assignment_sources[student.id] = "group"
    
    # If no lesson_id or group_id, this might be a standalone assignment
    # In this case, we need to determine who should see it
    if not assignment.lesson_id and not assignment.group_id:
        # For standalone assignments, we might need to check if there's a specific assignment assignment table
        # For now, we'll return empty list as these assignments need explicit assignment
        pass
    
    # If curator, filter students to only those in their groups
    if current_user.role == "curator":
        from src.schemas.models import Group, GroupStudent
        
        # Get all student IDs in groups managed by this curator
        curator_student_ids = [
            res[0] for res in db.query(GroupStudent.student_id).join(Group).filter(
                Group.curator_id == current_user.id
            ).all()
        ]
        curator_student_set = set(curator_student_ids)
        
        # Filter the students list
        students = [s for s in students if s.id in curator_student_set]

    # Remove duplicates (in case a student is both enrolled and in group)
    unique_students = []
    seen_ids = set()
    for student in students:
        if student.id not in seen_ids:
            unique_students.append(student)
            seen_ids.add(student.id)
    
    # Get all submissions for this assignment
    submissions = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.assignment_id == assignment_id
    ).all()
    
    # Create submission lookup
    submission_lookup = {sub.user_id: sub for sub in submissions}
    
    # Get all extensions for this assignment
    extensions = db.query(AssignmentExtension).filter(
        AssignmentExtension.assignment_id == assignment_id
    ).all()
    
    # Create extension lookup
    extension_lookup = {ext.student_id: ext for ext in extensions}
    
    # Build student progress data
    student_progress = []
    for student in unique_students:
        submission = submission_lookup.get(student.id)
        extension = extension_lookup.get(student.id)
        
        # Determine effective deadline (extension or original)
        effective_deadline = extension.extended_deadline if extension else assignment.due_date
        
        # Determine status
        status = "not_submitted"
        score = None
        submitted_at = None
        graded_at = None
        is_overdue = False
        
        if submission:
            if submission.is_graded and submission.score is not None:
                status = "graded"
                score = submission.score
                graded_at = submission.graded_at
            else:
                status = "submitted"
            submitted_at = submission.submitted_at
        else:
            # Check if overdue using effective deadline
            if effective_deadline and to_naive_utc(effective_deadline) < datetime.now(timezone.utc).replace(tzinfo=None):
                status = "overdue"
                is_overdue = True
        
        # Get assignment source information
        assignment_source = assignment_sources.get(student.id, "unknown")
        source_display = {
            "course": "Course Enrollment",
            "group": "Group Membership", 
            "both": "Course & Group",
            "unknown": "Unknown"
        }.get(assignment_source, "Unknown")
        
        student_progress.append({
            "id": student.id,
            "name": student.name,
            "email": student.email,
            "status": status,
            "submission_id": submission.id if submission else None,
            "score": score,
            "max_score": assignment.max_score,
            "submitted_at": submitted_at,
            "graded_at": graded_at,
            "is_overdue": is_overdue,
            "is_late": submission.is_late if submission else False,
            "is_hidden": submission.is_hidden if submission else False,
            "assignment_source": assignment_source,
            "source_display": source_display
        })
    
    # Sort by name
    student_progress.sort(key=lambda x: x["name"])
    
    # Calculate summary stats
    total_students = len(student_progress)
    not_submitted = len([s for s in student_progress if s["status"] == "not_submitted"])
    submitted = len([s for s in student_progress if s["status"] == "submitted"])
    graded = len([s for s in student_progress if s["status"] == "graded"])
    overdue = len([s for s in student_progress if s["status"] == "overdue"])
    
    # Calculate source breakdown
    source_breakdown = {}
    for student in student_progress:
        source = student["assignment_source"]
        source_breakdown[source] = source_breakdown.get(source, 0) + 1
    
    return {
        "assignment": {
            "id": assignment.id,
            "title": assignment.title,
            "description": assignment.description,
            "due_date": assignment.due_date,
            "max_score": assignment.max_score,
            "lesson_id": assignment.lesson_id,
            "group_id": assignment.group_id,
            "assignment_type": assignment.assignment_type,
            "assignment_type": assignment.assignment_type,
            "content": json.loads(assignment.content) if isinstance(assignment.content, str) else assignment.content,
            "late_penalty_enabled": assignment.late_penalty_enabled,
            "late_penalty_multiplier": assignment.late_penalty_multiplier
        },
        "students": student_progress,
        "summary": {
            "total_students": total_students,
            "not_submitted": not_submitted,
            "submitted": submitted,
            "graded": graded,
            "overdue": overdue
        },
        "source_breakdown": source_breakdown
    }

@router.get("/{assignment_id}/status", response_model=Dict[str, Any])
async def get_assignment_status_for_student(
    assignment_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get assignment status for current student"""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can access assignment status")
    
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    if not assignment_visible_to_student(current_user.id, assignment, db):
        raise HTTPException(status_code=403, detail="Access denied to this assignment")
    
    # Check if student has access to this assignment
    has_access = False
    
    # Check course access if assignment is linked to lesson
    if assignment.lesson_id:
        lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
        if lesson:
            module = db.query(Module).filter(Module.id == lesson.module_id).first()
            if module and check_course_access(module.course_id, current_user, db):
                has_access = True
    
    # Check group access if assignment is linked to group
    if assignment.group_id:
        from src.schemas.models import GroupStudent
        group_member = db.query(GroupStudent).filter(
            GroupStudent.group_id == assignment.group_id,
            GroupStudent.student_id == current_user.id
        ).first()
        if group_member:
            has_access = True
    
    if not has_access:
        raise HTTPException(status_code=403, detail="Access denied to this assignment")
    
    # Get existing submission (exclude hidden submissions - student should not see them)
    submission = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.assignment_id == assignment_id,
        AssignmentSubmission.user_id == current_user.id,
        AssignmentSubmission.is_hidden == False  # Don't show hidden submissions to students
    ).first()
    
    # Check if student has an extension
    extension = db.query(AssignmentExtension).filter(
        AssignmentExtension.assignment_id == assignment_id,
        AssignmentExtension.student_id == current_user.id
    ).first()
    
    # Determine effective deadline
    effective_deadline = extension.extended_deadline if extension else assignment.due_date
    
    # Determine status
    status = "not_started"
    attempts_left = 1  # async default to 1 attempt
    late = False
    
    if submission:
        if submission.is_graded:
            status = "graded"
        else:
            status = "submitted"
        attempts_left = 0  # Already submitted
    else:
        # Check if assignment is overdue using effective deadline
        if effective_deadline and effective_deadline < datetime.now(timezone.utc).replace(tzinfo=None):
            late = True
            status = "overdue"
    
    response_data = {
        "status": status,
        "attempts_left": attempts_left,
        "late": late,
        "due_date": assignment.due_date,
        "submitted_at": submission.submitted_at if submission else None,
        "score": submission.score if submission else None,
        "max_score": assignment.max_score,
        # Include submission details for displaying student's answers
        "submission_id": submission.id if submission else None,
        "answers": json.loads(submission.answers) if submission and submission.answers else None,
        "file_url": submission.file_url if submission else None,
        "submitted_file_name": submission.submitted_file_name if submission else None,
        "feedback": submission.feedback if submission else None
    }
    
    # Add extension info if exists
    if extension:
        response_data["extended_deadline"] = extension.extended_deadline
    
    return response_data

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def validate_assignment_content(assignment_type: str, content: Dict[str, Any]):
    """Validate assignment content based on type"""
    required_fields = {
        "single_choice": ["question", "options"],
        "multiple_choice": ["question", "options"],
        "picture_choice": ["question", "images"],
        "fill_in_blanks": ["text_with_blanks"],
        "matching": ["left_items", "right_items"],
        "matching_text": ["items_to_match"],
        "free_text": ["question"],
        "file_upload": ["question", "allowed_file_types"],
        "multi_task": ["tasks"]  # Multi-task assignment
    }
    
    if assignment_type not in required_fields:
        raise HTTPException(status_code=400, detail=f"Unsupported assignment type: {assignment_type}")
    
    # Validate required fields for the assignment type
    for field in required_fields[assignment_type]:
        if field not in content:
            raise HTTPException(status_code=400, detail=f"Missing required field '{field}' for {assignment_type}")
    
    # Additional validation for multi-task assignments
    if assignment_type == "multi_task":
        tasks = content.get("tasks", [])
        if not isinstance(tasks, list) or len(tasks) == 0:
            raise HTTPException(status_code=400, detail="multi_task assignment must have at least one task")
        
        # Validate each task
        valid_task_types = ["course_unit", "file_task", "text_task", "link_task", "pdf_text_task"]
        for i, task in enumerate(tasks):
            # Check required task fields
            if not isinstance(task, dict):
                raise HTTPException(status_code=400, detail=f"Task {i+1} must be an object")
            
            required_task_fields = ["id", "task_type", "title", "order_index", "points", "content"]
            for field in required_task_fields:
                if field not in task:
                    raise HTTPException(status_code=400, detail=f"Task {i+1} missing required field '{field}'")
            
            # Validate task type
            task_type = task.get("task_type")
            if task_type not in valid_task_types:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Task {i+1} has invalid task_type '{task_type}'. Must be one of: {', '.join(valid_task_types)}"
                )
            
            # Validate task-specific content
            task_content = task.get("content", {})
            if task_type == "course_unit":
                if "course_id" not in task_content or "lesson_ids" not in task_content:
                    raise HTTPException(
                        status_code=400, 
                        detail=f"Task {i+1} (course_unit) must have 'course_id' and 'lesson_ids' in content"
                    )
            elif task_type == "file_task":
                if "question" not in task_content:
                    raise HTTPException(
                        status_code=400, 
                        detail=f"Task {i+1} (file_task) must have 'question' in content"
                    )
            elif task_type == "text_task":
                if "question" not in task_content:
                    raise HTTPException(
                        status_code=400, 
                        detail=f"Task {i+1} (text_task) must have 'question' in content"
                    )
            elif task_type == "link_task":
                if "url" not in task_content:
                    raise HTTPException(
                        status_code=400, 
                        detail=f"Task {i+1} (link_task) must have 'url' in content"
                    )
            elif task_type == "pdf_text_task":
                if "question" not in task_content:
                    raise HTTPException(
                        status_code=400, 
                        detail=f"Task {i+1} (pdf_text_task) must have 'question' in content"
                    )


def remove_correct_answers_from_content(content: Dict[str, Any]) -> Dict[str, Any]:
    """Remove correct answers from content when showing to students"""
    # Make a copy to avoid modifying original
    clean_content = content.copy()
    
    # Remove fields that might contain answers
    fields_to_remove = ["correct_answer", "correct_answers", "answer_key"]
    for field in fields_to_remove:
        if field in clean_content:
            del clean_content[field]
    
    # Handle multi-task assignments - remove answers from each task
    if "tasks" in clean_content and isinstance(clean_content["tasks"], list):
        clean_tasks = []
        for task in clean_content["tasks"]:
            if isinstance(task, dict):
                clean_task = task.copy()
                # Remove answer fields from task content
                if "content" in clean_task and isinstance(clean_task["content"], dict):
                    task_content = clean_task["content"].copy()
                    for field in fields_to_remove:
                        if field in task_content:
                            del task_content[field]
                    clean_task["content"] = task_content
                clean_tasks.append(clean_task)
            else:
                clean_tasks.append(task)
        clean_content["tasks"] = clean_tasks
    
    return clean_content

def update_student_progress(assignment: Assignment, user_id: int, score: Optional[int], db: Session):
    """Update student progress after assignment submission"""
    if not assignment.lesson_id:
        return
    
    lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    
    # Find or create progress record
    from src.schemas.models import StudentProgress
    progress = db.query(StudentProgress).filter(
        StudentProgress.user_id == user_id,
        StudentProgress.course_id == module.course_id,
        StudentProgress.assignment_id == assignment.id
    ).first()
    
    if not progress:
        progress = StudentProgress(
            user_id=user_id,
            course_id=module.course_id,
            lesson_id=assignment.lesson_id,
            assignment_id=assignment.id,
            status="completed" if score is not None else "in_progress",
            completion_percentage=100 if score is not None and score >= (assignment.max_score * 0.6) else 50,
            last_accessed=datetime.now(timezone.utc).replace(tzinfo=None),
            completed_at=datetime.now(timezone.utc).replace(tzinfo=None) if score is not None else None
        )
        db.add(progress)
    else:
        progress.status = "completed" if score is not None else "in_progress"
        progress.completion_percentage = 100 if score is not None and score >= (assignment.max_score * 0.6) else 50
        progress.last_accessed = datetime.now(timezone.utc).replace(tzinfo=None)
        if score is not None:
            progress.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    
    db.commit()

# =============================================================================
# ASSIGNMENT TYPES INFO
# =============================================================================

@router.get("/types")
async def get_assignment_types():
    """Get supported assignment types and their schemas"""
    return {
        "supported_types": [
            {
                "type": "single_choice",
                "name": "Single Choice",
                "description": "Выбор одного правильного ответа из нескольких вариантов",
                "schema": {
                    "question": "str",
                    "options": ["str"],
                    "correct_answer": "int (index)"
                }
            },
            {
                "type": "multiple_choice",
                "name": "Multiple Choice", 
                "description": "Выбор нескольких правильных ответов",
                "schema": {
                    "question": "str",
                    "options": ["str"],
                    "correct_answers": ["int (indices)"]
                }
            },
            {
                "type": "picture_choice",
                "name": "Picture Choice",
                "description": "Выбор из изображений",
                "schema": {
                    "question": "str",
                    "images": [{"url": "str", "caption": "str"}],
                    "correct_answer": "int (index)"
                }
            },
            {
                "type": "fill_in_blanks",
                "name": "Fill in the Blanks",
                "description": "Заполнение пропусков в тексте",
                "schema": {
                    "text_with_blanks": "str (with _____ for blanks)",
                    "correct_answers": ["str"]
                }
            },
            {
                "type": "matching",
                "name": "Matching",
                "description": "Сопоставление элементов",
                "schema": {
                    "left_items": ["str"],
                    "right_items": ["str"],
                    "correct_matches": {"left_index": "right_index"}
                }
            },
            {
                "type": "matching_text",
                "name": "Matching Text",
                "description": "Сопоставление текстовых элементов",
                "schema": {
                    "items_to_match": [{"term": "str", "async definition": "str"}],
                    "shuffle": "bool"
                }
            },
            {
                "type": "free_text",
                "name": "Free Text",
                "description": "Свободный ответ текстом",
                "schema": {
                    "question": "str",
                    "max_length": "int (optional)",
                    "keywords": ["str (for auto-checking)"]
                }
            },
            {
                "type": "file_upload",
                "name": "File Upload",
                "description": "Загрузка файла",
                "schema": {
                    "question": "str",
                    "allowed_file_types": ["str"],
                    "max_file_size_mb": "int"
                }
            },
            {
                "type": "multi_task",
                "name": "Multi-Task Homework",
                "description": "Домашнее задание с несколькими задачами разных типов",
                "schema": {
                    "tasks": [{
                        "id": "str",
                        "task_type": "str (course_unit, file_task, text_task, link_task, pdf_text_task)",
                        "title": "str",
                        "description": "str (optional)",
                        "order_index": "int",
                        "points": "int",
                        "is_optional": "bool (optional, default false) - marks task as bonus/optional",
                        "content": "dict (task-specific)"
                    }],
                    "total_points": "int",
                    "required_points": "int (sum of non-optional task points)",
                    "bonus_points": "int (sum of optional task points)",
                    "instructions": "str (optional)"
                }
            }
        ]
    }

def sync_assignment_linked_lessons(assignment: Assignment, db: Session):
    """
    Synchronizes the assignment_linked_lessons table for an assignment.
    Extracts lesson IDs from multi_task content or the lesson_id field.
    """
    from src.schemas.models import AssignmentLinkedLesson
    
    # 1. Clear existing links
    db.query(AssignmentLinkedLesson).filter(
        AssignmentLinkedLesson.assignment_id == assignment.id
    ).delete()
    
    linked_lesson_ids = set()
    
    # 2. Add direct lesson_id if present
    if assignment.lesson_id:
        linked_lesson_ids.add(assignment.lesson_id)
        
    # 3. Add lessons from multi_task content
    if assignment.assignment_type == 'multi_task' and assignment.content:
        try:
            content = json.loads(assignment.content) if isinstance(assignment.content, str) else assignment.content
            tasks = content.get('tasks', [])
            for task in tasks:
                if task.get('task_type') == 'course_unit':
                    task_content = task.get('content', {})
                    lesson_ids = task_content.get('lesson_ids', [])
                    for lid in lesson_ids:
                        if isinstance(lid, int):
                            linked_lesson_ids.add(lid)
        except Exception as e:
            print(f"Error parsing assignment {assignment.id} content: {e}")
            
    # 4. Create new links
    for lid in linked_lesson_ids:
        link = AssignmentLinkedLesson(assignment_id=assignment.id, lesson_id=lid)
        db.add(link)
        
    db.commit()

# =============================================================================
# ASSIGNMENT EXTENSIONS (DEADLINE MANAGEMENT)
# =============================================================================

@router.post("/{assignment_id}/extensions", response_model=AssignmentExtensionSchema)
async def grant_extension(
    assignment_id: int,
    extension_data: GrantExtensionSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Grant deadline extension to a student (teacher/admin only)"""
    if current_user.role not in ["teacher", "admin"]:
        raise HTTPException(status_code=403, detail="Only teachers and admins can grant extensions")
    
    # Verify assignment exists
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # Verify student exists and is actually a student
    student = db.query(UserInDB).filter(UserInDB.id == extension_data.student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if student.role != "student":
        raise HTTPException(status_code=400, detail="Extensions can only be granted to students")
    
    # Check if extension already exists
    existing_extension = db.query(AssignmentExtension).filter(
        AssignmentExtension.assignment_id == assignment_id,
        AssignmentExtension.student_id == extension_data.student_id
    ).first()
    
    if existing_extension:
        # Update existing extension
        existing_extension.extended_deadline = extension_data.extended_deadline
        existing_extension.reason = extension_data.reason
        existing_extension.granted_by = current_user.id
        db.commit()
        db.refresh(existing_extension)
        
        # Add names for response
        result = AssignmentExtensionSchema.model_validate(existing_extension)
        result.student_name = student.name
        result.granter_name = current_user.name
        return result
    
    # Create new extension
    extension = AssignmentExtension(
        assignment_id=assignment_id,
        student_id=extension_data.student_id,
        extended_deadline=extension_data.extended_deadline,
        reason=extension_data.reason,
        granted_by=current_user.id
    )
    db.add(extension)
    db.commit()
    db.refresh(extension)
    
    # Add names for response
    result = AssignmentExtensionSchema.model_validate(extension)
    result.student_name = student.name
    result.granter_name = current_user.name
    return result

@router.get("/{assignment_id}/extensions", response_model=List[AssignmentExtensionSchema])
async def get_assignment_extensions(
    assignment_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get all extensions for an assignment (teacher/admin only)"""
    if current_user.role not in ["teacher", "admin"]:
        raise HTTPException(status_code=403, detail="Only teachers and admins can view extensions")
    
    # Verify assignment exists
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    extensions = db.query(AssignmentExtension).filter(
        AssignmentExtension.assignment_id == assignment_id
    ).all()
    
    # Add student and granter names
    result = []
    for ext in extensions:
        student = db.query(UserInDB).filter(UserInDB.id == ext.student_id).first()
        granter = db.query(UserInDB).filter(UserInDB.id == ext.granted_by).first()
        
        ext_schema = AssignmentExtensionSchema.model_validate(ext)
        ext_schema.student_name = student.name if student else None
        ext_schema.granter_name = granter.name if granter else None
        result.append(ext_schema)
    
    return result

@router.delete("/{assignment_id}/extensions/{student_id}")
async def revoke_extension(
    assignment_id: int,
    student_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Revoke deadline extension for a student (teacher/admin only)"""
    if current_user.role not in ["teacher", "admin"]:
        raise HTTPException(status_code=403, detail="Only teachers and admins can revoke extensions")
    
    extension = db.query(AssignmentExtension).filter(
        AssignmentExtension.assignment_id == assignment_id,
        AssignmentExtension.student_id == student_id
    ).first()
    
    if not extension:
        raise HTTPException(status_code=404, detail="Extension not found")
    
    db.delete(extension)
    db.commit()
    
    return {"message": "Extension revoked successfully"}

@router.get("/{assignment_id}/my-extension", response_model=Optional[AssignmentExtensionSchema])
async def get_my_extension(
    assignment_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get current user's extension for an assignment (students can check their own)"""
    extension = db.query(AssignmentExtension).filter(
        AssignmentExtension.assignment_id == assignment_id,
        AssignmentExtension.student_id == current_user.id
    ).first()
    
    if not extension:
        return None
    
    # Add names for response
    granter = db.query(UserInDB).filter(UserInDB.id == extension.granted_by).first()
    result = AssignmentExtensionSchema.model_validate(extension)
    result.student_name = current_user.name
    result.granter_name = granter.name if granter else None
    
    return result

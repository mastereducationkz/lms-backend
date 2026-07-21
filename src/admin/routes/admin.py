from fastapi import APIRouter, Depends, HTTPException, Query, status, BackgroundTasks
from fastapi.responses import Response
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, desc
from typing import List, Optional, Literal
from pydantic import BaseModel, EmailStr
from datetime import datetime, timedelta, time, date

from src.config import get_db
from src.schemas.models import (
    UserInDB, UserSchema, Group, GroupSchema, GroupStudent, Course, Module, Enrollment,
    StudentProgress, Assignment, AssignmentSubmission, AssignmentExtension, Event, EventGroup, EventParticipant,
    EventSchema, CreateEventRequest, UpdateEventRequest, EventGroupSchema, EventParticipantSchema,
    StepProgress, Step, Lesson, LessonSchedule, CourseGroupAccess, CourseHeadTeacher,
    QuestionErrorReport, LessonRequest, ParentStudent,
)
from src.utils.auth_utils import hash_password
from src.services.email_service import send_invite_email, send_password_changed_email
from src.utils.permissions import require_admin, require_teacher_or_admin_for_groups, require_teacher_curator_or_admin, require_admin_or_head_curator
from src.services.group_completion_service import sync_groups_over_status
from src.services.cache_service import cached
import secrets
import string
import logging
from datetime import timezone as _tz

logger = logging.getLogger(__name__)


def _sync_group_students(db: Session, group_id: int, desired_student_ids: List[int]) -> None:
    """Add/remove group members by diff; skip when unchanged."""
    desired_ids = set(desired_student_ids)
    current_ids = {
        row[0]
        for row in db.query(GroupStudent.student_id).filter(
            GroupStudent.group_id == group_id
        ).all()
    }
    if desired_ids == current_ids:
        return

    to_remove = current_ids - desired_ids
    to_add = desired_ids - current_ids

    if to_remove:
        db.query(GroupStudent).filter(
            GroupStudent.group_id == group_id,
            GroupStudent.student_id.in_(to_remove),
        ).delete(synchronize_session=False)

    for student_id in to_add:
        student = db.query(UserInDB).filter(
            UserInDB.id == student_id,
            UserInDB.role == "student",
            UserInDB.is_active == True,
        ).first()
        if student:
            db.add(GroupStudent(group_id=group_id, student_id=student_id))


def _sync_student_groups(db: Session, user_id: int, desired_group_ids: List[int]) -> set:
    """Add/remove a student's group memberships by diff; skip when unchanged.

    Submissions are tied to assignment_id and are never moved or deleted here —
    old group homework stays in the DB; new group gets its own assignments.

    Returns the set of affected group ids (added or removed) so the caller can
    resync those groups' chat channels; empty set when nothing changed.
    """
    desired_ids = set(desired_group_ids)
    current_ids = {
        row[0]
        for row in db.query(GroupStudent.group_id).filter(
            GroupStudent.student_id == user_id
        ).all()
    }
    if desired_ids == current_ids:
        return set()

    to_remove = current_ids - desired_ids
    to_add = desired_ids - current_ids

    if to_remove:
        db.query(GroupStudent).filter(
            GroupStudent.student_id == user_id,
            GroupStudent.group_id.in_(to_remove),
        ).delete(synchronize_session=False)

    for group_id in to_add:
        group = db.query(Group).filter(Group.id == group_id).first()
        if group:
            db.add(GroupStudent(group_id=group_id, student_id=user_id))

    return to_remove | to_add


router = APIRouter()

# Pydantic models for admin operations
class CreateUserRequest(BaseModel):
    email: EmailStr
    name: str
    password: Optional[str] = None  # If not provided, will be auto-generated
    role: str = "student"  # student, teacher, head_curator, curator, admin, head_teacher
    student_id: Optional[str] = None
    is_active: bool = True
    group_ids: Optional[List[int]] = None  # Multiple groups for students
    course_ids: Optional[List[int]] = None  # Courses for head teachers
    child_ids: Optional[List[int]] = None  # Student user ids to link when role == "parent"
    send_invites: bool = True  # Email an invite (with credentials) to created students

class LinkChildrenRequest(BaseModel):
    child_ids: List[int]

class BulkCreateUsersRequest(BaseModel):
    users: List[CreateUserRequest]
    notify_users: bool = False  # For future email notifications
    send_invites: bool = True  # Email invites to created students
    # Note: group_ids are in each CreateUserRequest

class BulkCreateUsersFromTextRequest(BaseModel):
    """
    Request model for bulk creating users from pasted text (TSV/CSV format).
    Expected format per line: name\tphone\tmonths\tdate\temail
    Example: Ибрагим Саида Асланкызы\t87756486372\tноябрь, декабрь\tDecember 3 2025\tibragim.saida@mail.ru
    """
    text: str  # Raw text with tab-separated values
    group_ids: Optional[List[int]] = None  # Groups to assign all created students to
    role: str = "student"  # Default role
    generate_passwords: bool = True  # Generate passwords for users
    send_invites: bool = True  # Email invites to created students

class UpdateUserRequest(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    role: Optional[str] = None
    student_id: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None
    group_ids: Optional[List[int]] = None  # Update user's groups
    course_ids: Optional[List[int]] = None  # Update head teacher's courses
    is_analytics_hidden: Optional[bool] = None  # Hide curator from analytics/dashboard/leaderboard

class CreateUserResponse(BaseModel):
    user: UserSchema
    generated_password: Optional[str] = None

class CreateAdminRequest(BaseModel):
    email: EmailStr
    name: str
    password: Optional[str] = None  # If not provided, will be auto-generated
    is_active: bool = True

class CreateAdminResponse(BaseModel):
    admin: UserSchema
    generated_password: Optional[str] = None

class BulkCreateResponse(BaseModel):
    created_users: List[CreateUserResponse]
    failed_users: List[dict]  # {email, error}

class AdminStatsResponse(BaseModel):
    total_users: int
    total_students: int
    total_teachers: int
    total_curators: int
    total_courses: int
    total_active_enrollments: int
    recent_registrations: int  # Last 7 days
    pending_homework_to_grade: int = 0
    students_without_assignment_zero: int = 0
    open_question_reports: int = 0
    pending_lesson_requests: int = 0
    events_in_next_7_days: int = 0
    teacher_active_last_7_days: int = 0
    teacher_active_last_30_days: int = 0
    teachers_who_graded_last_7_days: int = 0
    homework_graded_last_7_days: int = 0
    avg_homework_graded_per_active_teacher_last_7_days: float = 0.0

class StudentProgressSummary(BaseModel):
    user_id: int
    name: str
    email: str
    student_id: Optional[str]
    group_name: Optional[str]
    total_courses: int
    completed_courses: int
    average_progress: float
    total_study_time: int
    last_activity: Optional[datetime]

class CreateGroupRequest(BaseModel):
    name: str
    description: Optional[str] = None
    teacher_id: Optional[int] = None
    curator_id: Optional[int] = None
    course_id: int  # Курс, к которому привязана группа (обязателен при создании)
    is_active: bool = True
    is_special: bool = False
    is_over: bool = False
    group_type: Literal["group", "individual"] = "group"
    program_type: Literal["sat", "ielts", "general_english", "nuet"] = "general_english"
    # For special groups with course_id: cap on first N lessons (default 1 if omitted)
    max_open_lessons: Optional[int] = None

class UpdateGroupRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    teacher_id: Optional[int] = None
    curator_id: Optional[int] = None
    course_id: Optional[int] = None  # Курс, к которому привязана группа
    is_active: Optional[bool] = None
    is_special: Optional[bool] = None
    is_over: Optional[bool] = None
    group_type: Optional[Literal["group", "individual"]] = None
    program_type: Optional[Literal["sat", "ielts", "general_english", "nuet"]] = None
    student_ids: Optional[List[int]] = None  # Update student list
    max_open_lessons: Optional[int] = None

class AssignTeacherRequest(BaseModel):
    teacher_id: int

class AssignUserToGroupRequest(BaseModel):
    group_id: int

class BulkAssignUsersRequest(BaseModel):
    user_ids: List[int]
    group_id: int

class AddStudentToGroupRequest(BaseModel):
    student_id: int

class RemoveStudentFromGroupRequest(BaseModel):
    student_id: int

class GroupStudentsResponse(BaseModel):
    group_id: int
    group_name: str
    students: List[UserSchema]
    total_students: int

class UserListResponse(BaseModel):
    users: List[UserSchema]
    total: int
    skip: int
    limit: int


class TeacherGroupSummary(BaseModel):
    teacher_id: Optional[int] = None
    teacher_name: str
    total_students: int


class TeacherGroupListResponse(BaseModel):
    groups: List[TeacherGroupSummary]
    total: int
    skip: int
    limit: int


class TeacherGroupStudentsResponse(BaseModel):
    teacher_id: Optional[int] = None
    teacher_name: str
    students: List[UserSchema]
    total: int
    skip: int
    limit: int

class GroupListResponse(BaseModel):
    groups: List[GroupSchema]
    total: int
    skip: int
    limit: int

class BulkGroupScheduleUploadRequest(BaseModel):
    text: str

class BulkGroupScheduleUploadResponse(BaseModel):
    created_groups: List[dict]
    failed_lines: List[dict]

class AdminDashboardResponse(BaseModel):
    stats: AdminStatsResponse
    recent_users: List[UserSchema]
    recent_groups: List[GroupSchema]
    recent_courses: List[dict]


class AdminChartDayPoint(BaseModel):
    date: str
    count: int


class AdminDashboardChartsResponse(BaseModel):
    registrations_last_14_days: List[AdminChartDayPoint]
    homework_submissions_last_14_days: List[AdminChartDayPoint]


def _admin_operational_counts(db: Session) -> dict:
    """Counts that help admins prioritize daily work."""
    now = datetime.utcnow()
    week_end = now + timedelta(days=7)
    pending_homework_to_grade = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.is_graded == False
    ).count()
    students_without_assignment_zero = db.query(UserInDB).filter(
        UserInDB.role == "student",
        UserInDB.is_active == True,
        UserInDB.assignment_zero_completed == False,
    ).count()
    open_question_reports = db.query(QuestionErrorReport).filter(
        QuestionErrorReport.status == "pending"
    ).count()
    pending_lesson_requests = db.query(LessonRequest).filter(
        LessonRequest.status == "pending"
    ).count()
    events_in_next_7_days = db.query(Event).filter(
        Event.is_active == True,
        Event.start_datetime >= now,
        Event.start_datetime <= week_end,
    ).count()
    return {
        "pending_homework_to_grade": pending_homework_to_grade,
        "students_without_assignment_zero": students_without_assignment_zero,
        "open_question_reports": open_question_reports,
        "pending_lesson_requests": pending_lesson_requests,
        "events_in_next_7_days": events_in_next_7_days,
    }


def generate_password(length: int = 8) -> str:
    """Generate a random password that satisfies the password policy (>= 8 chars, >= 1 digit), so
    admin-generated passwords always mirror to Zitadel."""
    from src.utils.password_policy import generate_compliant_password
    return generate_compliant_password(length)

def generate_student_id() -> str:
    """Generate a unique student ID"""
    return f"STU{secrets.randbelow(100000):05d}"


def get_non_special_group_ids(db: Session, group_ids: List[int]) -> List[int]:
    if not group_ids:
        return []

    existing_groups = db.query(Group).filter(Group.id.in_(group_ids)).all()
    if len(existing_groups) != len(group_ids):
        raise HTTPException(status_code=400, detail="One or more groups not found")

    return [group.id for group in existing_groups if not group.is_special]

def _link_children_to_parent(db: Session, parent_id: int, child_ids: List[int]) -> List[int]:
    """Create ParentStudent links for the given student ids. Each child_id must be an
    active student; already-linked ones are skipped (unique constraint). Returns the
    ids newly linked. Does not commit."""
    linked: List[int] = []
    for child_id in child_ids or []:
        child = db.query(UserInDB).filter(UserInDB.id == child_id).first()
        if not child or child.role != "student":
            raise HTTPException(status_code=400, detail=f"child_id {child_id} is not a valid student")
        existing = db.query(ParentStudent).filter(
            ParentStudent.parent_id == parent_id,
            ParentStudent.student_id == child_id,
        ).first()
        if not existing:
            db.add(ParentStudent(parent_id=parent_id, student_id=child_id))
            linked.append(child_id)
    return linked


@router.post("/users/single", response_model=CreateUserResponse)
def create_single_user(
    user_data: CreateUserRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator())
):
    """Create a single user (admin or head_curator)"""
    if current_user.role == "head_curator" and user_data.role != "curator":
        raise HTTPException(status_code=403, detail="Head curators can only create curator accounts")
    try:
        # Normalize email
        user_data.email = user_data.email.lower()
        
        # Check if email already exists
        existing_user = db.query(UserInDB).filter(UserInDB.email == user_data.email).first()
        if existing_user:
            raise HTTPException(status_code=400, detail="Email already registered")
        
        # Generate password if not provided
        password = user_data.password
        generated_password = None
        if not password:
            password = generate_password()
            generated_password = password
        
        # Generate student ID for students if not provided
        student_id = user_data.student_id
        if user_data.role == "student" and not student_id:
            student_id = generate_student_id()
            # Ensure student_id is unique
            while db.query(UserInDB).filter(UserInDB.student_id == student_id).first():
                student_id = generate_student_id()
        
        # Create user
        new_user = UserInDB(
            email=user_data.email,
            name=user_data.name,
            hashed_password=hash_password(password),
            role=user_data.role,
            student_id=student_id,
            is_active=user_data.is_active,
            onboarding_completed=user_data.role != 'student'
        )
        
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        
        # Assign user to groups if group_ids provided and user is a student
        if user_data.group_ids and user_data.role == "student":
            for group_id in user_data.group_ids:
                # Verify group exists
                group = db.query(Group).filter(Group.id == group_id).first()
                if group:
                    # Check if association already exists
                    existing = db.query(GroupStudent).filter(
                        GroupStudent.group_id == group_id,
                        GroupStudent.student_id == new_user.id
                    ).first()
                    if not existing:
                        group_student = GroupStudent(
                            group_id=group_id,
                            student_id=new_user.id
                        )
                        db.add(group_student)
            db.commit()
        
        # Assign user to courses if course_ids provided and user is a head_teacher
        if user_data.course_ids and user_data.role == "head_teacher":
            for course_id in user_data.course_ids:
                # Verify course exists
                course = db.query(Course).filter(Course.id == course_id).first()
                if course:
                    # Check if association already exists
                    existing = db.query(CourseHeadTeacher).filter(
                        CourseHeadTeacher.course_id == course_id,
                        CourseHeadTeacher.head_teacher_id == new_user.id
                    ).first()
                    if not existing:
                        course_head_teacher = CourseHeadTeacher(
                            course_id=course_id,
                            head_teacher_id=new_user.id
                        )
                        db.add(course_head_teacher)
            db.commit()
        
        # Link children if creating a parent
        if user_data.role == "parent" and user_data.child_ids:
            _gc_linked = _link_children_to_parent(db, new_user.id, user_data.child_ids)
            db.commit()
            try:
                from src.messages.group_membership import sync_groups_for_students
                sync_groups_for_students(db, _gc_linked)
                db.commit()
            except Exception:
                logger.exception("group chat parent-link sync failed (create parent %s)", new_user.id)
                db.rollback()

        # Email an invite (link + credentials) to created students
        if user_data.send_invites and new_user.role == "student":
            background_tasks.add_task(
                send_invite_email, new_user.email, new_user.name or "", new_user.email, password
            )

        return CreateUserResponse(
            user=UserSchema.from_orm(new_user),
            generated_password=generated_password
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create user: {str(e)}")


def _child_summary(db: Session, student: UserInDB) -> dict:
    """Small child descriptor (id, name, email, group_name) for admin parent-link views."""
    group_name = None
    gs = db.query(GroupStudent).filter(GroupStudent.student_id == student.id).first()
    if gs:
        g = db.query(Group).filter(Group.id == gs.group_id).first()
        group_name = g.name if g else None
    return {"id": student.id, "name": student.name, "email": student.email, "group_name": group_name}


@router.get("/parents/{parent_id}/children")
def list_parent_children(
    parent_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator()),
):
    """List the students linked to a parent (admin view)."""
    parent = db.query(UserInDB).filter(UserInDB.id == parent_id, UserInDB.role == "parent").first()
    if not parent:
        raise HTTPException(status_code=404, detail="Parent not found")
    links = db.query(ParentStudent).filter(ParentStudent.parent_id == parent_id).all()
    children = []
    for link in links:
        student = db.query(UserInDB).filter(UserInDB.id == link.student_id).first()
        if student:
            children.append(_child_summary(db, student))
    return children


@router.post("/parents/{parent_id}/children")
def link_parent_children(
    parent_id: int,
    body: LinkChildrenRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator()),
):
    """Link one or more students to a parent (idempotent)."""
    parent = db.query(UserInDB).filter(UserInDB.id == parent_id, UserInDB.role == "parent").first()
    if not parent:
        raise HTTPException(status_code=404, detail="Parent not found")
    linked = _link_children_to_parent(db, parent_id, body.child_ids)
    db.commit()
    try:
        from src.messages.group_membership import sync_groups_for_students
        sync_groups_for_students(db, linked)
        db.commit()
    except Exception:
        logger.exception("group chat parent-link sync failed (link parent %s)", parent_id)
        db.rollback()
    return {"linked": linked}


@router.delete("/parents/{parent_id}/children/{student_id}")
def unlink_parent_child(
    parent_id: int,
    student_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator()),
):
    """Remove a single parent↔child link."""
    link = db.query(ParentStudent).filter(
        ParentStudent.parent_id == parent_id,
        ParentStudent.student_id == student_id,
    ).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link not found")
    db.delete(link)
    db.commit()
    try:
        from src.messages.group_membership import sync_groups_for_students
        sync_groups_for_students(db, [student_id])
        db.commit()
    except Exception:
        logger.exception("group chat parent-unlink sync failed (parent %s child %s)", parent_id, student_id)
        db.rollback()
    return {"detail": "unlinked"}


@router.post("/users/bulk", response_model=BulkCreateResponse)
def create_bulk_users(
    request: BulkCreateUsersRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Create multiple users at once (admin only)"""
    created_users = []
    failed_users = []
    invite_targets = []  # (email, name, password) for created students

    for user_data in request.users:
        try:
            # Normalize email
            user_data.email = user_data.email.lower()
            
            # Check if email already exists
            existing_user = db.query(UserInDB).filter(UserInDB.email == user_data.email).first()
            if existing_user:
                failed_users.append({
                    "email": user_data.email,
                    "error": "Email already registered"
                })
                continue
            
            # Generate password if not provided
            password = user_data.password
            generated_password = None
            if not password:
                password = generate_password()
                generated_password = password
            
            # Generate student ID for students if not provided
            student_id = user_data.student_id
            if user_data.role == "student" and not student_id:
                student_id = generate_student_id()
                # Ensure student_id is unique
                while db.query(UserInDB).filter(UserInDB.student_id == student_id).first():
                    student_id = generate_student_id()
            
            # Create user
            new_user = UserInDB(
                email=user_data.email,
                name=user_data.name,
                hashed_password=hash_password(password),
                role=user_data.role,
                student_id=student_id,
                is_active=user_data.is_active
            )
            
            db.add(new_user)
            db.flush()  # Get ID without committing
            
            # Assign user to groups if group_ids provided and user is a student
            if user_data.group_ids and user_data.role == "student":
                for group_id in user_data.group_ids:
                    # Verify group exists
                    group = db.query(Group).filter(Group.id == group_id).first()
                    if group:
                        # Check if association already exists
                        existing = db.query(GroupStudent).filter(
                            GroupStudent.group_id == group_id,
                            GroupStudent.student_id == new_user.id
                        ).first()
                        if not existing:
                            group_student = GroupStudent(
                                group_id=group_id,
                                student_id=new_user.id
                            )
                            db.add(group_student)
            
            if new_user.role == "student":
                invite_targets.append((new_user.email, new_user.name or "", password))

            created_users.append(CreateUserResponse(
                user=UserSchema.from_orm(new_user),
                generated_password=generated_password
            ))

        except Exception as e:
            failed_users.append({
                "email": user_data.email,
                "error": str(e)
            })

    # Commit all successful creations
    if created_users:
        db.commit()
        if request.send_invites:
            for email, name, password in invite_targets:
                background_tasks.add_task(send_invite_email, email, name, email, password)
    else:
        db.rollback()

    return BulkCreateResponse(
        created_users=created_users,
        failed_users=failed_users
    )

@router.post("/users/bulk-text", response_model=BulkCreateResponse)
def create_bulk_users_from_text(
    request: BulkCreateUsersFromTextRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """
    Create multiple users from pasted text (TSV format).
    Expected format per line: name<TAB>phone<TAB>months<TAB>date<TAB>email
    Lines starting with # are ignored as comments.
    Empty lines are skipped.
    """
    created_users = []
    failed_users = []
    invite_targets = []  # (email, name, password) for created students
    
    lines = request.text.strip().split('\n')
    
    for line_num, line in enumerate(lines, start=1):
        line = line.strip()
        
        # Skip empty lines and comments
        if not line or line.startswith('#'):
            continue
        
        try:
            # Split by tab
            parts = line.split('\t')
            
            if len(parts) < 5:
                failed_users.append({
                    "email": f"Line {line_num}",
                    "error": f"Invalid format: expected 5 tab-separated values, got {len(parts)}. Line: {line[:50]}..."
                })
                continue
            
            name = parts[0].strip()
            phone = parts[1].strip()
            # months = parts[2].strip()  # Not used for user creation, could be stored in notes
            # date = parts[3].strip()    # Not used for user creation, could be stored in notes
            email = parts[4].strip().lower()
            
            # Validate required fields
            if not name:
                failed_users.append({
                    "email": f"Line {line_num}",
                    "error": "Name is required"
                })
                continue
            
            if not email:
                failed_users.append({
                    "email": f"Line {line_num}",
                    "error": "Email is required"
                })
                continue
            
            # Validate email format (basic check)
            if '@' not in email or '.' not in email:
                failed_users.append({
                    "email": email,
                    "error": "Invalid email format"
                })
                continue
            
            # Check if email already exists
            existing_user = db.query(UserInDB).filter(UserInDB.email == email).first()
            if existing_user:
                failed_users.append({
                    "email": email,
                    "error": "Email already registered"
                })
                continue
            
            # Generate password
            password = generate_password() if request.generate_passwords else None
            generated_password = password
            
            # Generate student ID for students
            student_id = None
            if request.role == "student":
                student_id = generate_student_id()
                # Ensure student_id is unique
                while db.query(UserInDB).filter(UserInDB.student_id == student_id).first():
                    student_id = generate_student_id()
            
            # Create user
            new_user = UserInDB(
                email=email,
                name=name,
                hashed_password=hash_password(password) if password else hash_password(generate_password()),
                role=request.role,
                student_id=student_id,
                is_active=True,
                onboarding_completed=request.role != 'student'
            )
            
            db.add(new_user)
            db.flush()  # Get ID without committing
            
            # Assign user to groups if group_ids provided and user is a student
            if request.group_ids and request.role == "student":
                for group_id in request.group_ids:
                    # Verify group exists
                    group = db.query(Group).filter(Group.id == group_id).first()
                    if group:
                        # Check if association already exists
                        existing = db.query(GroupStudent).filter(
                            GroupStudent.group_id == group_id,
                            GroupStudent.student_id == new_user.id
                        ).first()
                        if not existing:
                            group_student = GroupStudent(
                                group_id=group_id,
                                student_id=new_user.id
                            )
                            db.add(group_student)
            
            if request.role == "student" and password:
                invite_targets.append((email, name, password))

            created_users.append(CreateUserResponse(
                user=UserSchema.from_orm(new_user),
                generated_password=generated_password
            ))

        except Exception as e:
            failed_users.append({
                "email": f"Line {line_num}",
                "error": str(e)
            })

    # Commit all successful creations
    if created_users:
        db.commit()
        if request.send_invites:
            for email, name, password in invite_targets:
                background_tasks.add_task(send_invite_email, email, name, email, password)
    else:
        db.rollback()

    return BulkCreateResponse(
        created_users=created_users,
        failed_users=failed_users
    )

@router.post("/create-admin", response_model=CreateAdminResponse)
def create_admin(
    admin_data: CreateAdminRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Create a new admin user (admin only)"""
    try:
        # Normalize email
        admin_data.email = admin_data.email.lower()
        
        # Check if email already exists
        existing_user = db.query(UserInDB).filter(UserInDB.email == admin_data.email).first()
        if existing_user:
            raise HTTPException(status_code=400, detail="Email already registered")
        
        # Generate password if not provided
        password = admin_data.password
        generated_password = None
        if not password:
            password = generate_password()
            generated_password = password
        
        # Create admin user
        new_admin = UserInDB(
            email=admin_data.email,
            name=admin_data.name,
            hashed_password=hash_password(password),
            role="admin",  # Fixed role for admin creation
            is_active=admin_data.is_active
        )
        
        db.add(new_admin)
        db.commit()
        db.refresh(new_admin)
        
        return CreateAdminResponse(
            admin=UserSchema.from_orm(new_admin),
            generated_password=generated_password
        )
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create admin: {str(e)}")



@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator())
):
    """Deactivate a user (soft delete; admin, or head_curator for curators only).

    Reversible: sets is_active=False and preserves the account and all related data.
    """
    user = db.query(UserInDB).filter(UserInDB.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if current_user.role == "head_curator" and user.role != "curator":
        raise HTTPException(status_code=403, detail="Head curators can only deactivate curator accounts")

    # Prevent deactivating yourself
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")

    # Prevent deactivating the last active admin
    if user.role == "admin":
        active_admins = db.query(UserInDB).filter(
            UserInDB.role == "admin", UserInDB.is_active == True
        ).count()
        if active_admins <= 1:
            raise HTTPException(status_code=400, detail="Cannot deactivate the last admin user")

    # Soft deactivate (reversible) — keeps the account and all related data
    user.is_active = False
    user.refresh_token = None  # Invalidate sessions
    user.updated_at = datetime.utcnow()
    db.commit()

    return {"detail": f"User '{user.name}' deactivated successfully"}

@router.get("/stats", response_model=AdminStatsResponse)
def get_admin_stats(
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Get platform statistics (admin only)"""
    # Basic counts
    total_users = db.query(UserInDB).count()
    total_students = db.query(UserInDB).filter(UserInDB.role == "student", UserInDB.is_trial == False).count()
    total_teachers = db.query(UserInDB).filter(UserInDB.role == "teacher").count()
    total_curators = db.query(UserInDB).filter(UserInDB.role == "curator").count()
    total_courses = db.query(Course).count()
    total_active_enrollments = db.query(Enrollment).filter(Enrollment.is_active == True).count()
    
    # Recent registrations (last 7 days)
    week_ago = datetime.utcnow() - timedelta(days=7)
    recent_registrations = db.query(UserInDB).filter(UserInDB.created_at >= week_ago).count()

    operational = _admin_operational_counts(db)

    return AdminStatsResponse(
        total_users=total_users,
        total_students=total_students,
        total_teachers=total_teachers,
        total_curators=total_curators,
        total_courses=total_courses,
        total_active_enrollments=total_active_enrollments,
        recent_registrations=recent_registrations,
        **operational,
    )

@router.get("/students/progress", response_model=List[StudentProgressSummary])
def get_students_progress_summary(
    skip: int = 0,
    limit: int = 50,
    group_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Get progress summary for all students (admin only)"""
    query = db.query(UserInDB).filter(UserInDB.role == "student", UserInDB.is_trial == False)
    
    if group_id:
        # Filter students by group using GroupStudent association table
        group_student_ids = db.query(GroupStudent.student_id).filter(
            GroupStudent.group_id == group_id
        ).subquery()
        query = query.filter(UserInDB.id.in_(group_student_ids))
    
    students = query.offset(skip).limit(limit).all()
    summaries = []
    
    for student in students:
        # Get student's enrollments
        enrollments = db.query(Enrollment).filter(
            Enrollment.user_id == student.id,
            Enrollment.is_active == True
        ).all()
        
        total_courses = len(enrollments)
        completed_courses = 0
        total_progress = 0
        
        for enrollment in enrollments:
            # Check if course is completed
            course_progress = db.query(StudentProgress).filter(
                StudentProgress.user_id == student.id,
                StudentProgress.course_id == enrollment.course_id
            ).all()
            
            if course_progress:
                avg_progress = sum(p.completion_percentage for p in course_progress) / len(course_progress)
                total_progress += avg_progress
                if avg_progress >= 100:
                    completed_courses += 1
        
        average_progress = total_progress / total_courses if total_courses > 0 else 0
        
        # Get last activity
        last_activity = db.query(StudentProgress.last_accessed).filter(
            StudentProgress.user_id == student.id
        ).order_by(desc(StudentProgress.last_accessed)).first()
        
        # Get group name using GroupStudent association table
        group_name = None
        group_student = db.query(GroupStudent).filter(GroupStudent.student_id == student.id).first()
        if group_student:
            group = db.query(Group).filter(Group.id == group_student.group_id).first()
            group_name = group.name if group else None
        
        summaries.append(StudentProgressSummary(
            user_id=student.id,
            name=student.name,
            email=student.email,
            student_id=student.student_id,
            group_name=group_name,
            total_courses=total_courses,
            completed_courses=completed_courses,
            average_progress=round(average_progress, 2),
            total_study_time=student.total_study_time_minutes,
            last_activity=last_activity[0] if last_activity else None
        ))
    
    return summaries

@router.post("/reset-password/{user_id}")
def reset_user_password(
    user_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Reset user password and return new password (admin only)"""
    user = db.query(UserInDB).filter(UserInDB.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    new_password = generate_password()
    user.hashed_password = hash_password(new_password)
    user.refresh_token = None  # Invalidate all sessions
    user.updated_at = datetime.utcnow()

    db.commit()

    # Email the user their new password (admin-initiated change)
    background_tasks.add_task(
        send_password_changed_email, user.email, user.name or "", new_password
    )
    # Keep the Master Education (Zitadel) password in step — best-effort, off the response path.
    from src.services.zitadel_provisioning import mirror_password

    background_tasks.add_task(
        mirror_password, user.central_auth_user_id, new_password, lms_user_id=user.id
    )

    return {
        "detail": "Password reset successfully",
        "new_password": new_password,
        "user_email": user.email
    }

@router.get("/groups", response_model=List[GroupSchema])
def get_all_groups(
    skip: int = 0,
    limit: int = 100,
    teacher_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    program_type: Optional[Literal["sat", "ielts", "general_english", "nuet"]] = Query(
        None, description="Фильтр по программе (SAT / IELTS / General English)"
    ),
    include_students: bool = Query(
        True, description="Embed full student lists per group. Set false for a lightweight group list."
    ),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_teacher_or_admin_for_groups())
):
    """Get all groups (teachers, head curators and admins)"""
    sync_groups_over_status(db)
    query = db.query(Group)
    
    # Teachers: own groups. Head teachers: groups linked to courses they manage.
    # Admins / head curators: all groups (optional teacher_id / filters below).
    if current_user.role == "teacher":
        query = query.filter(Group.teacher_id == current_user.id)
    elif current_user.role == "head_teacher":
        from src.utils.permissions import get_head_teacher_group_ids

        group_ids = get_head_teacher_group_ids(current_user, db)
        if not group_ids:
            return []
        query = query.filter(Group.id.in_(group_ids))
    elif teacher_id is not None:
        query = query.filter(Group.teacher_id == teacher_id)
    
    if is_active is not None:
        query = query.filter(Group.is_active == is_active)
    if program_type is not None:
        query = query.filter(Group.program_type == program_type)

    groups = query.offset(skip).limit(limit).all()
    # Enrich with teacher names, curator names and student counts
    result = []
    for group in groups:
        teacher = db.query(UserInDB).filter(UserInDB.id == group.teacher_id).first() if group.teacher_id else None
        curator = db.query(UserInDB).filter(UserInDB.id == group.curator_id).first() if group.curator_id else None
        cga = (
            db.query(CourseGroupAccess)
            .filter(
                CourseGroupAccess.group_id == group.id,
                CourseGroupAccess.is_active == True,
            )
            .first()
        )
        linked_course_ids = [
            row[0]
            for row in db.query(CourseGroupAccess.course_id).filter(
                CourseGroupAccess.group_id == group.id,
                CourseGroupAccess.is_active == True,
            ).all()
        ]
        max_open_lessons = cga.max_open_lessons if cga else None
        linked_course_id = linked_course_ids[0] if linked_course_ids else None
        # Get students for this group. In lightweight mode, only the count is
        # computed (avoids an N+1 per-student query when the caller needs metadata only).
        if not include_students:
            student_count = (
                db.query(func.count(GroupStudent.id))
                .filter(GroupStudent.group_id == group.id)
                .scalar()
                or 0
            )
            group_students = []
        else:
            group_students = db.query(GroupStudent).filter(GroupStudent.group_id == group.id).all()
            student_count = len(group_students)

        # Get student details
        students = []
        for group_student in group_students:
            student = db.query(UserInDB).filter(
                UserInDB.id == group_student.student_id,
                UserInDB.role == "student",
                UserInDB.is_active == True
            ).first()
            if student:
                # Create UserSchema with group information
                student_data = UserSchema(
                    id=student.id,
                    email=student.email,
                    name=student.name,
                    role=student.role,
                    avatar_url=student.avatar_url,
                    is_active=student.is_active,
                    student_id=student.student_id,
                    teacher_name=teacher.name if teacher else None,
                    curator_name=curator.name if curator else None,
                    total_study_time_minutes=student.total_study_time_minutes,
                    created_at=student.created_at
                )
                students.append(student_data)
        
        group_data = GroupSchema(
            id=group.id,
            name=group.name,
            description=group.description,
            teacher_id=group.teacher_id,
            teacher_name=teacher.name if teacher else None,
            curator_id=group.curator_id,
            curator_name=curator.name if curator else None,
            student_count=student_count,
            students=students,
            created_at=group.created_at,
            is_active=group.is_active,
            is_special=group.is_special,
            is_over=group.is_over,
            group_type=getattr(group, "group_type", None) or "group",
            program_type=getattr(group, "program_type", None) or "general_english",
            schedule_config=group.schedule_config,
            max_open_lessons=max_open_lessons,
            course_id=linked_course_id,
            course_ids=linked_course_ids or None,
        )
        
        result.append(group_data)
    
    return result

# =============================================================================
# GROUP MANAGEMENT ENDPOINTS (ADMIN ONLY)
# =============================================================================

@router.post("/groups", response_model=GroupSchema)
def create_group(
    group_data: CreateGroupRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Create a new group (admin only)"""
    if group_data.is_special:
        if not group_data.curator_id:
            raise HTTPException(status_code=400, detail="Curator is required for special groups")
        curator = db.query(UserInDB).filter(
            UserInDB.id == group_data.curator_id,
            UserInDB.role == "curator"
        ).first()
        if not curator:
            raise HTTPException(status_code=400, detail="Curator not found")
        teacher = None
        resolved_teacher_id = None
        if group_data.teacher_id is not None:
            teacher = db.query(UserInDB).filter(
                UserInDB.id == group_data.teacher_id,
                UserInDB.role == "teacher"
            ).first()
            if not teacher:
                raise HTTPException(status_code=400, detail="Teacher not found")
            resolved_teacher_id = group_data.teacher_id
    else:
        if not group_data.teacher_id:
            raise HTTPException(status_code=400, detail="Teacher is required")
        teacher = db.query(UserInDB).filter(
            UserInDB.id == group_data.teacher_id,
            UserInDB.role == "teacher"
        ).first()
        if not teacher:
            raise HTTPException(status_code=400, detail="Teacher not found")
        resolved_teacher_id = group_data.teacher_id
        curator = None
        if group_data.curator_id:
            curator = db.query(UserInDB).filter(
                UserInDB.id == group_data.curator_id,
                UserInDB.role == "curator"
            ).first()
            if not curator:
                raise HTTPException(status_code=400, detail="Curator not found")

    course = db.query(Course).filter(Course.id == group_data.course_id).first()
    if not course:
        raise HTTPException(status_code=400, detail="Course not found")

    max_for_access: Optional[int] = None
    if group_data.is_special:
        max_for_access = group_data.max_open_lessons if group_data.max_open_lessons is not None else 1
        if max_for_access < 1:
            raise HTTPException(status_code=400, detail="max_open_lessons must be at least 1")

    # Check if group name already exists
    existing_group = db.query(Group).filter(Group.name == group_data.name).first()
    if existing_group:
        raise HTTPException(status_code=400, detail="Group name already exists")

    new_group = Group(
        name=group_data.name,
        description=group_data.description,
        teacher_id=resolved_teacher_id,
        curator_id=group_data.curator_id,
        is_active=group_data.is_active,
        is_special=group_data.is_special,
        is_over=group_data.is_over,
        group_type=group_data.group_type,
        program_type=group_data.program_type,
    )

    db.add(new_group)
    db.commit()
    db.refresh(new_group)
    # Cross-platform sync is captured by a DB trigger on the `groups` table (see
    # p7_student_sync_group_trigger) so CRM cross-DB writes are covered too — no app hook needed.

    course_access = CourseGroupAccess(
        course_id=group_data.course_id,
        group_id=new_group.id,
        granted_by=current_user.id,
        is_active=True,
        max_open_lessons=max_for_access,
    )
    db.add(course_access)
    db.commit()

    group_response = GroupSchema(
        id=new_group.id,
        name=new_group.name,
        description=new_group.description,
        teacher_id=new_group.teacher_id,
        teacher_name=teacher.name if teacher else None,
        curator_id=new_group.curator_id,
        curator_name=curator.name if curator else None,
        student_count=0,
        students=[],
        created_at=new_group.created_at,
        is_active=new_group.is_active,
        is_special=new_group.is_special,
        is_over=new_group.is_over,
        group_type=getattr(new_group, "group_type", None) or "group",
        program_type=getattr(new_group, "program_type", None) or "general_english",
        schedule_config=new_group.schedule_config,
        max_open_lessons=max_for_access,
        course_id=group_data.course_id,
    )

    return group_response

@router.put("/groups/{group_id}", response_model=GroupSchema)
def update_group(
    group_id: int,
    group_data: UpdateGroupRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Update a group (admin only)"""
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    patch = group_data.model_dump(exclude_unset=True)

    if "teacher_id" in patch and patch["teacher_id"] is not None:
        tid = patch["teacher_id"]
        teacher = db.query(UserInDB).filter(
            UserInDB.id == tid,
            UserInDB.role == "teacher"
        ).first()
        if not teacher:
            raise HTTPException(status_code=400, detail="Teacher not found")

    if "curator_id" in patch:
        if patch["curator_id"]:
            curator = db.query(UserInDB).filter(
                UserInDB.id == patch["curator_id"],
                UserInDB.role == "curator"
            ).first()
            if not curator:
                raise HTTPException(status_code=400, detail="Curator not found")

    if patch.get("course_id"):
        course = db.query(Course).filter(Course.id == patch["course_id"]).first()
        if not course:
            raise HTTPException(status_code=400, detail="Course not found")

    if group_data.name and group_data.name != group.name:
        existing_group = db.query(Group).filter(
            Group.name == group_data.name,
            Group.id != group_id
        ).first()
        if existing_group:
            raise HTTPException(status_code=400, detail="Group name already exists")

    if group_data.name is not None:
        group.name = group_data.name
    if group_data.description is not None:
        group.description = group_data.description
    if "teacher_id" in patch:
        if group.teacher_id != patch["teacher_id"]:
            from src.services.schedule_reconciliation import sync_future_lesson_teachers
            sync_future_lesson_teachers(db, group.id, patch["teacher_id"])
        group.teacher_id = patch["teacher_id"]
    if "curator_id" in patch:
        group.curator_id = patch["curator_id"]
    if group_data.is_active is not None:
        group.is_active = group_data.is_active
    if group_data.is_special is not None:
        group.is_special = group_data.is_special
    if group_data.is_over is not None:
        group.is_over = group_data.is_over
    if group_data.group_type is not None:
        group.group_type = group_data.group_type
    if group_data.program_type is not None:
        group.program_type = group_data.program_type

    if group.is_special:
        if not group.curator_id:
            raise HTTPException(status_code=400, detail="Curator is required for special groups")
    else:
        if not group.teacher_id:
            raise HTTPException(status_code=400, detail="Teacher is required for non-special groups")

    max_for_new_access: Optional[int] = None
    if group_data.course_id is not None:
        db.query(CourseGroupAccess).filter(
            CourseGroupAccess.group_id == group_id
        ).delete()

        if group_data.course_id:
            if group.is_special:
                if "max_open_lessons" in patch and patch["max_open_lessons"] is not None:
                    max_for_new_access = patch["max_open_lessons"]
                else:
                    max_for_new_access = 1
                if max_for_new_access < 1:
                    raise HTTPException(status_code=400, detail="max_open_lessons must be at least 1")
            course_access = CourseGroupAccess(
                course_id=group_data.course_id,
                group_id=group_id,
                granted_by=current_user.id,
                is_active=True,
                max_open_lessons=max_for_new_access,
            )
            db.add(course_access)
    elif "max_open_lessons" in patch and group.is_special:
        q = db.query(CourseGroupAccess).filter(CourseGroupAccess.group_id == group_id)
        mo = patch["max_open_lessons"]
        if mo is not None and mo < 1:
            raise HTTPException(status_code=400, detail="max_open_lessons must be at least 1")
        for row in q.all():
            row.max_open_lessons = mo
    
    if "student_ids" in patch:
        _sync_group_students(db, group_id, patch["student_ids"])

    db.commit()
    db.refresh(group)
    # Cross-platform sync is captured by the `groups` DB trigger (see p7_student_sync_group_trigger).

    try:
        from src.messages.group_membership import sync_group_conversation_members
        sync_group_conversation_members(db, group_id)
        db.commit()
    except Exception:
        logger.exception("group chat member sync failed for group %s", group_id)
        db.rollback()

    # Create response with teacher name, curator name and student count
    teacher = db.query(UserInDB).filter(UserInDB.id == group.teacher_id).first() if group.teacher_id else None
    curator = db.query(UserInDB).filter(UserInDB.id == group.curator_id).first() if group.curator_id else None
    cga_out = (
        db.query(CourseGroupAccess)
        .filter(
            CourseGroupAccess.group_id == group.id,
            CourseGroupAccess.is_active == True,
        )
        .first()
    )
    max_open_out = cga_out.max_open_lessons if cga_out else None
    course_id_out = cga_out.course_id if cga_out else None
    
    # Get student count using GroupStudent association table
    student_count = db.query(GroupStudent).filter(
        GroupStudent.group_id == group.id
    ).count()
    
    # Get students for this group
    group_students = db.query(GroupStudent).filter(GroupStudent.group_id == group.id).all()
    students = []
    
    for group_student in group_students:
        student = db.query(UserInDB).filter(
            UserInDB.id == group_student.student_id,
            UserInDB.role == "student",
            UserInDB.is_active == True
        ).first()
        if student:
            students.append(UserSchema.from_orm(student))
    
    group_response = GroupSchema(
        id=group.id,
        name=group.name,
        description=group.description,
        teacher_id=group.teacher_id,
        teacher_name=teacher.name if teacher else None,
        curator_id=group.curator_id,
        curator_name=curator.name if curator else None,
        student_count=len(students),
        students=students,
        created_at=group.created_at,
        is_active=group.is_active,
        is_special=group.is_special,
        is_over=group.is_over,
        group_type=getattr(group, "group_type", None) or "group",
        program_type=getattr(group, "program_type", None) or "general_english",
        schedule_config=group.schedule_config,
        max_open_lessons=max_open_out,
        course_id=course_id_out,
    )
    
    return group_response

@router.delete("/groups/{group_id}")
def delete_group(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Delete a group (admin only) - soft delete"""
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    
    # Check if group has students
    student_count = db.query(GroupStudent).filter(GroupStudent.group_id == group_id).count()
    
    if student_count > 0:
        raise HTTPException(
            status_code=400, 
            detail=f"Cannot delete group with {student_count} active students. Remove students first."
        )
    
def parse_date(date_str: str) -> Optional[date]:
    try:
        # Example: February 5 2026
        return datetime.strptime(date_str.strip(), "%B %d %Y").date()
    except ValueError:
        try:
             # Try DD.MM.YYYY format
             return datetime.strptime(date_str.strip(), "%d.%m.%Y").date()
        except ValueError:
            try:
                 # Try other common formats
                 return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
            except:
                 return None

def parse_shorthand_python(text: str) -> List[dict]:
    day_map = {
        'пн': 0, 'вт': 1, 'ср': 2, 'чт': 3, 'пт': 4, 'сб': 5, 'вс': 6,
        'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6
    }
    
    tokens = text.lower().replace(':', ' ').split()
    days = []
    time_str = "19:00"
    
    # Collect days and find time
    for i, token in enumerate(tokens):
        if token in day_map:
            days.append(day_map[token])
        elif token.isdigit():
            # Potential hour
            if i + 1 < len(tokens) and tokens[i+1].isdigit() and len(tokens[i+1]) == 2:
                time_str = f"{token.zfill(2)}:{tokens[i+1]}"
                # Keep looking? usually it's one time for all days in shorthand
    
    return [{"day_of_week": d, "time_of_day": time_str} for d in days]

@router.post("/groups/bulk-schedule-upload", response_model=BulkGroupScheduleUploadResponse)
def bulk_schedule_upload(
    request: BulkGroupScheduleUploadRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """
    Bulk create groups/schedules from text.
    Format: Date\tStudentName\tTeacherName\tCourseInfo\tLessonsCount\tShorthand\tStartDate
    Example: January 1 2026\tБибинур Сырымкызы\tАданова Дарина\tSAT 4 месяца\t48\tвт чт сб 20 00\t06.01.2026
    The last column (StartDate) is used as the actual start date for the schedule.
    """
    created_groups = []
    failed_lines = []
    
    import math
    
    lines = request.text.strip().split('\n')
    for i, line in enumerate(lines):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
            
        parts = line.split('\t')
        if len(parts) < 7:
            failed_lines.append({"line_num": i+1, "error": f"Invalid format, expected 7 parts, got {len(parts)}"})
            continue
            
        try:
            # Skip first column (date), use last column as start date
            student_name = parts[1].strip()
            teacher_name = parts[2].strip()
            course_info = parts[3].strip()
            lessons_count_str = parts[4].strip()
            shorthand = parts[5].strip()
            start_date_str = parts[6].strip()
            
            # 1. Parse Start Date
            start_date = parse_date(start_date_str)
            if not start_date:
                failed_lines.append({"line_num": i+1, "error": f"Failed to parse start date: {start_date_str}"})
                continue
                
            # 3. Parse Lessons Count
            try:
                lessons_count = int(lessons_count_str)
            except ValueError:
                failed_lines.append({"line_num": i+1, "error": f"Invalid lessons count: {lessons_count_str}"})
                continue
                
            # 3. Find or Create Teacher (Case-insensitive)
            teacher = db.query(UserInDB).filter(
                func.lower(UserInDB.name) == teacher_name.lower(),
                UserInDB.role == "teacher"
            ).first()
            if not teacher:
                # Try all roles if not found among teachers
                teacher = db.query(UserInDB).filter(func.lower(UserInDB.name) == teacher_name.lower()).first()
            if not teacher:
                # Auto-create teacher if not found
                import string
                import secrets
                teacher_password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
                teacher_email = f"{teacher_name.lower().replace(' ', '.')}@auto.created"
                
                teacher = UserInDB(
                    name=teacher_name,
                    email=teacher_email,
                    hashed_password=hash_password(teacher_password),
                    role="teacher",
                    is_active=True
                )
                db.add(teacher)
                db.flush()
                
            # 4. Find or Create Student (never use teacher as student - prevents teacher appearing in attendance)
            student = db.query(UserInDB).filter(
                func.lower(UserInDB.name) == student_name.lower(),
                UserInDB.role == "student"
            ).first()
            if not student:
                existing_user = db.query(UserInDB).filter(func.lower(UserInDB.name) == student_name.lower()).first()
                if existing_user and existing_user.role != "student":
                    failed_lines.append({"line_num": i+1, "error": f"'{student_name}' is a {existing_user.role}, not a student. Column 2 must be a student name."})
                    continue
            if not student:
                 # Auto-create student if not found
                 import string
                 import secrets
                 student_password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
                 student_email = f"{student_name.lower().replace(' ', '.')}@auto.created"
                 
                 student = UserInDB(
                     name=student_name,
                     email=student_email,
                     hashed_password=hash_password(student_password),
                     role="student",
                     is_active=True
                 )
                 db.add(student)
                 db.flush()
                 
            # 5. Determine Course
            course = None
            if "SAT" in course_info.upper():
                course = db.query(Course).filter(Course.title.ilike("%SAT%")).first()
            elif "IELTS" in course_info.upper():
                course = db.query(Course).filter(Course.title.ilike("%IELTS%")).first()
                
            if not course:
                course = db.query(Course).first() # Fallback
            
            # 6. Find Existing Group (don't create new ones)
            # Try multiple search strategies to find the group
            
            group = None
            
            # Strategy 1: Try student name first (most likely to match)
            student_groups = db.query(Group).filter(
                Group.name.ilike(f"%{student_name}%")
            ).all()
            
            if student_groups:
                # Prefer groups with matching teacher
                for potential_group in student_groups:
                    if potential_group.teacher_id == teacher.id:
                        group = potential_group
                        break
                
                # If no teacher match, prefer groups containing course info
                if not group:
                    for potential_group in student_groups:
                        if course_info.upper() in potential_group.name.upper():
                            group = potential_group
                            break
                
                # Otherwise take the first one
                if not group:
                    group = student_groups[0]
            
            # Strategy 2: If not found, try student name + course info
            if not group:
                potential_groups = db.query(Group).filter(
                    Group.name.ilike(f"%{student_name}%")
                ).filter(
                    Group.name.ilike(f"%{course_info}%")
                ).all()
                
                if potential_groups:
                    # Prefer groups with matching teacher
                    for potential_group in potential_groups:
                        if potential_group.teacher_id == teacher.id:
                            group = potential_group
                            break
                    
                    # If no teacher match, take the first one
                    if not group:
                        group = potential_groups[0]
            
            # Strategy 3: If still not found, try exact match with course_info - student_name
            if not group:
                group_name = f"{course_info} - {student_name}"
                group = db.query(Group).filter(Group.name == group_name).first()
            
            # Strategy 4: If still not found, try date-based search (least specific)
            if not group:
                groups_with_date = db.query(Group).filter(
                    Group.name.ilike(f"%{start_date.strftime('%B %d %Y')}%") |
                    Group.name.ilike(f"%{start_date.strftime('%B %d')}%") |
                    Group.name.ilike(f"%{start_date.strftime('%Y-%m-%d')}%")
                ).all()
                
                if groups_with_date:
                    # Prefer groups with matching teacher
                    for potential_group in groups_with_date:
                        if potential_group.teacher_id == teacher.id:
                            group = potential_group
                            break
                    
                    # If no teacher match, take the first one
                    if not group:
                        group = groups_with_date[0]
            
            # Strategy 4: If still not found, try common transliterations
            if not group:
                # Common Kazakh name transliterations
                translit_map = {
                    'Абзал': 'Abzal',
                    'Азамат': 'Azamat', 
                    'Мадина': 'Madina',
                    'Жансая': 'Zhansaya',
                    'Маулен': 'Maulen',
                    'Аянат': 'Ayanat',
                    'Амина': 'Amina',
                    'Таймас': 'Taimas',
                    'Амирлан': 'Amirlan',
                    'Бибинур': 'Bibinur',
                    'Айша': 'Aisha'
                }
                
                for kazakh, english in translit_map.items():
                    if kazakh in student_name and not group:
                        group = db.query(Group).filter(
                            Group.name.ilike(f"%{english}%")
                        ).first()
                        if group:
                            break
            
            if not group:
                failed_lines.append({"line_num": i+1, "error": f"Group not found for student '{student_name}' starting {start_date}. Please ensure the group exists."})
                continue
            
            # 7. Add Student to Group (if not already there)
            existing_gs = db.query(GroupStudent).filter(
                GroupStudent.group_id == group.id,
                GroupStudent.student_id == student.id
            ).first()
            if not existing_gs:
                try:
                    gs = GroupStudent(group_id=group.id, student_id=student.id)
                    db.add(gs)
                    db.flush()  # Check for duplicates immediately
                except Exception as e:
                    # If duplicate key error, student is already in group - that's fine
                    if "duplicate key" in str(e).lower() or "unique constraint" in str(e).lower():
                        db.rollback()  # Rollback the failed insert
                        pass  # Continue without error
                    else:
                        raise  # Re-raise other errors
            
            # 7. Add Student to Group
            existing_gs = db.query(GroupStudent).filter(
                GroupStudent.group_id == group.id,
                GroupStudent.student_id == student.id
            ).first()
            if not existing_gs:
                gs = GroupStudent(group_id=group.id, student_id=student.id)
                db.add(gs)
                
            # 8. Link Course
            if course:
                existing_ca = db.query(CourseGroupAccess).filter(
                    CourseGroupAccess.group_id == group.id,
                    CourseGroupAccess.course_id == course.id
                ).first()
                if not existing_ca:
                    ca = CourseGroupAccess(
                        group_id=group.id, 
                        course_id=course.id, 
                        is_active=True,
                        granted_by=current_user.id
                    )
                    db.add(ca)
            
            # 9. Generate Schedule
            schedule_items = parse_shorthand_python(shorthand)
            if not schedule_items:
                failed_lines.append({"line_num": i+1, "error": f"Failed to parse shorthand: {shorthand}"})
                continue
                
            # Calculate end date based on lessons count and schedule frequency
            lessons_per_week = len(schedule_items)
            if lessons_per_week == 0:
                failed_lines.append({"line_num": i+1, "error": f"No lessons per week found in shorthand: {shorthand}"})
                continue
                
            total_weeks = math.ceil(lessons_count / lessons_per_week)
            end_date = start_date + timedelta(weeks=total_weeks - 1)  # -1 because start week counts
            
            end_recurrence = end_date
            
            # Calculate weeks between start and end dates
            weeks_diff = (end_date - start_date).days // 7
            week_limit = max(1, weeks_diff)  # At least 1 week
            
            # 9. Create individual Event entries for each lesson (reconciliation preserves attendance)
            # Kazakhstan timezone offset (GMT+5)
            KZ_OFFSET = timedelta(hours=5)

            # STEP 1: Generate all possible lesson dates first
            all_lesson_dates = []

            for week in range(week_limit + 2):  # +2 for safety margin
                for item in schedule_items:
                    try:
                        time_obj = datetime.strptime(item["time_of_day"], "%H:%M").time()
                    except Exception:
                        time_obj = time(19, 0)

                    days_ahead = item["day_of_week"] - start_date.weekday()
                    if days_ahead < 0:
                        days_ahead += 7

                    target_date = start_date + timedelta(days=days_ahead) + timedelta(weeks=week)
                    target_dt_kz = datetime.combine(target_date, time_obj)
                    target_dt = target_dt_kz - KZ_OFFSET

                    if target_date >= start_date:
                        all_lesson_dates.append(target_dt)

            # STEP 2: Sort and take lessons_count
            all_lesson_dates.sort()
            all_lesson_dates = all_lesson_dates[:lessons_count]

            # STEP 3: Reconcile (preserves event ids and attendance for matched slots)
            from src.services.schedule_reconciliation import reconcile_group_schedule

            dt_utc = lambda d: d.replace(tzinfo=_tz.utc) if d.tzinfo is None else d
            desired_slots = [(dt_utc(dt), ln) for ln, dt in enumerate(all_lesson_dates, start=1)]
            result = reconcile_group_schedule(
                db=db,
                group_id=group.id,
                desired_slots=desired_slots,
                group_name=group.name,
                teacher_id=group.teacher_id,
                created_by=current_user.id,
            )
            lessons_created = result["updated"] + result["created"]
                
            # Save config
            group.schedule_config = {
                "start_date": start_date.isoformat(),
                "weeks_count": week_limit,
                "lessons_count": lessons_count,
                "schedule_items": schedule_items
            }
            
            created_groups.append({
                "student_name": student_name,
                "group_name": group.name,
                "lessons_count": lessons_count
            })
            
        except Exception as e:
            db.rollback()
            failed_lines.append({"line_num": i+1, "error": str(e)})
            continue
            
    db.commit()
    return BulkGroupScheduleUploadResponse(created_groups=created_groups, failed_lines=failed_lines)

@router.post("/groups/{group_id}/assign-teacher")
def assign_teacher_to_group(
    group_id: int,
    teacher_data: AssignTeacherRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Assign a teacher to a group (admin only)"""
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    
    teacher = db.query(UserInDB).filter(
        UserInDB.id == teacher_data.teacher_id,
        UserInDB.role == "teacher"
    ).first()
    if not teacher:
        raise HTTPException(status_code=400, detail="Teacher not found")
    
    teacher_changed = group.teacher_id != teacher_data.teacher_id
    group.teacher_id = teacher_data.teacher_id
    if teacher_changed:
        from src.services.schedule_reconciliation import sync_future_lesson_teachers
        sync_future_lesson_teachers(db, group.id, teacher_data.teacher_id)
    db.commit()

    try:
        from src.messages.group_membership import sync_group_conversation_members
        sync_group_conversation_members(db, group_id)
        db.commit()
    except Exception:
        logger.exception("group chat member sync failed for group %s", group_id)
        db.rollback()

    return {"detail": f"Teacher '{teacher.name}' assigned to group '{group.name}'"}



# =============================================================================
# USER MANAGEMENT ENDPOINTS (ADMIN ONLY)
# =============================================================================

@router.get("/users", response_model=UserListResponse)
def get_all_users(
    skip: int = 0,
    limit: int = 50,
    role: Optional[str] = None,
    group_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    search: Optional[str] = None,
    all_students: bool = False,
    is_trial: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_teacher_curator_or_admin())
):
    """Get all users with filtering (teachers/curators see only their students, admin sees all).

    Curators may pass all_students=True to search across every student (still role=student
    only) — used when adding students to their own groups.
    """

    query = db.query(UserInDB)

    # Enforce role-based filtering for non-admins
    if current_user.role == "teacher":
        role = "student"  # Teachers can only see students
        # Filter by students in groups taught by this teacher
        teacher_group_student_ids = db.query(GroupStudent.student_id).join(Group).filter(Group.teacher_id == current_user.id).subquery()
        query = query.filter(UserInDB.id.in_(teacher_group_student_ids))
    elif current_user.role == "curator":
        role = "student"  # Curators can only ever see students (never staff)
        if not all_students:
            # Default: only students in groups managed by this curator
            curator_group_student_ids = db.query(GroupStudent.student_id).join(Group).filter(Group.curator_id == current_user.id).subquery()
            query = query.filter(UserInDB.id.in_(curator_group_student_ids))
        # all_students=True: curator may search every student (to add to their groups)
    elif current_user.role == "head_teacher":
        role = "student"
        from src.utils.permissions import get_head_teacher_group_ids

        head_teacher_group_ids = get_head_teacher_group_ids(current_user, db)
        if not head_teacher_group_ids:
            return UserListResponse(users=[], total=0, skip=skip, limit=limit)

        head_teacher_group_student_ids = db.query(GroupStudent.student_id).filter(
            GroupStudent.group_id.in_(head_teacher_group_ids)
        ).subquery()
        query = query.filter(UserInDB.id.in_(head_teacher_group_student_ids))

    # Apply filters
    if role:
        query = query.filter(UserInDB.role == role)
    if group_id is not None:
        # Filter by group using the association table
        query = query.join(GroupStudent, UserInDB.id == GroupStudent.student_id).filter(GroupStudent.group_id == group_id)
    if is_active is not None:
        query = query.filter(UserInDB.is_active == is_active)
    if is_trial is not None:
        query = query.filter(UserInDB.is_trial == is_trial)
    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            (UserInDB.name.ilike(search_filter)) |
            (UserInDB.email.ilike(search_filter)) |
            (UserInDB.student_id.ilike(search_filter))
        )

    # Get total count
    total = query.count()
    
    # Apply pagination
    users = query.offset(skip).limit(limit).all()
    
    # Batch enrich group/teacher/curator info for students to avoid N+1 queries
    result = []
    student_ids = [u.id for u in users if u.role == "student"]
    student_group_rows = []
    teacher_name_by_id = {}
    curator_name_by_id = {}
    if student_ids:
        student_group_rows = (
            db.query(
                GroupStudent.student_id,
                GroupStudent.group_id,
                Group.teacher_id,
                Group.curator_id,
            )
            .join(Group, Group.id == GroupStudent.group_id)
            .filter(GroupStudent.student_id.in_(student_ids))
            .all()
        )
        teacher_ids = {row.teacher_id for row in student_group_rows if row.teacher_id is not None}
        curator_ids = {row.curator_id for row in student_group_rows if row.curator_id is not None}
        if teacher_ids:
            teacher_rows = db.query(UserInDB.id, UserInDB.name).filter(UserInDB.id.in_(teacher_ids)).all()
            teacher_name_by_id = {r.id: r.name for r in teacher_rows}
        if curator_ids:
            curator_rows = db.query(UserInDB.id, UserInDB.name).filter(UserInDB.id.in_(curator_ids)).all()
            curator_name_by_id = {r.id: r.name for r in curator_rows}

    groups_by_student = {}
    teacher_names_by_student = {}
    curator_names_by_student = {}
    for row in student_group_rows:
        groups_by_student.setdefault(row.student_id, []).append(row.group_id)
        if row.teacher_id in teacher_name_by_id:
            teacher_names_by_student.setdefault(row.student_id, set()).add(teacher_name_by_id[row.teacher_id])
        if row.curator_id in curator_name_by_id:
            curator_names_by_student.setdefault(row.student_id, set()).add(curator_name_by_id[row.curator_id])

    for user in users:
        group_ids = groups_by_student.get(user.id, []) if user.role == "student" else []
        teacher_name = None
        curator_name = None
        if user.role == "student":
            teacher_name = ", ".join(sorted(teacher_names_by_student.get(user.id, set()))) or None
            curator_name = ", ".join(sorted(curator_names_by_student.get(user.id, set()))) or None

        result.append(UserSchema(
            id=user.id,
            email=user.email,
            name=user.name,
            role=user.role,
            avatar_url=user.avatar_url,
            is_active=user.is_active,
            is_trial=user.is_trial,
            student_id=user.student_id,
            teacher_name=teacher_name,
            curator_name=curator_name,
            group_ids=group_ids if group_ids else None,
            total_study_time_minutes=user.total_study_time_minutes,
            created_at=user.created_at
        ))
    
    return UserListResponse(
        users=result,
        total=total,
        skip=skip,
        limit=limit
    )


@router.get("/students/teacher-groups", response_model=TeacherGroupListResponse)
def get_students_teacher_groups(
    skip: int = 0,
    limit: int = 10,
    group_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_teacher_curator_or_admin()),
):
    """
    Paginated list of teacher groups (teacher -> count of students) for student management UI.
    Filters apply to students (search / is_active) and optionally group_id.
    """
    base_students = db.query(UserInDB.id).filter(UserInDB.role == "student")
    if is_active is not None:
        base_students = base_students.filter(UserInDB.is_active == is_active)
    if search:
        search_filter = f"%{search}%"
        base_students = base_students.filter(
            (UserInDB.name.ilike(search_filter)) |
            (UserInDB.email.ilike(search_filter)) |
            (UserInDB.student_id.ilike(search_filter))
        )
    base_students_sq = base_students.subquery()

    group_students_q = (
        db.query(
            Group.teacher_id.label("teacher_id"),
            func.count(func.distinct(GroupStudent.student_id)).label("total_students"),
        )
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .filter(GroupStudent.student_id.in_(base_students_sq))
    )
    if group_id is not None:
        group_students_q = group_students_q.filter(Group.id == group_id)

    group_students_q = group_students_q.group_by(Group.teacher_id)

    total_groups = group_students_q.count()
    rows = (
        group_students_q
        .order_by(desc("total_students"))
        .offset(skip)
        .limit(limit)
        .all()
    )

    teacher_ids = [r.teacher_id for r in rows if r.teacher_id is not None]
    teacher_name_by_id = {}
    if teacher_ids:
        teacher_rows = db.query(UserInDB.id, UserInDB.name).filter(UserInDB.id.in_(teacher_ids)).all()
        teacher_name_by_id = {r.id: r.name for r in teacher_rows}

    groups = [
        TeacherGroupSummary(
            teacher_id=r.teacher_id,
            teacher_name=teacher_name_by_id.get(r.teacher_id, "No Teacher Assigned") if r.teacher_id else "No Teacher Assigned",
            total_students=int(r.total_students or 0),
        )
        for r in rows
    ]

    return TeacherGroupListResponse(groups=groups, total=total_groups, skip=skip, limit=limit)


@router.get("/students/teacher-groups/{teacher_id}", response_model=TeacherGroupStudentsResponse)
def get_students_for_teacher_group(
    teacher_id: int,
    skip: int = 0,
    limit: int = 20,
    group_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_teacher_curator_or_admin()),
):
    """
    Paginated students for a given teacher group.
    teacher_id uses -1 for 'No Teacher Assigned'.
    """
    base_students = db.query(UserInDB).filter(UserInDB.role == "student")
    if is_active is not None:
        base_students = base_students.filter(UserInDB.is_active == is_active)
    if search:
        search_filter = f"%{search}%"
        base_students = base_students.filter(
            (UserInDB.name.ilike(search_filter)) |
            (UserInDB.email.ilike(search_filter)) |
            (UserInDB.student_id.ilike(search_filter))
        )

    gs = db.query(GroupStudent.student_id).join(Group, Group.id == GroupStudent.group_id)
    if teacher_id == -1:
        gs = gs.filter(Group.teacher_id.is_(None))
        teacher_name = "No Teacher Assigned"
    else:
        gs = gs.filter(Group.teacher_id == teacher_id)
        teacher = db.query(UserInDB).filter(UserInDB.id == teacher_id).first()
        teacher_name = teacher.name if teacher else "Unknown"
    if group_id is not None:
        gs = gs.filter(Group.id == group_id)
    student_ids_sq = gs.subquery()

    filtered = base_students.filter(UserInDB.id.in_(student_ids_sq))
    total = filtered.count()
    students = filtered.order_by(UserInDB.name.asc()).offset(skip).limit(limit).all()

    # Lightweight enrich: teacher/curator names for each student
    student_ids = [s.id for s in students]
    student_group_rows = []
    teacher_name_by_id = {}
    curator_name_by_id = {}
    if student_ids:
        student_group_rows = (
            db.query(
                GroupStudent.student_id,
                GroupStudent.group_id,
                Group.teacher_id,
                Group.curator_id,
            )
            .join(Group, Group.id == GroupStudent.group_id)
            .filter(GroupStudent.student_id.in_(student_ids))
            .all()
        )
        teacher_ids = {row.teacher_id for row in student_group_rows if row.teacher_id is not None}
        curator_ids = {row.curator_id for row in student_group_rows if row.curator_id is not None}
        if teacher_ids:
            teacher_rows = db.query(UserInDB.id, UserInDB.name).filter(UserInDB.id.in_(teacher_ids)).all()
            teacher_name_by_id = {r.id: r.name for r in teacher_rows}
        if curator_ids:
            curator_rows = db.query(UserInDB.id, UserInDB.name).filter(UserInDB.id.in_(curator_ids)).all()
            curator_name_by_id = {r.id: r.name for r in curator_rows}

    groups_by_student = {}
    teacher_names_by_student = {}
    curator_names_by_student = {}
    for row in student_group_rows:
        groups_by_student.setdefault(row.student_id, []).append(row.group_id)
        if row.teacher_id in teacher_name_by_id:
            teacher_names_by_student.setdefault(row.student_id, set()).add(teacher_name_by_id[row.teacher_id])
        if row.curator_id in curator_name_by_id:
            curator_names_by_student.setdefault(row.student_id, set()).add(curator_name_by_id[row.curator_id])

    out = []
    for u in students:
        group_ids = groups_by_student.get(u.id, [])
        t_name = ", ".join(sorted(teacher_names_by_student.get(u.id, set()))) or None
        c_name = ", ".join(sorted(curator_names_by_student.get(u.id, set()))) or None
        out.append(UserSchema(
            id=u.id,
            email=u.email,
            name=u.name,
            role=u.role,
            avatar_url=u.avatar_url,
            is_active=u.is_active,
            student_id=u.student_id,
            teacher_name=t_name,
            curator_name=c_name,
            group_ids=group_ids if group_ids else None,
            total_study_time_minutes=u.total_study_time_minutes,
            created_at=u.created_at,
        ))

    return TeacherGroupStudentsResponse(
        teacher_id=None if teacher_id == -1 else teacher_id,
        teacher_name=teacher_name,
        students=out,
        total=total,
        skip=skip,
        limit=limit,
    )

@router.put("/users/{user_id}", response_model=UserSchema)
def update_user(
    user_id: int,
    user_data: UpdateUserRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator())
):
    """Update a user (admin or head_curator for curators only)"""

    user = db.query(UserInDB).filter(UserInDB.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if current_user.role == "head_curator":
        if user.role != "curator":
            raise HTTPException(status_code=403, detail="Head curators can only edit curator accounts")
        if user_data.role is not None and user_data.role != "curator":
            raise HTTPException(status_code=403, detail="Head curators cannot change a curator's role to another role")
    
    # Check if email already exists (if changing email)
    if user_data.email and user_data.email != user.email:
        existing_user = db.query(UserInDB).filter(
            UserInDB.email == user_data.email,
            UserInDB.id != user_id
        ).first()
        if existing_user:
            raise HTTPException(status_code=400, detail="Email already registered")
    
    # Update fields
    if user_data.name is not None:
        user.name = user_data.name
    if user_data.email is not None:
        user.email = user_data.email
    if user_data.role is not None:
        user.role = user_data.role
    if user_data.student_id is not None:
        user.student_id = user_data.student_id
    if user_data.is_active is not None:
        user.is_active = user_data.is_active
    if user_data.is_analytics_hidden is not None:
        user.is_analytics_hidden = user_data.is_analytics_hidden
    if user_data.password is not None:
        from src.utils.password_policy import password_policy_error
        _pw_err = password_policy_error(user_data.password)
        if _pw_err:
            raise HTTPException(status_code=400, detail=_pw_err)
        user.hashed_password = hash_password(user_data.password)
        user.refresh_token = None  # Invalidate sessions
    
    user.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user)

    # If an admin set a new password, email it to the user
    if user_data.password is not None:
        background_tasks.add_task(
            send_password_changed_email, user.email, user.name or "", user_data.password
        )
        # Keep the Master Education (Zitadel) password in step — best-effort, off the response path.
        from src.services.zitadel_provisioning import mirror_password

        background_tasks.add_task(
            mirror_password, user.central_auth_user_id, user_data.password, lms_user_id=user.id
        )

    user_patch = user_data.model_dump(exclude_unset=True)
    final_role = user_data.role if user_data.role is not None else user.role

    if "group_ids" in user_patch and final_role == "student":
        _gc_affected_group_ids = _sync_student_groups(db, user_id, user_patch["group_ids"])
        db.commit()
        # Resync group-chat channels for every group the student joined or left.
        for _gc_gid in _gc_affected_group_ids:
            try:
                from src.messages.group_membership import sync_group_conversation_members
                sync_group_conversation_members(db, _gc_gid)
                db.commit()
            except Exception:
                logger.exception("group chat member sync failed for group %s", _gc_gid)
                db.rollback()
    
    # Update managed courses for Head Teacher
    if user_data.course_ids is not None and final_role == "head_teacher":
        # Remove all existing course associations
        db.query(CourseHeadTeacher).filter(CourseHeadTeacher.head_teacher_id == user_id).delete()
        db.flush()
        
        # Add new course associations
        for course_id in user_data.course_ids:
            course = db.query(Course).filter(Course.id == course_id).first()
            if course:
                db.add(CourseHeadTeacher(
                    course_id=course.id,
                    head_teacher_id=user_id
                ))
        db.commit()
    
    # Create response
    user_response = UserSchema.from_orm(user)
    
    return user_response

@router.post("/users/{user_id}/toggle-analytics-hidden", response_model=UserSchema)
def toggle_curator_analytics_hidden(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Toggle is_analytics_hidden flag for a curator (admin only)"""
    user = db.query(UserInDB).filter(UserInDB.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_analytics_hidden = not user.is_analytics_hidden
    user.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user)

    return UserSchema.from_orm(user)


@router.get("/users/{user_id}/groups")
def get_user_groups(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Get group IDs for a user (admin only)"""
    user = db.query(UserInDB).filter(UserInDB.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Get all groups for this user
    group_students = db.query(GroupStudent).filter(GroupStudent.student_id == user_id).all()
    group_ids = [gs.group_id for gs in group_students]
    
    return {"user_id": user_id, "group_ids": group_ids}

@router.post("/users/{user_id}/assign-group")
def assign_user_to_group(
    user_id: int,
    group_data: AssignUserToGroupRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Assign a user to a group (admin only)"""
    user = db.query(UserInDB).filter(UserInDB.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    group = db.query(Group).filter(Group.id == group_data.group_id).first()
    if not group:
        raise HTTPException(status_code=400, detail="Group not found")
    
    # Check if user is already in this group
    existing_association = db.query(GroupStudent).filter(
        GroupStudent.group_id == group_data.group_id,
        GroupStudent.student_id == user_id
    ).first()
    if existing_association:
        raise HTTPException(status_code=400, detail="User is already in this group")
    
    # Add user to group
    group_student = GroupStudent(
        group_id=group_data.group_id,
        student_id=user_id
    )
    db.add(group_student)
    db.commit()
    
    return {"detail": f"User '{user.name}' assigned to group '{group.name}'"}

@router.post("/users/bulk-assign-group")
def bulk_assign_users_to_group(
    bulk_data: BulkAssignUsersRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Bulk assign users to a group (admin only)"""
    group = db.query(Group).filter(Group.id == bulk_data.group_id).first()
    if not group:
        raise HTTPException(status_code=400, detail="Group not found")
    
    # Get all users to assign
    users = db.query(UserInDB).filter(UserInDB.id.in_(bulk_data.user_ids)).all()
    if len(users) != len(bulk_data.user_ids):
        raise HTTPException(status_code=400, detail="Some users not found")
    
    # Assign users to group
    assigned_count = 0
    for user in users:
        # Check if user is already in this group
        existing_association = db.query(GroupStudent).filter(
            GroupStudent.group_id == bulk_data.group_id,
            GroupStudent.student_id == user.id
        ).first()
        if not existing_association:
            group_student = GroupStudent(
                group_id=bulk_data.group_id,
                student_id=user.id
            )
            db.add(group_student)
            assigned_count += 1
    
    db.commit()
    
    return {"detail": f"{assigned_count} users assigned to group '{group.name}'"}

@router.get("/dashboard", response_model=AdminDashboardResponse)
@cached(namespace="admin:dashboard", ttl=60)
def get_admin_dashboard(
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Get admin dashboard data (admin only)"""
    # Get basic stats
    total_users = db.query(UserInDB).count()
    total_students = db.query(UserInDB).filter(UserInDB.role == "student", UserInDB.is_trial == False).count()
    total_teachers = db.query(UserInDB).filter(UserInDB.role == "teacher").count()
    total_curators = db.query(UserInDB).filter(UserInDB.role == "curator").count()
    total_courses = db.query(Course).count()
    total_active_enrollments = db.query(Enrollment).filter(Enrollment.is_active == True).count()
    
    # Recent registrations (last 7 days)
    week_ago = datetime.utcnow() - timedelta(days=7)
    recent_registrations = db.query(UserInDB).filter(
        UserInDB.created_at >= week_ago
    ).count()

    operational = _admin_operational_counts(db)
    today = datetime.utcnow().date()
    seven_days_ago = today - timedelta(days=7)
    thirty_days_ago = today - timedelta(days=30)
    seven_days_ago_dt = datetime.utcnow() - timedelta(days=7)

    teacher_active_last_7_days = db.query(UserInDB).filter(
        UserInDB.role == "teacher",
        UserInDB.last_activity_date.isnot(None),
        UserInDB.last_activity_date >= seven_days_ago,
    ).count()
    teacher_active_last_30_days = db.query(UserInDB).filter(
        UserInDB.role == "teacher",
        UserInDB.last_activity_date.isnot(None),
        UserInDB.last_activity_date >= thirty_days_ago,
    ).count()
    teachers_who_graded_last_7_days = db.query(
        func.count(func.distinct(AssignmentSubmission.graded_by))
    ).filter(
        AssignmentSubmission.is_graded == True,
        AssignmentSubmission.graded_by.isnot(None),
        AssignmentSubmission.graded_at.isnot(None),
        AssignmentSubmission.graded_at >= seven_days_ago_dt,
    ).scalar() or 0
    homework_graded_last_7_days = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.is_graded == True,
        AssignmentSubmission.graded_at.isnot(None),
        AssignmentSubmission.graded_at >= seven_days_ago_dt,
    ).count()
    avg_homework_graded_per_active_teacher_last_7_days = (
        round(homework_graded_last_7_days / teachers_who_graded_last_7_days, 1)
        if teachers_who_graded_last_7_days
        else 0.0
    )

    stats = AdminStatsResponse(
        total_users=total_users,
        total_students=total_students,
        total_teachers=total_teachers,
        total_curators=total_curators,
        total_courses=total_courses,
        total_active_enrollments=total_active_enrollments,
        recent_registrations=recent_registrations,
        teacher_active_last_7_days=teacher_active_last_7_days,
        teacher_active_last_30_days=teacher_active_last_30_days,
        teachers_who_graded_last_7_days=teachers_who_graded_last_7_days,
        homework_graded_last_7_days=homework_graded_last_7_days,
        avg_homework_graded_per_active_teacher_last_7_days=avg_homework_graded_per_active_teacher_last_7_days,
        **operational,
    )

    # Recent users (last 5) — nullslast so legacy rows without created_at don't break ordering
    recent_users = (
        db.query(UserInDB)
        .order_by(UserInDB.created_at.desc().nullslast(), UserInDB.id.desc())
        .limit(5)
        .all()
    )
    recent_users_data = [UserSchema.from_orm(user) for user in recent_users]
    
    # Recent groups (last 5)
    recent_groups = db.query(Group).order_by(desc(Group.created_at)).limit(5).all()
    recent_groups_data = []
    for group in recent_groups:
        teacher = db.query(UserInDB).filter(UserInDB.id == group.teacher_id).first() if group.teacher_id else None
        student_count = db.query(GroupStudent).filter(GroupStudent.group_id == group.id).count()
        
        group_data = GroupSchema(
            id=group.id,
            name=group.name,
            description=group.description,
            teacher_id=group.teacher_id,
            teacher_name=teacher.name if teacher else None,
            curator_id=group.curator_id,
            curator_name=None,  # Not needed for dashboard
            student_count=student_count,
            students=[],  # Not needed for dashboard
            created_at=group.created_at,
            is_active=group.is_active,
            is_special=group.is_special,
            is_over=group.is_over,
            group_type=getattr(group, "group_type", None) or "group",
            program_type=getattr(group, "program_type", None) or "general_english",
        )
        recent_groups_data.append(group_data)
    
    # Recent courses (last 5)
    recent_courses = db.query(Course).order_by(desc(Course.created_at)).limit(5).all()
    recent_courses_data = []
    for course in recent_courses:
        teacher = db.query(UserInDB).filter(UserInDB.id == course.teacher_id).first()
        module_count = db.query(Module).filter(Module.course_id == course.id).count()
        
        course_data = {
            "id": course.id,
            "title": course.title,
            "teacher_name": teacher.name if teacher else "Unknown",
            "module_count": module_count,
            "is_active": course.is_active,
            "created_at": course.created_at,
            "course_type": getattr(course, "course_type", None) or "general_english",
        }
        recent_courses_data.append(course_data)
    
    return AdminDashboardResponse(
        stats=stats,
        recent_users=recent_users_data,
        recent_groups=recent_groups_data,
        recent_courses=recent_courses_data
    )


@router.get("/dashboard/charts", response_model=AdminDashboardChartsResponse)
@cached(namespace="admin:dashboard-charts", ttl=120)
def get_admin_dashboard_charts(
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin()),
):
    """Time-series for charts (last 14 days, UTC day buckets)."""
    n = 14
    today = datetime.utcnow().date()
    registrations_last_14_days: List[AdminChartDayPoint] = []
    homework_submissions_last_14_days: List[AdminChartDayPoint] = []

    for i in range(n - 1, -1, -1):
        d = today - timedelta(days=i)
        start = datetime.combine(d, time.min)
        end = start + timedelta(days=1)
        registrations_last_14_days.append(
            AdminChartDayPoint(
                date=d.isoformat(),
                count=db.query(UserInDB).filter(
                    UserInDB.created_at >= start,
                    UserInDB.created_at < end,
                ).count(),
            )
        )
        homework_submissions_last_14_days.append(
            AdminChartDayPoint(
                date=d.isoformat(),
                count=db.query(AssignmentSubmission).filter(
                    AssignmentSubmission.submitted_at >= start,
                    AssignmentSubmission.submitted_at < end,
                ).count(),
            )
        )

    return AdminDashboardChartsResponse(
        registrations_last_14_days=registrations_last_14_days,
        homework_submissions_last_14_days=homework_submissions_last_14_days,
    )


# =============================================================================
# GROUP STUDENTS MANAGEMENT ENDPOINTS
# =============================================================================

@router.get("/groups/{group_id}/students", response_model=GroupStudentsResponse)
def get_group_students(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_teacher_curator_or_admin())
):
    """Get all students in a group"""
    # Check if group exists
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    # Check permissions
    if current_user.role == "teacher" and group.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    if current_user.role == "curator" and group.curator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get students in this group
    group_students = db.query(GroupStudent).filter(GroupStudent.group_id == group_id).all()
    students = []
    
    for group_student in group_students:
        student = db.query(UserInDB).filter(
            UserInDB.id == group_student.student_id,
            UserInDB.role == "student",
            UserInDB.is_active == True
        ).first()
        if student:
            students.append(UserSchema.from_orm(student))
    
    return GroupStudentsResponse(
        group_id=group_id,
        group_name=group.name,
        students=students,
        total_students=len(students)
    )

@router.post("/groups/{group_id}/students", response_model=dict)
def add_student_to_group(
    group_id: int,
    student_data: AddStudentToGroupRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_teacher_curator_or_admin())
):
    """Add a student to a group"""
    # Check if group exists
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    # Check permissions
    if current_user.role == "teacher" and group.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    if current_user.role == "curator" and group.curator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Check if student exists and is active
    student = db.query(UserInDB).filter(
        UserInDB.id == student_data.student_id,
        UserInDB.role == "student",
        UserInDB.is_active == True
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    # Check if student is already in this group
    existing_association = db.query(GroupStudent).filter(
        GroupStudent.group_id == group_id,
        GroupStudent.student_id == student_data.student_id
    ).first()
    if existing_association:
        raise HTTPException(status_code=400, detail="Student is already in this group")
    
    # Add student to group
    group_student = GroupStudent(
        group_id=group_id,
        student_id=student_data.student_id
    )
    db.add(group_student)
    db.commit()

    try:
        from src.messages.group_membership import sync_group_conversation_members
        sync_group_conversation_members(db, group_id)
        db.commit()
    except Exception:
        logger.exception("group chat member sync failed for group %s", group_id)
        db.rollback()

    return {"detail": f"Student '{student.name}' added to group '{group.name}'"}

@router.delete("/groups/{group_id}/students/{student_id}", response_model=dict)
def remove_student_from_group(
    group_id: int,
    student_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_teacher_curator_or_admin())
):
    """Remove a student from a group"""
    # Check if group exists
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    # Check permissions
    if current_user.role == "teacher" and group.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    if current_user.role == "curator" and group.curator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Check if student exists
    student = db.query(UserInDB).filter(
        UserInDB.id == student_id,
        UserInDB.role == "student"
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    # Check if student is in this group
    group_student = db.query(GroupStudent).filter(
        GroupStudent.group_id == group_id,
        GroupStudent.student_id == student_id
    ).first()
    if not group_student:
        raise HTTPException(status_code=400, detail="Student is not in this group")
    
    # Remove student from group
    db.delete(group_student)
    db.commit()

    try:
        from src.messages.group_membership import sync_group_conversation_members
        sync_group_conversation_members(db, group_id)
        db.commit()
    except Exception:
        logger.exception("group chat member sync failed for group %s", group_id)
        db.rollback()

    return {"detail": f"Student '{student.name}' removed from group '{group.name}'"}

@router.post("/groups/{group_id}/students/bulk", response_model=dict)
def bulk_add_students_to_group(
    group_id: int,
    student_ids: List[int],
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_teacher_curator_or_admin())
):
    """Add multiple students to a group"""
    # Check if group exists
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    # Check permissions
    if current_user.role == "teacher" and group.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    if current_user.role == "curator" and group.curator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Check if all students exist and are active
    students = db.query(UserInDB).filter(
        UserInDB.id.in_(student_ids),
        UserInDB.role == "student",
        UserInDB.is_active == True
    ).all()
    if len(students) != len(student_ids):
        raise HTTPException(status_code=400, detail="Some students not found")
    
    # Add students to group (skip if already in group)
    added_count = 0
    for student_id in student_ids:
        existing_association = db.query(GroupStudent).filter(
            GroupStudent.group_id == group_id,
            GroupStudent.student_id == student_id
        ).first()
        if not existing_association:
            group_student = GroupStudent(
                group_id=group_id,
                student_id=student_id
            )
            db.add(group_student)
            added_count += 1
    
    db.commit()

    try:
        from src.messages.group_membership import sync_group_conversation_members
        sync_group_conversation_members(db, group_id)
        db.commit()
    except Exception:
        logger.exception("group chat member sync failed for group %s", group_id)
        db.rollback()

    return {"detail": f"{added_count} students added to group '{group.name}'"}

# =============================================================================
# EVENT MANAGEMENT ENDPOINTS
# =============================================================================

@router.get("/events", response_model=List[EventSchema])
def get_all_events(
    skip: int = 0,
    limit: int = 100,
    event_type: Optional[str] = None,
    exclude_type: Optional[str] = None,
    group_id: Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Get all events with filtering options (admin only)"""
    query = db.query(Event).filter(Event.is_active == True)
    
    # Apply filters
    if event_type:
        query = query.filter(Event.event_type == event_type)
    if exclude_type:
        query = query.filter(Event.event_type != exclude_type)
    if start_date:
        query = query.filter(Event.start_datetime >= start_date)
    if end_date:
        query = query.filter(Event.end_datetime <= end_date)
    if group_id:
        query = query.join(EventGroup).filter(EventGroup.group_id == group_id)
    
    # Eager load relationships to avoid N+1
    query = query.options(
        joinedload(Event.creator),
        joinedload(Event.teacher),
        joinedload(Event.event_groups).joinedload(EventGroup.group)
    )
    
    events = query.order_by(Event.start_datetime).offset(skip).limit(limit).all()
    
    # Batch fetch participant counts
    event_ids = [e.id for e in events]
    count_map = {}
    if event_ids:
        participant_counts = db.query(
            EventParticipant.event_id, 
            func.count(EventParticipant.id)
        ).filter(
            EventParticipant.event_id.in_(event_ids)
        ).group_by(EventParticipant.event_id).all()
        count_map = {event_id: count for event_id, count in participant_counts}
    
    # Enrich with additional data
    result = []
    for event in events:
        event_data = EventSchema.from_orm(event)
        event_data.creator_name = event.creator.name if event.creator else "Unknown"
        event_data.teacher_name = event.teacher.name if event.teacher else None
        event_data.groups = [eg.group.name for eg in event.event_groups if eg.group]
        event_data.participant_count = count_map.get(event.id, 0)
        result.append(event_data)
        
    # Sort result
    result.sort(key=lambda x: x.start_datetime)
    return result

@router.post("/events", response_model=EventSchema)
def create_event(
    event_data: CreateEventRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Create a new event (admin only)"""
    
    # Validate event type
    valid_types = ["class", "weekly_test", "webinar"]
    if event_data.event_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Invalid event type. Must be one of: {valid_types}")
    
    # Validate datetime
    if event_data.start_datetime >= event_data.end_datetime:
        raise HTTPException(status_code=400, detail="Start datetime must be before end datetime")
    
    eligible_group_ids = get_non_special_group_ids(db, event_data.group_ids or [])
    if event_data.group_ids and not eligible_group_ids and not event_data.course_ids:
        raise HTTPException(
            status_code=400,
            detail="Selected groups are special and cannot be used for events"
        )

    # Validate courses exist
    if event_data.course_ids:
        courses = db.query(Course).filter(Course.id.in_(event_data.course_ids)).all()
        if len(courses) != len(event_data.course_ids):
            raise HTTPException(status_code=400, detail="One or more courses not found")
    
    # Create event
    event = Event(
        title=event_data.title,
        description=event_data.description,
        event_type=event_data.event_type,
        start_datetime=event_data.start_datetime,
        end_datetime=event_data.end_datetime,
        location=event_data.location,
        is_online=event_data.is_online,
        meeting_url=event_data.meeting_url,
        created_by=current_user.id,
        is_recurring=event_data.is_recurring,
        recurrence_pattern=event_data.recurrence_pattern,
        recurrence_end_date=event_data.recurrence_end_date,
        max_participants=event_data.max_participants,
        lesson_id=event_data.lesson_id,
        teacher_id=event_data.teacher_id
    )
    
    db.add(event)
    db.flush()  # To get the event ID
    
    # Create event-group associations
    for group_id in eligible_group_ids:
        event_group = EventGroup(event_id=event.id, group_id=group_id)
        db.add(event_group)

    # Create event-course associations
    from src.schemas.models import EventCourse
    for course_id in event_data.course_ids:
        event_course = EventCourse(event_id=event.id, course_id=course_id)
        db.add(event_course)
    
    event_data.group_ids = eligible_group_ids

    # If recurring, create additional events
    # Only create physical copies if an end date is specified.
    # If no end date, we rely on dynamic generation in retrieval endpoints.
    if event_data.is_recurring and event_data.recurrence_pattern and event_data.recurrence_end_date:
        create_recurring_events(db, event, event_data)
    
    db.commit()
    db.refresh(event)
    
    # Return enriched event data
    result = EventSchema.from_orm(event)
    creator = db.query(UserInDB).filter(UserInDB.id == event.created_by).first()
    result.creator_name = creator.name if creator else "Unknown"
    
    if event.teacher_id:
        teacher = db.query(UserInDB).filter(UserInDB.id == event.teacher_id).first()
        result.teacher_name = teacher.name if teacher else None
    
    result.groups = [eg.group.name for eg in event.event_groups if eg.group]
    result.group_ids = eligible_group_ids
    result.courses = [ec.course.title for ec in event.event_courses if ec.course]
    
    return result

@router.put("/events/{event_id}", response_model=EventSchema)
def update_event(
    event_id: int,
    event_data: UpdateEventRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Update an event (admin only)"""
    
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    
    # Update fields
    update_data = event_data.dict(exclude_unset=True)
    
    if "event_type" in update_data:
        valid_types = ["class", "weekly_test", "webinar"]
        if update_data["event_type"] not in valid_types:
            raise HTTPException(status_code=400, detail=f"Invalid event type. Must be one of: {valid_types}")
    
    if "start_datetime" in update_data and "end_datetime" in update_data:
        if update_data["start_datetime"] >= update_data["end_datetime"]:
            raise HTTPException(status_code=400, detail="Start datetime must be before end datetime")
    
    # Update group associations if provided
    if "group_ids" in update_data:
        validated_group_ids = get_non_special_group_ids(db, update_data["group_ids"] or [])
        existing_course_ids = [ec.course_id for ec in event.event_courses] if event.event_courses else []
        requested_course_ids = update_data.get("course_ids")
        effective_course_ids = requested_course_ids if requested_course_ids is not None else existing_course_ids
        if update_data["group_ids"] and not validated_group_ids and not effective_course_ids:
            raise HTTPException(
                status_code=400,
                detail="Selected groups are special and cannot be used for events"
            )
        
        # Remove existing associations
        db.query(EventGroup).filter(EventGroup.event_id == event_id).delete()
        
        # Create new associations
        for group_id in validated_group_ids:
            event_group = EventGroup(event_id=event_id, group_id=group_id)
            db.add(event_group)
        
        del update_data["group_ids"]

    # Update course associations if provided
    if "course_ids" in update_data:
        # Validate courses exist
        if update_data["course_ids"]:
            courses = db.query(Course).filter(Course.id.in_(update_data["course_ids"])).all()
            if len(courses) != len(update_data["course_ids"]):
                raise HTTPException(status_code=400, detail="One or more courses not found")
        
        # Remove existing associations
        from src.schemas.models import EventCourse
        db.query(EventCourse).filter(EventCourse.event_id == event_id).delete()
        
        # Create new associations
        for course_id in update_data["course_ids"]:
            event_course = EventCourse(event_id=event_id, course_id=course_id)
            db.add(event_course)
        
        del update_data["course_ids"]
    
    # Update event fields
    for field, value in update_data.items():
        setattr(event, field, value)
    
    event.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(event)
    
    # Return enriched event data
    result = EventSchema.from_orm(event)
    creator = db.query(UserInDB).filter(UserInDB.id == event.created_by).first()
    result.creator_name = creator.name if creator else "Unknown"
    
    if event.teacher_id:
        teacher = db.query(UserInDB).filter(UserInDB.id == event.teacher_id).first()
        result.teacher_name = teacher.name if teacher else None
    
    return result

@router.delete("/events/{event_id}")
def delete_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Delete an event (admin only)"""
    
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    
    # Soft delete - just mark as inactive
    event.is_active = False
    event.updated_at = datetime.utcnow()
    
    db.commit()
    
    return {"detail": "Event deleted successfully"}

@router.post("/events/bulk-delete")
def bulk_delete_events(
    event_ids: List[int],
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Bulk delete events (admin only)"""
    if not event_ids:
        return {"detail": "No events provided"}
    
    # Soft delete
    db.query(Event).filter(
        Event.id.in_(event_ids)
    ).update({
        Event.is_active: False,
        Event.updated_at: datetime.utcnow()
    }, synchronize_session=False)
    
    db.commit()
    
    return {"detail": f"Successfully deleted {len(event_ids)} events"}

@router.post("/events/bulk", response_model=List[EventSchema])
def create_bulk_events(
    events_data: List[CreateEventRequest],
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Create multiple events at once (admin only)"""
    
    created_events = []
    
    for event_data in events_data:
        # Validate event type
        valid_types = ["class", "weekly_test", "webinar"]
        if event_data.event_type not in valid_types:
            raise HTTPException(status_code=400, detail=f"Invalid event type. Must be one of: {valid_types}")
        
        # Validate datetime
        if event_data.start_datetime >= event_data.end_datetime:
            raise HTTPException(status_code=400, detail="Start datetime must be before end datetime")
        
        eligible_group_ids = get_non_special_group_ids(db, event_data.group_ids or [])
        if event_data.group_ids and not eligible_group_ids and not event_data.course_ids:
            raise HTTPException(
                status_code=400,
                detail="Selected groups are special and cannot be used for events"
            )

        # Create event
        event = Event(
            title=event_data.title,
            description=event_data.description,
            event_type=event_data.event_type,
            start_datetime=event_data.start_datetime,
            end_datetime=event_data.end_datetime,
            location=event_data.location,
            is_online=event_data.is_online,
            meeting_url=event_data.meeting_url,
            created_by=current_user.id,
            is_recurring=event_data.is_recurring,
            recurrence_pattern=event_data.recurrence_pattern,
            recurrence_end_date=event_data.recurrence_end_date,
            max_participants=event_data.max_participants
        )
        
        db.add(event)
        db.flush()
        
        # Create event-group associations
        for group_id in eligible_group_ids:
            event_group = EventGroup(event_id=event.id, group_id=group_id)
            db.add(event_group)
        
        created_events.append(event)
    
    db.commit()
    
    # Return enriched event data
    result = []
    for event in created_events:
        db.refresh(event)
        event_schema = EventSchema.from_orm(event)
        creator = db.query(UserInDB).filter(UserInDB.id == event.created_by).first()
        event_schema.creator_name = creator.name if creator else "Unknown"
        result.append(event_schema)
    
    return result

def create_recurring_events(db: Session, base_event: Event, event_data: CreateEventRequest):
    """Helper function to create recurring events"""
    from datetime import timedelta
    import calendar
    
    current_start = base_event.start_datetime
    current_end = base_event.end_datetime
    original_start_day = base_event.start_datetime.day
    original_end_day = base_event.end_datetime.day
    
    # Initial increment based on pattern
    if event_data.recurrence_pattern == "weekly":
        delta = timedelta(weeks=1)
        current_start += delta
        current_end += delta
    elif event_data.recurrence_pattern == "biweekly":
        delta = timedelta(weeks=2)
        current_start += delta
        current_end += delta
    elif event_data.recurrence_pattern == "daily":
        delta = timedelta(days=1)
        current_start += delta
        current_end += delta
    elif event_data.recurrence_pattern == "monthly":
        # For monthly, we don't use a fixed delta
        pass
    else:
        return  # Unsupported pattern
    
    # For monthly, we need to handle the first increment manually if we haven't already
    if event_data.recurrence_pattern == "monthly":
        # Add one month to start
        year = current_start.year + (current_start.month // 12)
        month = (current_start.month % 12) + 1
        day = min(original_start_day, calendar.monthrange(year, month)[1])
        current_start = current_start.replace(year=year, month=month, day=day)
        
        # Add one month to end
        year_end = current_end.year + (current_end.month // 12)
        month_end = (current_end.month % 12) + 1
        day_end = min(original_end_day, calendar.monthrange(year_end, month_end)[1])
        current_end = current_end.replace(year=year_end, month=month_end, day=day_end)
    
    while current_start.date() <= event_data.recurrence_end_date:
        # Check if event already exists for any of the target groups at this time
        existing_event = None
        for group_id in event_data.group_ids:
            existing = db.query(Event).join(EventGroup).filter(
                EventGroup.group_id == group_id,
                Event.start_datetime == current_start,
                Event.is_active == True
            ).first()
            if existing:
                existing_event = existing
                break
        
        if existing_event:
            # Skip this time slot - event already exists
            # Increment and continue
            if event_data.recurrence_pattern == "monthly":
                year = current_start.year + (current_start.month // 12)
                month = (current_start.month % 12) + 1
                day = min(original_start_day, calendar.monthrange(year, month)[1])
                current_start = current_start.replace(year=year, month=month, day=day)
                
                year_end = current_end.year + (current_end.month // 12)
                month_end = (current_end.month % 12) + 1
                day_end = min(original_end_day, calendar.monthrange(year_end, month_end)[1])
                current_end = current_end.replace(year=year_end, month=month_end, day=day_end)
            else:
                current_start += delta
                current_end += delta
            continue
        
        recurring_event = Event(
            title=base_event.title,
            description=base_event.description,
            event_type=base_event.event_type,
            start_datetime=current_start,
            end_datetime=current_end,
            location=base_event.location,
            is_online=base_event.is_online,
            meeting_url=base_event.meeting_url,
            created_by=base_event.created_by,
            is_recurring=False,  # Individual instances are not recurring
            max_participants=base_event.max_participants
        )
        
        db.add(recurring_event)
        db.flush()
        
        # Copy group associations
        for group_id in event_data.group_ids:
            event_group = EventGroup(event_id=recurring_event.id, group_id=group_id)
            db.add(event_group)

        # Copy course associations
        from src.schemas.models import EventCourse
        for course_id in event_data.course_ids:
            event_course = EventCourse(event_id=recurring_event.id, course_id=course_id)
            db.add(event_course)
        
        # Increment for next iteration
        if event_data.recurrence_pattern == "monthly":
            # Increment start
            year = current_start.year + (current_start.month // 12)
            month = (current_start.month % 12) + 1
            day = min(original_start_day, calendar.monthrange(year, month)[1])
            current_start = current_start.replace(year=year, month=month, day=day)
            
            # Increment end
            year_end = current_end.year + (current_end.month // 12)
            month_end = (current_end.month % 12) + 1
            day_end = min(original_end_day, calendar.monthrange(year_end, month_end)[1])
            current_end = current_end.replace(year=year_end, month=month_end, day=day_end)
        else:
            current_start += delta
            current_end += delta


# ---------------------------------------------------------------------------
# CRM: Teacher lessons count
# ---------------------------------------------------------------------------

class TeacherLessonsCountSchema(BaseModel):
    teacher_id: int
    year: int
    month: int
    count: int


@router.get(
    "/teachers/{teacher_id}/lessons-count",
    response_model=TeacherLessonsCountSchema,
    tags=["CRM"],
    summary="Count lessons conducted by a teacher in a given month",
)
def get_teacher_lessons_count(
    teacher_id: int,
    year: int = Query(..., ge=2020, le=2030, description="Year (Kazakhstan GMT+5)"),
    month: int = Query(..., ge=1, le=12, description="Month (1–12)"),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin()),
):
    """
    Returns the number of active class Events conducted by the given teacher
    in the specified calendar month using Kazakhstan timezone (GMT+5).

    Counting rules:
    - Active class events (event_type='class', is_active=True)
    - Conducted only (end_datetime <= now)
    - Substitutions: lesson counts for the substitute teacher (Event.teacher_id)
    - Schedule regeneration: old events deactivated (is_active=False), not counted

    Used by the CRM to calculate teacher workload / salary.
    """
    teacher = db.query(UserInDB).filter(UserInDB.id == teacher_id).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")

    kz_tz = _tz(timedelta(hours=5))
    month_start_kz = datetime(year, month, 1, tzinfo=kz_tz)
    if month == 12:
        month_end_kz = datetime(year + 1, 1, 1, tzinfo=kz_tz)
    else:
        month_end_kz = datetime(year, month + 1, 1, tzinfo=kz_tz)

    month_start_utc = month_start_kz.astimezone(_tz.utc)
    month_end_utc = month_end_kz.astimezone(_tz.utc)
    now_utc = datetime.now(_tz.utc)

    count = (
        db.query(Event)
        .filter(
            Event.teacher_id == teacher_id,
            Event.event_type == "class",
            Event.is_active == True,
            Event.start_datetime >= month_start_utc,
            Event.start_datetime < month_end_utc,
            Event.end_datetime <= now_utc,  # Count only actually conducted lessons
        )
        .count()
    )

    return TeacherLessonsCountSchema(
        teacher_id=teacher_id,
        year=year,
        month=month,
        count=count,
    )


class LessonDetailItem(BaseModel):
    id: int
    title: str
    start_datetime: str
    end_datetime: str


class TeacherLessonsDetailSchema(BaseModel):
    teacher_id: int
    year: int
    month: int
    count: int
    lessons: List[LessonDetailItem]


@router.get(
    "/teachers/{teacher_id}/lessons-detail",
    response_model=TeacherLessonsDetailSchema,
    tags=["CRM"],
    summary="List lessons conducted by a teacher in a given month (for audit/reconciliation)",
)
def get_teacher_lessons_detail(
    teacher_id: int,
    year: int = Query(..., ge=2020, le=2030, description="Year (Kazakhstan GMT+5)"),
    month: int = Query(..., ge=1, le=12, description="Month (1–12)"),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin()),
):
    """
    Returns the list of active class Events conducted by the given teacher
    in the specified calendar month. Same filters as lessons-count.
    Use for manual reconciliation with schedules / payroll.
    """
    teacher = db.query(UserInDB).filter(UserInDB.id == teacher_id).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")

    kz_tz = _tz(timedelta(hours=5))
    month_start_kz = datetime(year, month, 1, tzinfo=kz_tz)
    if month == 12:
        month_end_kz = datetime(year + 1, 1, 1, tzinfo=kz_tz)
    else:
        month_end_kz = datetime(year, month + 1, 1, tzinfo=kz_tz)

    month_start_utc = month_start_kz.astimezone(_tz.utc)
    month_end_utc = month_end_kz.astimezone(_tz.utc)
    now_utc = datetime.now(_tz.utc)

    events = (
        db.query(Event)
        .filter(
            Event.teacher_id == teacher_id,
            Event.event_type == "class",
            Event.is_active == True,
            Event.start_datetime >= month_start_utc,
            Event.start_datetime < month_end_utc,
            Event.end_datetime <= now_utc,
        )
        .order_by(Event.start_datetime.asc())
        .all()
    )

    lessons = [
        LessonDetailItem(
            id=e.id,
            title=e.title,
            start_datetime=e.start_datetime.isoformat(),
            end_datetime=e.end_datetime.isoformat(),
        )
        for e in events
    ]

    return TeacherLessonsDetailSchema(
        teacher_id=teacher_id,
        year=year,
        month=month,
        count=len(lessons),
        lessons=lessons,
    )


@router.get(
    "/teachers/lessons-count-export",
    tags=["CRM"],
    summary="Export all teachers' lessons count for a month as CSV",
)
def export_teachers_lessons_count_csv(
    year: int = Query(..., ge=2020, le=2030, description="Year (Kazakhstan GMT+5)"),
    month: int = Query(..., ge=1, le=12, description="Month (1–12)"),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin()),
):
    """
    Returns CSV: teacher_id, teacher_name, year, month, lessons_count.
    For reconciliation with payroll.
    """
    kz_tz = _tz(timedelta(hours=5))
    month_start_kz = datetime(year, month, 1, tzinfo=kz_tz)
    if month == 12:
        month_end_kz = datetime(year + 1, 1, 1, tzinfo=kz_tz)
    else:
        month_end_kz = datetime(year, month + 1, 1, tzinfo=kz_tz)

    month_start_utc = month_start_kz.astimezone(_tz.utc)
    month_end_utc = month_end_kz.astimezone(_tz.utc)
    now_utc = datetime.now(_tz.utc)

    teachers = (
        db.query(UserInDB)
        .filter(UserInDB.role == "teacher", UserInDB.is_active == True)
        .order_by(UserInDB.name.asc())
        .all()
    )

    rows = ["teacher_id,teacher_name,year,month,lessons_count"]
    for t in teachers:
        count = (
            db.query(Event)
            .filter(
                Event.teacher_id == t.id,
                Event.event_type == "class",
                Event.is_active == True,
                Event.start_datetime >= month_start_utc,
                Event.start_datetime < month_end_utc,
                Event.end_datetime <= now_utc,
            )
            .count()
        )
        name_escaped = (t.name or "").replace('"', '""')
        rows.append(f'{t.id},"{name_escaped}",{year},{month},{count}')

    csv_content = "\n".join(rows)
    filename = f"teachers_lessons_{year}_{month:02d}.csv"

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class ProvisionPlatformRequest(BaseModel):
    platform: Literal["ielts", "sat"]
    # SAT only; derived from the student's SAT/NUET groups when omitted. Ignored for IELTS.
    product: Optional[Literal["SAT", "NUET", "BOTH"]] = None


class ProvisionPlatformResponse(BaseModel):
    ok: bool
    outcome: str  # "created" | "exists"
    platform: str
    product: Optional[str] = None
    detail: str
    memberships_relinked: int


@router.post("/users/{user_id}/provision-platform", response_model=ProvisionPlatformResponse)
async def provision_user_to_platform(
    user_id: int,
    body: ProvisionPlatformRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin()),
):
    """Provision an LMS student onto SAT/NUET or IELTS — the LMS-side equivalent of the CRM
    «Создать аккаунт SAT/IELTS» button. Idempotent by email; never emails credentials (students
    log in via SSO / their LMS password). After creating the account it re-emits the student's
    memberships for that platform's programs (p14 touch) so the new account is linked to its groups.
    Staff (teacher/curator) are provisioned automatically by the sync drainer and use no button."""
    from src.services.sync_provision_gaps import (
        _is_test_email,
        platform_configured,
        provision_student_account,
        reemit_student_memberships,
        sat_product_for_student,
    )

    user = db.query(UserInDB).filter(UserInDB.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role != "student":
        raise HTTPException(
            status_code=400,
            detail="Only student accounts can be provisioned here; teachers/curators sync automatically.",
        )
    if getattr(user, "is_trial", False):
        raise HTTPException(
            status_code=400,
            detail="Trial accounts are LMS-only and are not provisioned to external platforms.",
        )

    email = (user.email or "").strip().lower()
    if not email or _is_test_email(email):
        raise HTTPException(status_code=400, detail="This account has no real email to provision.")

    platform = body.platform
    if not platform_configured(platform):
        raise HTTPException(
            status_code=503,
            detail=f"{platform.upper()} provisioning is not configured on this server.",
        )

    product = body.product
    if platform == "sat" and not product:
        product = sat_product_for_student(db, user_id)

    outcome, detail = provision_student_account(email, user.name, platform, product)
    if outcome == "error":
        # 502: we reached our own layer fine, the downstream platform rejected/was unreachable.
        raise HTTPException(status_code=502, detail=f"Provisioning failed: {detail}")

    relinked = reemit_student_memberships(db, user_id, platform)
    return ProvisionPlatformResponse(
        ok=True,
        outcome=outcome,
        platform=platform,
        product=product if platform == "sat" else None,
        detail=detail,
        memberships_relinked=relinked,
    )

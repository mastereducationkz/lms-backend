from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, desc
from typing import List, Optional
from pydantic import BaseModel, EmailStr
from datetime import datetime, timedelta, time, date

from src.config import get_db
from src.schemas.models import (
    UserInDB, UserSchema, Group, GroupSchema, GroupStudent, Course, Module, Enrollment, 
    StudentProgress, Assignment, AssignmentSubmission, AssignmentExtension, Event, EventGroup, EventParticipant,
    EventSchema, CreateEventRequest, UpdateEventRequest, EventGroupSchema, EventParticipantSchema,
    StepProgress, Step, Lesson, LessonSchedule, CourseGroupAccess, CourseHeadTeacher
)
from src.utils.auth_utils import hash_password
from src.utils.permissions import require_admin, require_teacher_or_admin_for_groups, require_teacher_curator_or_admin
import secrets
import string
import logging
from datetime import timezone as _tz

logger = logging.getLogger(__name__)


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

class BulkCreateUsersRequest(BaseModel):
    users: List[CreateUserRequest]
    notify_users: bool = False  # For future email notifications
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

class UpdateUserRequest(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    role: Optional[str] = None
    student_id: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None
    group_ids: Optional[List[int]] = None  # Update user's groups
    course_ids: Optional[List[int]] = None  # Update head teacher's courses

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
    teacher_id: int
    curator_id: Optional[int] = None
    course_id: Optional[int] = None  # Курс, к которому привязана группа
    is_active: bool = True
    is_special: bool = False

class UpdateGroupRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    teacher_id: Optional[int] = None
    curator_id: Optional[int] = None
    course_id: Optional[int] = None  # Курс, к которому привязана группа
    is_active: Optional[bool] = None
    is_special: Optional[bool] = None
    student_ids: Optional[List[int]] = None  # Update student list

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

def generate_password(length: int = 8) -> str:
    """Generate a random password"""
    characters = string.ascii_letters + string.digits
    return ''.join(secrets.choice(characters) for _ in range(length))

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

@router.post("/users/single", response_model=CreateUserResponse)
async def create_single_user(
    user_data: CreateUserRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Create a single user (admin only)"""
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
        
        return CreateUserResponse(
            user=UserSchema.from_orm(new_user),
            generated_password=generated_password
        )
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create user: {str(e)}")

@router.post("/users/bulk", response_model=BulkCreateResponse)
async def create_bulk_users(
    request: BulkCreateUsersRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Create multiple users at once (admin only)"""
    created_users = []
    failed_users = []
    
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
    else:
        db.rollback()
    
    return BulkCreateResponse(
        created_users=created_users,
        failed_users=failed_users
    )

@router.post("/users/bulk-text", response_model=BulkCreateResponse)
async def create_bulk_users_from_text(
    request: BulkCreateUsersFromTextRequest,
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
    else:
        db.rollback()
    
    return BulkCreateResponse(
        created_users=created_users,
        failed_users=failed_users
    )

@router.post("/create-admin", response_model=CreateAdminResponse)
async def create_admin(
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
async def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Delete user (admin only)"""
    user = db.query(UserInDB).filter(UserInDB.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Prevent deleting the last admin
    if user.role == "admin":
        admin_count = db.query(UserInDB).filter(UserInDB.role == "admin").count()
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last admin user")
            
    # Prevent deleting a teacher if they own groups
    group_count = db.query(Group).filter(Group.teacher_id == user_id).count()
    if group_count > 0:
        raise HTTPException(
            status_code=400, 
            detail=f"Cannot delete user. They are the teacher for {group_count} group(s). Please reassign or delete these groups first."
        )
    
    # Delete related records before user delete (avoid FK/ORM cascade issues)
    db.query(EventParticipant).filter(EventParticipant.user_id == user_id).delete()
    db.query(GroupStudent).filter(GroupStudent.student_id == user_id).delete()
    db.query(AssignmentExtension).filter(AssignmentExtension.student_id == user_id).delete()
    
    db.delete(user)
    db.commit()
    
    return {"detail": "User deleted successfully"}

@router.get("/stats", response_model=AdminStatsResponse)
async def get_admin_stats(
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Get platform statistics (admin only)"""
    # Basic counts
    total_users = db.query(UserInDB).count()
    total_students = db.query(UserInDB).filter(UserInDB.role == "student").count()
    total_teachers = db.query(UserInDB).filter(UserInDB.role == "teacher").count()
    total_curators = db.query(UserInDB).filter(UserInDB.role == "curator").count()
    total_courses = db.query(Course).count()
    total_active_enrollments = db.query(Enrollment).filter(Enrollment.is_active == True).count()
    
    # Recent registrations (last 7 days)
    week_ago = datetime.utcnow() - timedelta(days=7)
    recent_registrations = db.query(UserInDB).filter(UserInDB.created_at >= week_ago).count()
    
    return AdminStatsResponse(
        total_users=total_users,
        total_students=total_students,
        total_teachers=total_teachers,
        total_curators=total_curators,
        total_courses=total_courses,
        total_active_enrollments=total_active_enrollments,
        recent_registrations=recent_registrations
    )

@router.get("/students/progress", response_model=List[StudentProgressSummary])
async def get_students_progress_summary(
    skip: int = 0,
    limit: int = 50,
    group_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Get progress summary for all students (admin only)"""
    query = db.query(UserInDB).filter(UserInDB.role == "student")
    
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
async def reset_user_password(
    user_id: int,
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
    
    return {
        "detail": "Password reset successfully",
        "new_password": new_password,
        "user_email": user.email
    }

@router.get("/groups", response_model=List[GroupSchema])
async def get_all_groups(
    skip: int = 0,
    limit: int = 100,
    teacher_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_teacher_or_admin_for_groups())
):
    """Get all groups (teachers, head curators and admins)"""
    query = db.query(Group)
    
    # Teachers can only see their own groups, admins and head curators can see all
    if current_user.role == "teacher":
        query = query.filter(Group.teacher_id == current_user.id)
    elif teacher_id is not None:
        query = query.filter(Group.teacher_id == teacher_id)
    
    if is_active is not None:
        query = query.filter(Group.is_active == is_active)
    
    groups = query.offset(skip).limit(limit).all()
    # Enrich with teacher names, curator names and student counts
    result = []
    for group in groups:
        teacher = db.query(UserInDB).filter(UserInDB.id == group.teacher_id).first()
        curator = db.query(UserInDB).filter(UserInDB.id == group.curator_id).first() if group.curator_id else None
        # Get students for this group
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
                    teacher_name=teacher.name if teacher else "Unknown",
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
            teacher_name=teacher.name if teacher else "Unknown",
            curator_id=group.curator_id,
            curator_name=curator.name if curator else None,
            student_count=student_count,
            students=students,
            created_at=group.created_at,
            is_active=group.is_active,
            is_special=group.is_special,
            schedule_config=group.schedule_config
        )
        
        result.append(group_data)
    
    return result

# =============================================================================
# GROUP MANAGEMENT ENDPOINTS (ADMIN ONLY)
# =============================================================================

@router.post("/groups", response_model=GroupSchema)
async def create_group(
    group_data: CreateGroupRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Create a new group (admin only)"""
    # Check if teacher exists
    teacher = db.query(UserInDB).filter(
        UserInDB.id == group_data.teacher_id,
        UserInDB.role == "teacher"
    ).first()
    if not teacher:
        raise HTTPException(status_code=400, detail="Teacher not found")
    
    # Check if curator exists if provided
    curator = None
    if group_data.curator_id:
        curator = db.query(UserInDB).filter(
            UserInDB.id == group_data.curator_id,
            UserInDB.role == "curator"
        ).first()
        if not curator:
            raise HTTPException(status_code=400, detail="Curator not found")
    
    # Check if course exists if provided
    course = None
    if group_data.course_id:
        course = db.query(Course).filter(Course.id == group_data.course_id).first()
        if not course:
            raise HTTPException(status_code=400, detail="Course not found")
    
    # Check if group name already exists
    existing_group = db.query(Group).filter(Group.name == group_data.name).first()
    if existing_group:
        raise HTTPException(status_code=400, detail="Group name already exists")
    
    new_group = Group(
        name=group_data.name,
        description=group_data.description,
        teacher_id=group_data.teacher_id,
        curator_id=group_data.curator_id,
        is_active=group_data.is_active,
        is_special=group_data.is_special
    )
    
    db.add(new_group)
    db.commit()
    db.refresh(new_group)
    
    # If course_id provided, automatically grant access to the course
    if group_data.course_id:
        course_access = CourseGroupAccess(
            course_id=group_data.course_id,
            group_id=new_group.id,
            granted_by=current_user.id,
            is_active=True
        )
        db.add(course_access)
        db.commit()
    
    # Create response with teacher and curator names
    group_response = GroupSchema(
        id=new_group.id,
        name=new_group.name,
        description=new_group.description,
        teacher_id=new_group.teacher_id,
        teacher_name=teacher.name,
        curator_id=new_group.curator_id,
        curator_name=curator.name if curator else None,
        student_count=0,
        students=[],
        created_at=new_group.created_at,
        is_active=new_group.is_active,
        is_special=new_group.is_special,
        schedule_config=new_group.schedule_config
    )
    
    return group_response

@router.put("/groups/{group_id}", response_model=GroupSchema)
async def update_group(
    group_id: int,
    group_data: UpdateGroupRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Update a group (admin only)"""
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    
    # Check if new teacher exists if provided
    if group_data.teacher_id is not None:
        teacher = db.query(UserInDB).filter(
            UserInDB.id == group_data.teacher_id,
            UserInDB.role == "teacher"
        ).first()
        if not teacher:
            raise HTTPException(status_code=400, detail="Teacher not found")
    
    # Check if new curator exists if provided
    if group_data.curator_id is not None:
        if group_data.curator_id:
            curator = db.query(UserInDB).filter(
                UserInDB.id == group_data.curator_id,
                UserInDB.role == "curator"
            ).first()
            if not curator:
                raise HTTPException(status_code=400, detail="Curator not found")
        else:
            curator = None
    
    # Check if new course exists if provided
    if group_data.course_id is not None:
        if group_data.course_id:
            course = db.query(Course).filter(Course.id == group_data.course_id).first()
            if not course:
                raise HTTPException(status_code=400, detail="Course not found")
    
    # Check if new name already exists (if changing name)
    if group_data.name and group_data.name != group.name:
        existing_group = db.query(Group).filter(
            Group.name == group_data.name,
            Group.id != group_id
        ).first()
        if existing_group:
            raise HTTPException(status_code=400, detail="Group name already exists")
    
    # Update fields
    if group_data.name is not None:
        group.name = group_data.name
    if group_data.description is not None:
        group.description = group_data.description
    if group_data.teacher_id is not None:
        group.teacher_id = group_data.teacher_id
    if group_data.curator_id is not None:
        group.curator_id = group_data.curator_id
    if group_data.is_active is not None:
        group.is_active = group_data.is_active
    if group_data.is_special is not None:
        group.is_special = group_data.is_special
    
    # Update course access if provided
    if group_data.course_id is not None:
        # Remove existing course access for this group
        db.query(CourseGroupAccess).filter(
            CourseGroupAccess.group_id == group_id
        ).delete()
        
        # Add new course access if course_id is provided
        if group_data.course_id:
            course_access = CourseGroupAccess(
                course_id=group_data.course_id,
                group_id=group_id,
                granted_by=current_user.id,
                is_active=True
            )
            db.add(course_access)
    
    # Update student list if provided
    if group_data.student_ids is not None:
        # Remove all existing students from this group
        db.query(GroupStudent).filter(GroupStudent.group_id == group_id).delete()
        
        # Add new students
        for student_id in group_data.student_ids:
            # Verify student exists and is active
            student = db.query(UserInDB).filter(
                UserInDB.id == student_id,
                UserInDB.role == "student",
                UserInDB.is_active == True
            ).first()
            if student:
                group_student = GroupStudent(
                    group_id=group_id,
                    student_id=student_id
                )
                db.add(group_student)
    
    db.commit()
    db.refresh(group)
    
    # Create response with teacher name, curator name and student count
    teacher = db.query(UserInDB).filter(UserInDB.id == group.teacher_id).first()
    curator = db.query(UserInDB).filter(UserInDB.id == group.curator_id).first() if group.curator_id else None
    
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
        teacher_name=teacher.name if teacher else "Unknown",
        curator_id=group.curator_id,
        curator_name=curator.name if curator else None,
        student_count=len(students),
        students=students,
        created_at=group.created_at,
        is_active=group.is_active,
        is_special=group.is_special,
        schedule_config=group.schedule_config
    )
    
    return group_response

@router.delete("/groups/{group_id}")
async def delete_group(
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
async def bulk_schedule_upload(
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
async def assign_teacher_to_group(
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
    
    group.teacher_id = teacher_data.teacher_id
    db.commit()
    
    return {"detail": f"Teacher '{teacher.name}' assigned to group '{group.name}'"}



# =============================================================================
# USER MANAGEMENT ENDPOINTS (ADMIN ONLY)
# =============================================================================

@router.get("/users", response_model=UserListResponse)
async def get_all_users(
    skip: int = 0,
    limit: int = 50,
    role: Optional[str] = None,
    group_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_teacher_curator_or_admin())
):
    """Get all users with filtering (teachers/curators see only their students, admin sees all)"""
    
    query = db.query(UserInDB)
    
    # Enforce role-based filtering for non-admins
    if current_user.role == "teacher":
        role = "student"  # Teachers can only see students
        # Filter by students in groups taught by this teacher
        teacher_group_student_ids = db.query(GroupStudent.student_id).join(Group).filter(Group.teacher_id == current_user.id).subquery()
        query = query.filter(UserInDB.id.in_(teacher_group_student_ids))
    elif current_user.role == "curator":
        role = "student"  # Curators can only see students
        # Filter by students in groups managed by this curator
        curator_group_student_ids = db.query(GroupStudent.student_id).join(Group).filter(Group.curator_id == current_user.id).subquery()
        query = query.filter(UserInDB.id.in_(curator_group_student_ids))

    # Apply filters
    if role:
        query = query.filter(UserInDB.role == role)
    if group_id is not None:
        # Filter by group using the association table
        query = query.join(GroupStudent, UserInDB.id == GroupStudent.student_id).filter(GroupStudent.group_id == group_id)
    if is_active is not None:
        query = query.filter(UserInDB.is_active == is_active)
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
    
    # Enrich with group information (name, teacher, curator)
    result = []
    for user in users:
        # Get groups for this user first
        teacher_name = None
        curator_name = None
        group_ids = []
        
        if user.role == "student":
            
            # Check if there are any group associations for this student
            group_students = db.query(GroupStudent).filter(GroupStudent.student_id == user.id).all()
            
            if group_students:
                # Get all groups for this student
                teacher_names = []
                curator_names = []
                
                for group_student in group_students:
                    group_ids.append(group_student.group_id)
                    
                    group = db.query(Group).filter(Group.id == group_student.group_id).first()
                    
                    if group:
                        # Get teacher name
                        teacher = db.query(UserInDB).filter(UserInDB.id == group.teacher_id).first()
                        if teacher and teacher.name not in teacher_names:
                            teacher_names.append(teacher.name)
                        
                        # Get curator name from group.curator_id
                        if group.curator_id:
                            curator = db.query(UserInDB).filter(UserInDB.id == group.curator_id).first()
                            if curator and curator.name not in curator_names:
                                curator_names.append(curator.name)
                
                # Use the first teacher and curator (or combine them)
                teacher_name = ", ".join(teacher_names) if teacher_names else None
                curator_name = ", ".join(curator_names) if curator_names else None
        
        # Create UserSchema with group information
        user_data = UserSchema(
            id=user.id,
            email=user.email,
            name=user.name,
            role=user.role,
            avatar_url=user.avatar_url,
            is_active=user.is_active,
            student_id=user.student_id,
            teacher_name=teacher_name,
            curator_name=curator_name,
            group_ids=group_ids if group_ids else None,
            total_study_time_minutes=user.total_study_time_minutes,
            created_at=user.created_at
        )
        
        result.append(user_data)
    
    return UserListResponse(
        users=result,
        total=total,
        skip=skip,
        limit=limit
    )

@router.put("/users/{user_id}", response_model=UserSchema)
async def update_user(
    user_id: int,
    user_data: UpdateUserRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Update a user (admin only)"""
    
    user = db.query(UserInDB).filter(UserInDB.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
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
    if user_data.password is not None:
        user.hashed_password = hash_password(user_data.password)
        user.refresh_token = None  # Invalidate sessions
    
    user.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    
    # Update user's groups - check the FINAL role after updates
    final_role = user_data.role if user_data.role is not None else user.role
    
    # Always update groups if group_ids is provided (even if empty array to clear groups)
    if user_data.group_ids is not None and final_role == "student":
        # Remove all existing groups
        db.query(GroupStudent).filter(GroupStudent.student_id == user_id).delete()
        db.flush()
        
        # Add new groups
        for group_id in user_data.group_ids:
            group = db.query(Group).filter(Group.id == group_id).first()
            if group:
                db.add(GroupStudent(group_id=group_id, student_id=user_id))
        
        db.commit()
    
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

@router.get("/users/{user_id}/groups")
async def get_user_groups(
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

@router.delete("/users/{user_id}")
async def deactivate_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Deactivate a user (admin only) - soft delete"""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
    
    user = db.query(UserInDB).filter(UserInDB.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Soft delete
    user.is_active = False
    user.refresh_token = None  # Invalidate sessions
    user.updated_at = datetime.utcnow()
    db.commit()
    
    return {"detail": f"User '{user.name}' deactivated successfully"}

@router.post("/users/{user_id}/assign-group")
async def assign_user_to_group(
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
async def bulk_assign_users_to_group(
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
async def get_admin_dashboard(
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """Get admin dashboard data (admin only)"""
    # Get basic stats
    total_users = db.query(UserInDB).count()
    total_students = db.query(UserInDB).filter(UserInDB.role == "student").count()
    total_teachers = db.query(UserInDB).filter(UserInDB.role == "teacher").count()
    total_curators = db.query(UserInDB).filter(UserInDB.role == "curator").count()
    total_courses = db.query(Course).count()
    total_active_enrollments = db.query(Enrollment).filter(Enrollment.is_active == True).count()
    
    # Recent registrations (last 7 days)
    week_ago = datetime.utcnow() - timedelta(days=7)
    recent_registrations = db.query(UserInDB).filter(
        UserInDB.created_at >= week_ago
    ).count()
    
    stats = AdminStatsResponse(
        total_users=total_users,
        total_students=total_students,
        total_teachers=total_teachers,
        total_curators=total_curators,
        total_courses=total_courses,
        total_active_enrollments=total_active_enrollments,
        recent_registrations=recent_registrations
    )
    
    # Recent users (last 5)
    recent_users = db.query(UserInDB).order_by(desc(UserInDB.created_at)).limit(5).all()
    recent_users_data = [UserSchema.from_orm(user) for user in recent_users]
    
    # Recent groups (last 5)
    recent_groups = db.query(Group).order_by(desc(Group.created_at)).limit(5).all()
    recent_groups_data = []
    for group in recent_groups:
        teacher = db.query(UserInDB).filter(UserInDB.id == group.teacher_id).first()
        student_count = db.query(GroupStudent).filter(GroupStudent.group_id == group.id).count()
        
        group_data = GroupSchema(
            id=group.id,
            name=group.name,
            description=group.description,
            teacher_id=group.teacher_id,
            teacher_name=teacher.name if teacher else "Unknown",
            curator_id=group.curator_id,
            curator_name=None,  # Not needed for dashboard
            student_count=student_count,
            students=[],  # Not needed for dashboard
            created_at=group.created_at,
            is_active=group.is_active
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
            "created_at": course.created_at
        }
        recent_courses_data.append(course_data)
    
    return AdminDashboardResponse(
        stats=stats,
        recent_users=recent_users_data,
        recent_groups=recent_groups_data,
        recent_courses=recent_courses_data
    )

# =============================================================================
# GROUP STUDENTS MANAGEMENT ENDPOINTS
# =============================================================================

@router.get("/groups/{group_id}/students", response_model=GroupStudentsResponse)
async def get_group_students(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_teacher_or_admin_for_groups())
):
    """Get all students in a group"""
    # Check if group exists
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    
    # Check permissions
    if current_user.role == "teacher" and group.teacher_id != current_user.id:
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
async def add_student_to_group(
    group_id: int,
    student_data: AddStudentToGroupRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_teacher_or_admin_for_groups())
):
    """Add a student to a group"""
    # Check if group exists
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    
    # Check permissions
    if current_user.role == "teacher" and group.teacher_id != current_user.id:
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
    
    return {"detail": f"Student '{student.name}' added to group '{group.name}'"}

@router.delete("/groups/{group_id}/students/{student_id}", response_model=dict)
async def remove_student_from_group(
    group_id: int,
    student_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_teacher_or_admin_for_groups())
):
    """Remove a student from a group"""
    # Check if group exists
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    
    # Check permissions
    if current_user.role == "teacher" and group.teacher_id != current_user.id:
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
    
    return {"detail": f"Student '{student.name}' removed from group '{group.name}'"}

@router.post("/groups/{group_id}/students/bulk", response_model=dict)
async def bulk_add_students_to_group(
    group_id: int,
    student_ids: List[int],
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_teacher_or_admin_for_groups())
):
    """Add multiple students to a group"""
    # Check if group exists
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    
    # Check permissions
    if current_user.role == "teacher" and group.teacher_id != current_user.id:
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
    
    return {"detail": f"{added_count} students added to group '{group.name}'"}

# =============================================================================
# EVENT MANAGEMENT ENDPOINTS
# =============================================================================

@router.get("/events", response_model=List[EventSchema])
async def get_all_events(
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
async def create_event(
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
async def update_event(
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
async def delete_event(
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
async def bulk_delete_events(
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
async def create_bulk_events(
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

async def create_recurring_events(db: Session, base_event: Event, event_data: CreateEventRequest):
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
async def get_teacher_lessons_count(
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
async def get_teacher_lessons_detail(
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
async def export_teachers_lessons_count_csv(
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

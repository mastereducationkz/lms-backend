from fastapi import HTTPException, Depends
from sqlalchemy.orm import Session
from typing import List, Optional
from src.config import get_db
from src.schemas.models import UserInDB, Course, Group, Enrollment, CourseHeadTeacher, CourseTeacherAccess
from src.routes.auth import get_current_user_dependency

def require_role(allowed_roles: List[str]):
    """
    Dependency factory to require specific roles
    Usage: @require_role(["admin", "teacher"])
    """
    def role_checker(current_user: UserInDB = Depends(get_current_user_dependency)):
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=403, 
                detail=f"Access denied. Required roles: {', '.join(allowed_roles)}"
            )
        return current_user
    return role_checker

def require_admin():
    """Require admin role"""
    return require_role(["admin"])

def require_teacher_or_admin():
    """Require teacher, head teacher, or admin role"""
    return require_role(["teacher", "head_teacher", "admin"])

def require_teacher_or_admin_for_groups():
    """Require teacher, head teacher, head curator, or admin for group list reads."""
    def group_access_checker(current_user: UserInDB = Depends(get_current_user_dependency)):
        if current_user.role not in ["teacher", "head_teacher", "admin", "head_curator"]:
            raise HTTPException(
                status_code=403,
                detail="Access denied. Only teachers, head teachers, head curators, and admins can access groups."
            )
        return current_user
    return group_access_checker

def require_curator_or_admin():
    """Require curator or admin role"""
    return require_role(["curator", "admin"])

def require_admin_or_head_curator():
    """Require admin or head_curator role (for curator account management)"""
    return require_role(["admin", "head_curator"])

def require_teacher_curator_or_admin():
    """Require teacher, curator or admin role (includes head_curator and head_teacher)"""
    return require_role(["teacher", "curator", "admin", "head_curator", "head_teacher"])

def check_course_access(course_id: int, user: UserInDB, db: Session) -> bool:
    """
    Check if user has access to a specific course
    - Students: if enrolled OR if their group has access to the course
    - Teachers: only if they created the course
    - Curators: if they have access to students in the course
    - Admins: always
    """
    if user.role in ["admin", "head_curator"]:
        return True
    
    # Get course
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        return False
    
    if user.role == "teacher":
        # Teachers can access their own courses AND courses their groups have access to
        # Check if teacher created this course
        if course.teacher_id == user.id:
            return True
        
        # Check if teacher has direct access granted by admin
        direct_access = db.query(CourseTeacherAccess).filter(
            CourseTeacherAccess.course_id == course_id,
            CourseTeacherAccess.teacher_id == user.id,
            CourseTeacherAccess.is_active == True
        ).first()
        if direct_access:
            return True
        
        # Check if any of teacher's groups have access to this course
        from src.schemas.models import Group, CourseGroupAccess
        
        # Get groups where user is teacher
        teacher_groups = db.query(Group).filter(Group.teacher_id == user.id).all()
        
        if not teacher_groups:
            return False
        
        teacher_group_ids = [g.id for g in teacher_groups]
        
        # Check if any of teacher's groups have access to this course
        group_access = db.query(CourseGroupAccess).filter(
            CourseGroupAccess.group_id.in_(teacher_group_ids),
            CourseGroupAccess.course_id == course_id,
            CourseGroupAccess.is_active == True
        ).first()
        
        return group_access is not None
    
    elif user.role == "head_teacher":
        # Head teachers can access courses they are assigned to
        head_teacher_link = db.query(CourseHeadTeacher).filter(
            CourseHeadTeacher.course_id == course_id,
            CourseHeadTeacher.head_teacher_id == user.id
        ).first()
        return head_teacher_link is not None

    elif user.role == "student":
        if getattr(user, "is_trial", False):
            from src.trials.services import get_active_trial
            return get_active_trial(db, user.id, course_id) is not None

        # Students can access if their group has access to the course
        from src.schemas.models import GroupStudent, CourseGroupAccess
        
        # Find student's groups
        group_students = db.query(GroupStudent).filter(
            GroupStudent.student_id == user.id
        ).all()
        
        if not group_students:
            return False
        
        group_ids = [gs.group_id for gs in group_students]
        
        # Check if any of the student's groups have access to this course
        group_access = db.query(CourseGroupAccess).filter(
            CourseGroupAccess.group_id.in_(group_ids),
            CourseGroupAccess.course_id == course_id,
            CourseGroupAccess.is_active == True
        ).first()
        
        return group_access is not None
    
    elif user.role == "curator":
        # Curators can access courses if their groups have access to the course
        from src.schemas.models import GroupStudent, Group, CourseGroupAccess
        
        # Get groups where user is curator
        curator_groups = db.query(Group).filter(Group.curator_id == user.id).all()
        
        if not curator_groups:
            return False
        
        curator_group_ids = [g.id for g in curator_groups]
        
        # Check if any of curator's groups have access to this course
        group_access = db.query(CourseGroupAccess).filter(
            CourseGroupAccess.group_id.in_(curator_group_ids),
            CourseGroupAccess.course_id == course_id,
            CourseGroupAccess.is_active == True
        ).first()
        
        return group_access is not None
    
    return False

def require_course_access(course_id: int):
    """
    Dependency to require access to a specific course
    Usage: require_course_access(course_id)
    """
    def course_access_checker(
        current_user: UserInDB = Depends(get_current_user_dependency),
        db: Session = Depends(get_db)
    ):
        if not check_course_access(course_id, current_user, db):
            raise HTTPException(
                status_code=403,
                detail="Access denied to this course"
            )
        return current_user
    return course_access_checker

def check_student_access(student_id: int, user: UserInDB, db: Session) -> bool:
    """
    Check if user has access to a specific student's data
    - Students: only their own data
    - Teachers: only their students (enrolled in their courses)
    - Curators: only assigned students
    - Admins: all students
    """
    if user.role in ["admin", "head_curator"]:
        return True
    
    if user.role == "student":
        # Students can only access their own data
        return user.id == student_id

    if user.role == "parent":
        # Parents can access data for students they are linked to.
        from src.schemas.models import ParentStudent
        return db.query(ParentStudent).filter(
            ParentStudent.parent_id == user.id,
            ParentStudent.student_id == student_id,
        ).first() is not None

    student = db.query(UserInDB).filter(UserInDB.id == student_id).first()
    if not student or student.role != "student":
        return False
    
    if user.role == "teacher":
        from src.schemas.models import GroupStudent, Group, CourseGroupAccess
        
        # 1. Check if teacher is assigned to any of the student's groups
        student_groups = db.query(Group).join(GroupStudent).filter(
            GroupStudent.student_id == student_id,
            Group.teacher_id == user.id
        ).first()
        
        if student_groups:
            return True
            
        # 2. Check if student has access to any of the teacher's courses (legacy/creator access)
        teacher_courses = db.query(Course).filter(Course.teacher_id == user.id, Course.is_active == True).all()
        teacher_course_ids = [course.id for course in teacher_courses]
        
        if teacher_course_ids:
            # Найти все группы, в которых состоит студент
            group_students = db.query(GroupStudent).filter(GroupStudent.student_id == student_id).all()
            group_ids = [gs.group_id for gs in group_students]
            
            if group_ids:
                # Проверить, есть ли у этих групп доступ к курсам учителя
                access = db.query(CourseGroupAccess).filter(
                    CourseGroupAccess.group_id.in_(group_ids),
                    CourseGroupAccess.course_id.in_(teacher_course_ids),
                    CourseGroupAccess.is_active == True
                ).first()
                if access:
                    return True
        
        return False
    
    elif user.role == "curator":
        # Curators can access students in their assigned groups
        from src.schemas.models import GroupStudent, Group
        
        # Get groups where user is curator
        curator_groups = db.query(Group).filter(Group.curator_id == user.id).all()
        
        if not curator_groups:
            return False
        
        curator_group_ids = [g.id for g in curator_groups]
        
        # Check if student is in any of curator's groups
        student_in_group = db.query(GroupStudent).filter(
            GroupStudent.student_id == student_id,
            GroupStudent.group_id.in_(curator_group_ids)
        ).first()
        
        return student_in_group is not None

    elif user.role == "head_teacher":
        from src.schemas.models import GroupStudent, CourseGroupAccess

        assigned_course_ids = [
            row[0]
            for row in db.query(CourseHeadTeacher.course_id).filter(
                CourseHeadTeacher.head_teacher_id == user.id
            ).all()
        ]

        if not assigned_course_ids:
            return False

        student_group_ids = [
            row[0]
            for row in db.query(GroupStudent.group_id).filter(
                GroupStudent.student_id == student_id
            ).all()
        ]

        if not student_group_ids:
            return False

        has_course_access = db.query(CourseGroupAccess.id).filter(
            CourseGroupAccess.group_id.in_(student_group_ids),
            CourseGroupAccess.course_id.in_(assigned_course_ids),
            CourseGroupAccess.is_active == True
        ).first()

        return has_course_access is not None
    
    return False

def require_student_access(student_id: int):
    """
    Dependency to require access to a specific student
    Usage: require_student_access(student_id)
    """
    def student_access_checker(
        current_user: UserInDB = Depends(get_current_user_dependency),
        db: Session = Depends(get_db)
    ):
        if not check_student_access(student_id, current_user, db):
            raise HTTPException(
                status_code=403,
                detail="Access denied to this student's data"
            )
        return current_user
    return student_access_checker

def get_group_course_ids(db: Session, group_id: int) -> List[int]:
    from src.schemas.models import CourseGroupAccess

    return [
        row[0]
        for row in db.query(CourseGroupAccess.course_id).filter(
            CourseGroupAccess.group_id == group_id,
            CourseGroupAccess.is_active == True,
        ).all()
    ]


def get_head_teacher_group_ids(user: UserInDB, db: Session) -> List[int]:
    """Groups linked to courses managed by the head teacher."""
    from src.schemas.models import CourseGroupAccess

    managed_course_ids = [
        row[0]
        for row in db.query(CourseHeadTeacher.course_id).filter(
            CourseHeadTeacher.head_teacher_id == user.id
        ).all()
    ]
    if not managed_course_ids:
        return []

    return [
        row[0]
        for row in db.query(CourseGroupAccess.group_id).filter(
            CourseGroupAccess.course_id.in_(managed_course_ids),
            CourseGroupAccess.is_active == True,
        ).distinct().all()
    ]


def check_group_access(group_id: int, user: UserInDB, db: Session) -> bool:
    """
    Check if user has access to a specific group
    - Teachers: only if they created the group
    - Head teachers: groups linked to their managed courses
    - Curators: only their assigned groups
    - Head curators / admins: all groups
    """
    if user.role in ["admin", "head_curator"]:
        return True

    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        return False

    if user.role == "teacher":
        return group.teacher_id == user.id

    if user.role == "head_teacher":
        return group_id in get_head_teacher_group_ids(user, db)

    if user.role == "curator":
        return group.curator_id == user.id

    return False

def check_event_access(event_id: int, user: UserInDB, db: Session) -> bool:
    """
    Check if user has access to a specific event.
    - Admins / head_curator: always
    - Teachers: if event's groups have user as teacher, or event's courses have user as teacher
    - Curators: if event's groups have user as curator
    """
    if user.role in ["admin", "head_curator"]:
        return True

    from src.schemas.models import Event, EventGroup, EventCourse

    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        return False

    event_groups = db.query(EventGroup).filter(EventGroup.event_id == event_id).all()
    event_courses = db.query(EventCourse).filter(EventCourse.event_id == event_id).all()

    if user.role == "teacher":
        for eg in event_groups:
            group = db.query(Group).filter(Group.id == eg.group_id).first()
            if group and group.teacher_id == user.id:
                return True
        for ec in event_courses:
            course = db.query(Course).filter(Course.id == ec.course_id).first()
            if course and course.teacher_id == user.id:
                return True
        if event.teacher_id == user.id:
            return True

    elif user.role == "curator":
        for eg in event_groups:
            group = db.query(Group).filter(Group.id == eg.group_id).first()
            if group and group.curator_id == user.id:
                return True

    return False

def can_mark_event_attendance(event, user: UserInDB, db: Session) -> bool:
    """Who may *write* the register for one lesson.

    Deliberately stricter than :func:`check_event_access`, which answers "may this person see
    this lesson" and lets a teacher through on group ownership. Ownership is the wrong test
    for marking: the person who can say who was in the room is the person who was in the room.
    After Azamat hands a lesson to Nuray he still owns the group, so the read check keeps
    passing for him — and he could mark her lesson by posting the event id directly, even
    though the UI had already stopped offering it. Hiding it in the UI is not access control.

    * admin / head_teacher — yes. Supervisory correction has to remain possible, and both are
      accountable roles.
    * teacher — only when they are the lesson's actual teacher. Lessons that never recorded
      one fall back to the group's regular teacher, matching
      :func:`~src.services.payable_lessons.teacher_lesson_clause`, so legacy rows keep working.
    * anyone else (curators included) — no.
    """
    role = (user.role or "").strip().lower()
    if role in ("admin", "head_teacher"):
        return True
    if role != "teacher":
        return False

    if event.teacher_id is not None:
        return event.teacher_id == user.id

    from src.schemas.models import EventGroup

    owned = (
        db.query(EventGroup.event_id)
        .join(Group, Group.id == EventGroup.group_id)
        .filter(EventGroup.event_id == event.id, Group.teacher_id == user.id)
        .first()
    )
    return owned is not None


def require_group_access(group_id: int):
    """
    Dependency to require access to a specific group
    Usage: require_group_access(group_id)
    """
    def group_access_checker(
        current_user: UserInDB = Depends(get_current_user_dependency),
        db: Session = Depends(get_db)
    ):
        if not check_group_access(group_id, current_user, db):
            raise HTTPException(
                status_code=403,
                detail="Access denied to this group"
            )
        return current_user
    return group_access_checker

def can_create_course(user: UserInDB) -> bool:
    """Check if user can create courses"""
    return user.role in ["teacher", "admin"]

def can_edit_course(course_id: int, user: UserInDB, db: Session) -> bool:
    """Check if user can edit a specific course"""
    if user.role == "admin":
        return True
    
    if user.role == "teacher":
        course = db.query(Course).filter(Course.id == course_id).first()
        return course and course.teacher_id == user.id
    
    return False

def can_create_assignment(user: UserInDB) -> bool:
    """Check if user can create assignments"""
    return user.role in ["teacher", "admin"]

def can_grade_assignment(assignment_id: int, user: UserInDB, db: Session) -> bool:
    """Check if user can grade a specific assignment"""
    if user.role == "admin":
        return True
    
    if user.role == "teacher":
        # Teachers can grade assignments in their courses
        from src.schemas.models import Assignment, Lesson, Module
        assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
        if not assignment:
            return False
        
        if assignment.lesson_id:
            lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
            if lesson:
                module = db.query(Module).filter(Module.id == lesson.module_id).first()
                if module:
                    course = db.query(Course).filter(Course.id == module.course_id).first()
                    return course and course.teacher_id == user.id
        
        # For standalone assignments, check if teacher has rights
        # TODO: Implement standalone assignment access logic
        return True
    
    return False

# Role hierarchy for permission checking.
# "parent" is a low-privilege external role (≈ student level); parent-specific
# access is enforced explicitly via ParentStudent link checks, not the hierarchy.
ROLE_HIERARCHY = {
    "student": 1,
    "parent": 1,
    "curator": 2,
    "head_curator": 3,
    "teacher": 4,
    "head_teacher": 4,
    "admin": 5
}

def has_higher_or_equal_role(user_role: str, required_role: str) -> bool:
    """Check if user has higher or equal role than required"""
    return ROLE_HIERARCHY.get(user_role, 0) >= ROLE_HIERARCHY.get(required_role, 0)

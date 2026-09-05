from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, model_validator
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, desc, and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
import json
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta, date, timezone

from src.config import get_db
from src.schemas.models import (
    UserInDB, StudentProgress, Course, Module, Lesson, Step,
    StepProgress, StepProgressSchema, StepProgressCreateSchema,
    Assignment, AssignmentSubmission, Enrollment,
    GroupStudent, CourseGroupAccess, ProgressSchema,
    ProgressSnapshot, QuizAttempt, QuizAttemptSchema, QuizAttemptCreateSchema,
    QuizAttemptGradeSchema, QuizAttemptUpdateSchema,
    ManualLessonUnlock, ManualLessonUnlockSchema, ManualLessonUnlockCreateSchema,
    Group,
    PointHistory,
)
from src.routes.auth import get_current_user_dependency
from src.utils.permissions import check_course_access, check_student_access, check_group_access, require_teacher_or_admin
from src.progress.services.lesson_completion import (
    complete_steps_for_user,
    reset_steps_for_user,
    get_user_lesson_progress_summary,
    get_group_lesson_progress_summary,
)
from src.services.summary_cache import update_student_course_summary, update_summary_for_assignment
from src.services.cache_service import cached, invalidate
from src.utils.course_access import get_user_courses, student_has_only_special_groups
from src.utils.quiz_passing_score import resolve_quiz_passing_score_percent


router = APIRouter()
_progress_log = logging.getLogger(__name__)

COURSE_QUIZ_POINTS_MIN = 8
COURSE_QUIZ_POINTS_MAX = 24


def _course_quiz_points_already_awarded(db: Session, user_id: int, step_id: int) -> bool:
    return (
        db.query(PointHistory)
        .filter(
            PointHistory.user_id == user_id,
            PointHistory.reason == "course_quiz",
            PointHistory.description.like(f"step:{step_id}|%"),
        )
        .first()
        is not None
    )


def _maybe_award_course_quiz_points(
    db: Session,
    *,
    user_id: int,
    step_id: int,
    course_id: int,
    score_percentage: float,
    is_graded: bool,
    attempt_id: int,
) -> None:
    if not is_graded:
        return
    try:
        pct = float(score_percentage)
    except (TypeError, ValueError):
        return

    step = db.query(Step).filter(Step.id == step_id).first()
    # Checkpoints are pilot-only assessments: awarding points would let pilot students outscore
    # classmates who cannot take them (user decision, 2026-09-04).
    if step is not None:
        lesson_kind = db.query(Lesson.kind).filter(Lesson.id == step.lesson_id).scalar()
        if lesson_kind == "checkpoint":
            return
    pass_pct = resolve_quiz_passing_score_percent(
        step.content_text if step else None,
        is_optional=bool(step.is_optional) if step else False,
    )
    if pct < pass_pct:
        return
    if _course_quiz_points_already_awarded(db, user_id, step_id):
        return
    from src.gamification.routes.gamification import award_points

    span = 100.0 - pass_pct
    ratio = min(1.0, max(0.0, (pct - pass_pct) / span)) if span > 0 else 1.0
    amount = int(COURSE_QUIZ_POINTS_MIN + ratio * (COURSE_QUIZ_POINTS_MAX - COURSE_QUIZ_POINTS_MIN))
    amount = max(COURSE_QUIZ_POINTS_MIN, min(COURSE_QUIZ_POINTS_MAX, amount))
    try:
        award_points(
            db,
            user_id,
            amount,
            "course_quiz",
            f"step:{step_id}|course:{course_id}|attempt:{attempt_id}",
        )
    except Exception as e:
        _progress_log.warning("course_quiz award_points failed: %s", e, exc_info=True)


# =============================================================================
# DAILY STREAK HELPER FUNCTIONS
# =============================================================================

def update_daily_streak(user: UserInDB, db: Session):
    """
    Update user's daily streak based on current activity.
    
    Logic:
    - If user is active today and was active yesterday: increment streak
    - If user is active today but wasn't active yesterday: reset streak to 1
    - If user hasn't been active today yet: start/continue streak
    """
    today = date.today()
    yesterday = today - timedelta(days=1)
    
    # If user was already active today, don't update again
    if user.last_activity_date == today:
        return
    
    # Calculate new streak based on previous last_activity_date
    previous_activity_date = user.last_activity_date
    
    # Update last activity date to today
    user.last_activity_date = today
    
    # Calculate new streak
    if previous_activity_date is None:
        # First time activity
        user.daily_streak = 1
    elif previous_activity_date == yesterday:
        # Consecutive day activity
        user.daily_streak += 1
    elif previous_activity_date < yesterday:
        # Gap in activity, reset streak
        user.daily_streak = 1
    
    db.commit()

# =============================================================================
# PROGRESS UPDATE FUNCTIONS
# =============================================================================

def update_student_progress(user_id: int, course_id: int, db: Session):
    """
    Обновить или создать запись прогресса студента по курсу
    Используем существующую модель StudentProgress для общего прогресса по курсу
    """
    # Получаем все шаги курса
    total_steps = db.query(func.count(Step.id)).join(Lesson).join(Module).filter(
        Module.course_id == course_id
    ).scalar() or 0
    
    # Получаем завершенные шаги
    completed_steps = db.query(func.count(StepProgress.id)).join(Step).join(Lesson).join(Module).filter(
        Module.course_id == course_id,
        StepProgress.user_id == user_id,
        StepProgress.status == 'completed'
    ).scalar() or 0
    
    # Получаем общее время изучения
    total_time = db.query(func.sum(StepProgress.time_spent_minutes)).join(Step).join(Lesson).join(Module).filter(
        Module.course_id == course_id,
        StepProgress.user_id == user_id
    ).scalar() or 0
    
    # Рассчитываем процент завершения
    completion_percentage = int((completed_steps / total_steps * 100)) if total_steps > 0 else 0
    
    # Находим или создаем запись общего прогресса по курсу (без lesson_id и assignment_id)
    student_progress = db.query(StudentProgress).filter(
        StudentProgress.user_id == user_id,
        StudentProgress.course_id == course_id,
        StudentProgress.lesson_id.is_(None),
        StudentProgress.assignment_id.is_(None)
    ).first()
    
    if not student_progress:
        # Создаем новую запись общего прогресса по курсу
        student_progress = StudentProgress(
            user_id=user_id,
            course_id=course_id,
            lesson_id=None,  # Общий прогресс по курсу
            assignment_id=None,
            status="in_progress" if completion_percentage > 0 else "not_started",
            completion_percentage=completion_percentage,
            time_spent_minutes=int(total_time) if total_time else 0,
            last_accessed=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc) if completion_percentage >= 100 else None
        )
        db.add(student_progress)
    else:
        # Обновляем существующую запись
        student_progress.completion_percentage = completion_percentage
        student_progress.time_spent_minutes = int(total_time) if total_time else 0
        student_progress.last_accessed = datetime.now(timezone.utc)
        
        # Обновляем статус
        if completion_percentage >= 100:
            student_progress.status = "completed"
            if not student_progress.completed_at:
                student_progress.completed_at = datetime.now(timezone.utc)
        elif completion_percentage > 0:
            student_progress.status = "in_progress"
        else:
            student_progress.status = "not_started"
    
    db.commit()
    return student_progress

def create_progress_snapshot(user_id: int, course_id: int, db: Session):
    """
    Создать или обновить снимок прогресса за сегодня (user + course + день).
    Использует upsert, чтобы не падать при гонке двух запросов и обновлять метрики при повторных визитах.
    """
    today = date.today()

    student_progress = db.query(StudentProgress).filter(
        StudentProgress.user_id == user_id,
        StudentProgress.course_id == course_id,
        StudentProgress.lesson_id.is_(None),
        StudentProgress.assignment_id.is_(None)
    ).first()

    if not student_progress:
        return None

    total_steps = db.query(func.count(Step.id)).join(Lesson).join(Module).filter(
        Module.course_id == course_id
    ).scalar() or 0

    completed_steps = db.query(func.count(StepProgress.id)).join(Step).join(Lesson).join(Module).filter(
        Module.course_id == course_id,
        StepProgress.user_id == user_id,
        StepProgress.status == 'completed'
    ).scalar() or 0

    total_assignments = db.query(func.count(Assignment.id)).join(Lesson).join(Module).filter(
        Module.course_id == course_id
    ).scalar() or 0

    completed_assignments = db.query(func.count(AssignmentSubmission.id)).join(Assignment).join(Lesson).join(Module).filter(
        Module.course_id == course_id,
        AssignmentSubmission.user_id == user_id,
        AssignmentSubmission.is_graded == True
    ).scalar() or 0

    avg_assignment_score = db.query(func.avg(AssignmentSubmission.score)).join(Assignment).join(Lesson).join(Module).filter(
        Module.course_id == course_id,
        AssignmentSubmission.user_id == user_id,
        AssignmentSubmission.is_graded == True
    ).scalar() or 0

    assignment_pct = float(avg_assignment_score) if avg_assignment_score else 0.0
    completion_pct = float(student_progress.completion_percentage)

    stmt = pg_insert(ProgressSnapshot).values(
        user_id=user_id,
        course_id=course_id,
        snapshot_date=today,
        completed_steps=completed_steps,
        total_steps=total_steps,
        completion_percentage=completion_pct,
        total_time_spent_minutes=student_progress.time_spent_minutes,
        assignments_completed=completed_assignments,
        total_assignments=total_assignments,
        assignment_score_percentage=assignment_pct,
        created_at=datetime.now(timezone.utc),
    )
    stmt = stmt.on_conflict_do_update(
        constraint='uq_progress_snapshot',
        set_={
            'completed_steps': stmt.excluded.completed_steps,
            'total_steps': stmt.excluded.total_steps,
            'completion_percentage': stmt.excluded.completion_percentage,
            'total_time_spent_minutes': stmt.excluded.total_time_spent_minutes,
            'assignments_completed': stmt.excluded.assignments_completed,
            'total_assignments': stmt.excluded.total_assignments,
            'assignment_score_percentage': stmt.excluded.assignment_score_percentage,
        },
    )
    db.execute(stmt)
    db.commit()

    return db.query(ProgressSnapshot).filter(
        ProgressSnapshot.user_id == user_id,
        ProgressSnapshot.course_id == course_id,
        ProgressSnapshot.snapshot_date == today,
    ).first()

# =============================================================================
# PROGRESS TRACKING
# =============================================================================

@router.get("/my", response_model=List[ProgressSchema])
@cached(
    namespace="progress:my",
    ttl=45,
    key_args=("course_id", "lesson_id", "skip", "limit"),
)
def get_my_progress(
    course_id: Optional[int] = None,
    lesson_id: Optional[int] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, le=1000),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить прогресс текущего пользователя"""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can access this endpoint")
    
    query = db.query(StudentProgress).filter(StudentProgress.user_id == current_user.id)
    
    if course_id:
        query = query.filter(StudentProgress.course_id == course_id)
    if lesson_id:
        query = query.filter(StudentProgress.lesson_id == lesson_id)
    
    progress_records = query.order_by(desc(StudentProgress.last_accessed)).offset(skip).limit(limit).all()
    return [ProgressSchema.from_orm(record) for record in progress_records]

@router.get("/course/{course_id}")
@cached(namespace="progress:course", ttl=60, key_args=("course_id", "student_id"))
def get_course_progress(
    course_id: int,
    student_id: Optional[int] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить детальный прогресс по курсу"""
    
    # Определяем, чей прогресс смотрим
    target_student_id = student_id if student_id else current_user.id
    
    # Проверяем права доступа
    if current_user.role == "student" and target_student_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    elif current_user.role in ["teacher", "curator"] and not check_student_access(target_student_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this student")
    elif not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")
    
    # Получаем информацию о курсе
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Получаем модули курса
    modules = db.query(Module).filter(Module.course_id == course_id).order_by(Module.order_index).all()
    
    course_progress = {
        "course_id": course_id,
        "course_title": course.title,
        "student_id": target_student_id,
        "overall_progress": 0,
        "total_time_spent": 0,
        "modules": []
    }
    
    total_lessons = 0
    completed_lessons = 0
    total_time = 0
    
    for module in modules:
        # Получаем уроки модуля
        lessons = db.query(Lesson).filter(Lesson.module_id == module.id).order_by(Lesson.order_index).all()
        
        module_data = {
            "module_id": module.id,
            "module_title": module.title,
            "lessons": [],
            "module_progress": 0,
            "time_spent": 0
        }
        
        module_completed = 0
        module_time = 0
        
        for lesson in lessons:
            total_lessons += 1
            
            # Получаем прогресс по уроку
            lesson_progress = db.query(StudentProgress).filter(
                StudentProgress.user_id == target_student_id,
                StudentProgress.lesson_id == lesson.id
            ).first()
            
            # Получаем задания урока
            assignments = db.query(Assignment).filter(Assignment.lesson_id == lesson.id).all()
            assignment_scores = []
            
            for assignment in assignments:
                submission = db.query(AssignmentSubmission).filter(
                    AssignmentSubmission.assignment_id == assignment.id,
                    AssignmentSubmission.user_id == target_student_id
                ).first()
                
                if submission:
                    assignment_scores.append({
                        "assignment_id": assignment.id,
                        "assignment_title": assignment.title,
                        "score": submission.score,
                        "max_score": submission.max_score,
                        "submitted_at": submission.submitted_at
                    })
            
            lesson_data = {
                "lesson_id": lesson.id,
                "lesson_title": lesson.title,
                "status": lesson_progress.status if lesson_progress else "not_started",
                "completion_percentage": lesson_progress.completion_percentage if lesson_progress else 0,
                "time_spent": lesson_progress.time_spent_minutes if lesson_progress else 0,
                "last_accessed": lesson_progress.last_accessed if lesson_progress else None,
                "assignments": assignment_scores
            }
            
            if lesson_progress and lesson_progress.status == "completed":
                completed_lessons += 1
                module_completed += 1
            
            if lesson_progress:
                module_time += lesson_progress.time_spent_minutes
                total_time += lesson_progress.time_spent_minutes
            
            module_data["lessons"].append(lesson_data)
        
        # Вычисляем прогресс модуля
        if lessons:
            module_data["module_progress"] = (module_completed / len(lessons)) * 100
        module_data["time_spent"] = module_time
        
        course_progress["modules"].append(module_data)
    
    # Вычисляем общий прогресс курса
    if total_lessons > 0:
        course_progress["overall_progress"] = (completed_lessons / total_lessons) * 100
    course_progress["total_time_spent"] = total_time
    
    return course_progress

@router.post("/lesson/{lesson_id}/complete")
def mark_lesson_complete(
    lesson_id: int,
    time_spent: int = 0,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Отметить урок как завершенный"""
    if current_user.role not in ["student", "teacher", "admin", "curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    # Проверяем доступ к курсу
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not check_course_access(module.course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this lesson")
    
    # Находим или создаем запись прогресса
    progress = db.query(StudentProgress).filter(
        StudentProgress.user_id == current_user.id,
        StudentProgress.lesson_id == lesson_id
    ).first()
    
    if not progress:
        progress = StudentProgress(
            user_id=current_user.id,
            course_id=module.course_id,
            lesson_id=lesson_id,
            status="completed",
            completion_percentage=100,
            time_spent_minutes=time_spent,
            last_accessed=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc)
        )
        db.add(progress)
    else:
        progress.status = "completed"
        progress.completion_percentage = 100
        progress.time_spent_minutes += time_spent
        progress.last_accessed = datetime.now(timezone.utc)
        progress.completed_at = datetime.now(timezone.utc)
    
    # Обновляем общее время изучения пользователя
    current_user.total_study_time_minutes += time_spent
    
    # Обновляем daily streak
    update_daily_streak(current_user, db)

    db.commit()

    # Completing a lesson changes is_completed and unlocks the next lesson (accessibility
    # is derived on read). Drop the cached course structure + progress so clients see it
    # immediately instead of waiting out the 60s TTL.
    invalidate("courses:modules:*", "courses:module-lessons:*", "progress:*")

    # Report assignments that just became ready-to-submit (all linked lessons now
    # completed) so the frontend can show a "you can submit this HW now" popup.
    from src.assignments.models import AssignmentLinkedLesson, AssignmentSubmission, Assignment
    from src.assignments.routes.assignments import assignment_ready_for_student, _student_assignments

    linked_assignment_ids = [
        row[0] for row in db.query(AssignmentLinkedLesson.assignment_id).filter(
            AssignmentLinkedLesson.lesson_id == lesson_id).all()
    ]
    visible_ids = {a.id for a in _student_assignments(db, current_user.id)}
    newly_ready = []
    for aid in set(linked_assignment_ids):
        if aid not in visible_ids:
            continue
        assignment = db.query(Assignment).filter(Assignment.id == aid).first()
        if not assignment:
            continue
        has_sub = db.query(AssignmentSubmission).filter(
            AssignmentSubmission.assignment_id == aid,
            AssignmentSubmission.user_id == current_user.id,
            AssignmentSubmission.is_hidden == False).first() is not None
        if has_sub:
            continue
        if assignment_ready_for_student(current_user.id, assignment, db)["ready"]:
            newly_ready.append({"id": assignment.id, "title": assignment.title})

    # SAT Checkpoints: a completed unit may be the last required one of a block.
    from src.checkpoints import service as checkpoint_service
    try:
        newly_opened = checkpoint_service.sync_student_checkpoints(db, current_user.id, commit=True)
    except Exception:  # never fail an already-committed lesson completion because of checkpoint bookkeeping
        _progress_log.exception("checkpoint sync failed for user %s after lesson %s", current_user.id, lesson_id)
        db.rollback()
        newly_opened = []

    return {
        "detail": "Lesson marked as complete",
        "time_spent": time_spent,
        "newly_ready_assignments": newly_ready,
        "newly_opened_checkpoints": [
            {"checkpoint_id": r.checkpoint_id, "number": r.checkpoint_number,
             "title": r.definition.title if r.definition else f"Checkpoint {r.checkpoint_number}",
             "deadline": checkpoint_service.naive(r.deadline).isoformat() + "Z" if r.deadline else None}
            for r in newly_opened
        ],
    }

@router.post("/lesson/{lesson_id}/start")
def start_lesson(
    lesson_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Начать изучение урока"""
    if current_user.role not in ["student", "teacher", "admin", "curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    # Проверяем доступ к курсу
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not check_course_access(module.course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this lesson")
    
    # Создаем или обновляем запись прогресса
    progress = db.query(StudentProgress).filter(
        StudentProgress.user_id == current_user.id,
        StudentProgress.lesson_id == lesson_id
    ).first()
    
    if not progress:
        progress = StudentProgress(
            user_id=current_user.id,
            course_id=module.course_id,
            lesson_id=lesson_id,
            status="in_progress",
            completion_percentage=0,
            last_accessed=datetime.now(timezone.utc)
        )
        db.add(progress)
    else:
        if progress.status == "not_started":
            progress.status = "in_progress"
        progress.last_accessed = datetime.now(timezone.utc)
    
    # Обновляем daily streak при начале урока
    update_daily_streak(current_user, db)
    
    db.commit()
    
    return {"detail": "Lesson started"}

@router.get("/students", response_model=List[Dict[str, Any]])
def get_students_progress(
    course_id: Optional[int] = None,
    group_id: Optional[int] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, le=1000),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить прогресс всех студентов (для учителей/кураторов/админов)"""
    
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Формируем запрос студентов в зависимости от роли
    students_query = db.query(UserInDB).filter(UserInDB.role == "student", UserInDB.is_active == True)
    
    if current_user.role == "teacher":
        # Учителя видят только учеников своих курсов
        if course_id:
            # Проверяем, что курс принадлежит учителю
            course = db.query(Course).filter(
                Course.id == course_id,
                Course.teacher_id == current_user.id
            ).first()
            if not course:
                raise HTTPException(status_code=403, detail="Access denied to this course")
            
            enrolled_student_ids = db.query(Enrollment.user_id).filter(
                Enrollment.course_id == course_id,
                Enrollment.is_active == True
            ).subquery()
            students_query = students_query.filter(UserInDB.id.in_(enrolled_student_ids))
        else:
            # Все ученики всех курсов учителя
            teacher_course_ids = db.query(Course.id).filter(Course.teacher_id == current_user.id).subquery()
            enrolled_student_ids = db.query(Enrollment.user_id).filter(
                Enrollment.course_id.in_(teacher_course_ids),
                Enrollment.is_active == True
            ).subquery()
            students_query = students_query.filter(UserInDB.id.in_(enrolled_student_ids))
    
    elif current_user.role == "curator":
        # Кураторы видят учеников из своих групп
        from src.schemas.models import Group
        
        # Get groups where current user is curator
        curator_groups = db.query(Group).filter(Group.curator_id == current_user.id).all()
        
        if curator_groups:
            group_ids = [g.id for g in curator_groups]
            # Get students in curator's groups using GroupStudent association table
            group_student_ids = db.query(GroupStudent.student_id).filter(
                GroupStudent.group_id.in_(group_ids)
            ).subquery()
            students_query = students_query.filter(UserInDB.id.in_(group_student_ids))
        else:
            students_query = students_query.filter(UserInDB.id == -1)  # Пустой результат
    
    # Дополнительные фильтры
    if group_id and current_user.role == "admin":
        # Filter students by group using GroupStudent association table
        group_student_ids = db.query(GroupStudent.student_id).filter(
            GroupStudent.group_id == group_id
        ).subquery()
        students_query = students_query.filter(UserInDB.id.in_(group_student_ids))
    
    students = students_query.offset(skip).limit(limit).all()
    
    # Собираем статистику по каждому студенту
    students_progress = []
    
    for student in students:
        # Получаем все записи прогресса студента
        progress_records = db.query(StudentProgress).filter(
            StudentProgress.user_id == student.id
        ).all()
        
        if course_id:
            progress_records = [p for p in progress_records if p.course_id == course_id]
        
        # Подсчитываем статистику
        total_lessons = len([p for p in progress_records if p.lesson_id])
        completed_lessons = len([p for p in progress_records if p.status == "completed" and p.lesson_id])
        total_time = sum(p.time_spent_minutes for p in progress_records)
        
        # Средний прогресс по курсам
        if progress_records:
            avg_progress = sum(p.completion_percentage for p in progress_records) / len(progress_records)
        else:
            avg_progress = 0
        
        # Последняя активность
        last_activity = None
        if progress_records:
            last_activity = max(p.last_accessed for p in progress_records if p.last_accessed)
        
        # Получаем количество выполненных заданий
        assignment_count = db.query(AssignmentSubmission).filter(
            AssignmentSubmission.user_id == student.id
        ).count()
        
        # Получаем group_id студента через GroupStudent association table
        group_student = db.query(GroupStudent).filter(GroupStudent.student_id == student.id).first()
        student_group_id = group_student.group_id if group_student else None
        
        students_progress.append({
            "student_id": student.id,
            "student_name": student.name,
            "student_identifier": student.student_id,
            "email": student.email,
            "group_id": student_group_id,
            "total_lessons": total_lessons,
            "completed_lessons": completed_lessons,
            "completion_rate": (completed_lessons / total_lessons * 100) if total_lessons > 0 else 0,
            "average_progress": round(avg_progress, 1),
            "total_study_time_minutes": total_time,
            "assignment_submissions": assignment_count,
            "last_activity": last_activity
        })
    
    return students_progress

@router.get("/analytics")
@cached(namespace="progress:analytics", ttl=45, key_args=("course_id", "time_range"))
def get_progress_analytics(
    course_id: Optional[int] = None,
    time_range: int = Query(30, description="Days to analyze"),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить аналитику прогресса (для учителей/админов)"""
    
    if current_user.role not in ["teacher", "admin"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Определяем временной диапазон
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=time_range)
    
    # Базовый запрос прогресса
    progress_query = db.query(StudentProgress).filter(
        StudentProgress.last_accessed >= start_date
    )
    
    # Фильтр по курсу
    if course_id:
        if current_user.role == "teacher":
            # Проверяем права на курс
            course = db.query(Course).filter(
                Course.id == course_id,
                Course.teacher_id == current_user.id
            ).first()
            if not course:
                raise HTTPException(status_code=403, detail="Access denied to this course")
        
        progress_query = progress_query.filter(StudentProgress.course_id == course_id)
    elif current_user.role == "teacher":
        # Ограничиваем курсами учителя
        teacher_course_ids = db.query(Course.id).filter(Course.teacher_id == current_user.id).subquery()
        progress_query = progress_query.filter(StudentProgress.course_id.in_(teacher_course_ids))
    
    progress_records = progress_query.all()
    
    # Аналитика
    analytics = {
        "time_range_days": time_range,
        "total_students": len(set(p.user_id for p in progress_records)),
        "total_lessons_accessed": len([p for p in progress_records if p.lesson_id]),
        "total_assignments_completed": len([p for p in progress_records if p.assignment_id and p.status == "completed"]),
        "total_study_time_hours": sum(p.time_spent_minutes for p in progress_records) // 60,
        "average_completion_rate": 0,
        "daily_activity": {},
        "progress_distribution": {
            "not_started": 0,
            "in_progress": 0,
            "completed": 0
        },
        "top_performing_students": [],
        "struggling_students": []
    }
    
    # Распределение статусов
    for status in ["not_started", "in_progress", "completed"]:
        analytics["progress_distribution"][status] = len([
            p for p in progress_records if p.status == status
        ])
    
    # Средний процент завершения
    if progress_records:
        analytics["average_completion_rate"] = sum(
            p.completion_percentage for p in progress_records
        ) / len(progress_records)
    
    # Активность по дням
    daily_activity = {}
    for i in range(time_range):
        day = (start_date + timedelta(days=i)).date()
        daily_activity[day.isoformat()] = 0
    
    for record in progress_records:
        if record.last_accessed:
            day = record.last_accessed.date()
            if day.isoformat() in daily_activity:
                daily_activity[day.isoformat()] += 1
    
    analytics["daily_activity"] = daily_activity
    
    # Топ студенты и отстающие (упрощенная версия)
    student_stats = {}
    for record in progress_records:
        if record.user_id not in student_stats:
            student_stats[record.user_id] = {
                "completion_sum": 0,
                "record_count": 0,
                "time_spent": 0
            }
        
        student_stats[record.user_id]["completion_sum"] += record.completion_percentage
        student_stats[record.user_id]["record_count"] += 1
        student_stats[record.user_id]["time_spent"] += record.time_spent_minutes
    
    # Вычисляем средний прогресс для каждого студента
    student_averages = []
    for user_id, stats in student_stats.items():
        avg_completion = stats["completion_sum"] / stats["record_count"] if stats["record_count"] > 0 else 0
        student = db.query(UserInDB).filter(UserInDB.id == user_id).first()
        
        if student:
            student_averages.append({
                "student_id": user_id,
                "student_name": student.name,
                "average_progress": round(avg_completion, 1),
                "total_time_hours": stats["time_spent"] // 60
            })
    
    # Сортируем для топа и отстающих
    student_averages.sort(key=lambda x: x["average_progress"], reverse=True)
    
    analytics["top_performing_students"] = student_averages[:5]
    analytics["struggling_students"] = student_averages[-5:]
    
    return analytics

@router.get("/student/overview")
@cached(namespace="progress:overview", ttl=45)
def get_student_progress_overview(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить общий обзор прогресса текущего студента по всем курсам"""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can access this endpoint")
    
    courses = get_user_courses(current_user.id, db)

    # ---- Batch every per-course/module/lesson/step query up front. This endpoint previously
    # walked course -> module -> lesson and issued a Step query AND a StepProgress query per
    # lesson (plus a teacher query per course) — hundreds of queries per dashboard load. ----
    _course_ids = [c.id for c in courses]
    modules_by_course: Dict[int, List[Module]] = {}
    lessons_by_module: Dict[int, List[Lesson]] = {}
    steps_count_by_lesson: Dict[int, int] = {}
    progress_by_lesson: Dict[int, List[StepProgress]] = {}
    teacher_names: Dict[int, str] = {}
    if _course_ids:
        _modules = (
            db.query(Module).filter(Module.course_id.in_(_course_ids)).order_by(Module.order_index).all()
        )
        for _m in _modules:
            modules_by_course.setdefault(_m.course_id, []).append(_m)
        _module_ids = [_m.id for _m in _modules]
        if _module_ids:
            _lessons = (
                db.query(Lesson).filter(Lesson.module_id.in_(_module_ids)).order_by(Lesson.order_index).all()
            )
            for _l in _lessons:
                lessons_by_module.setdefault(_l.module_id, []).append(_l)
            _lesson_ids = [_l.id for _l in _lessons]
            if _lesson_ids:
                for _lid, _cnt in (
                    db.query(Step.lesson_id, func.count(Step.id))
                    .filter(Step.lesson_id.in_(_lesson_ids)).group_by(Step.lesson_id).all()
                ):
                    steps_count_by_lesson[_lid] = _cnt
                for _sp in (
                    db.query(StepProgress).filter(
                        StepProgress.user_id == current_user.id,
                        StepProgress.lesson_id.in_(_lesson_ids),
                    ).all()
                ):
                    progress_by_lesson.setdefault(_sp.lesson_id, []).append(_sp)
    _teacher_ids = {c.teacher_id for c in courses if c.teacher_id}
    if _teacher_ids:
        for _t in db.query(UserInDB.id, UserInDB.name).filter(UserInDB.id.in_(_teacher_ids)).all():
            teacher_names[_t.id] = _t.name

    # Calculate overall statistics
    total_courses = len(courses)
    total_lessons = 0
    total_steps = 0
    completed_lessons = 0
    completed_steps = 0
    total_time_spent = 0
    
    course_progress = []
    
    for course in courses:
        modules = modules_by_course.get(course.id, [])
        
        course_lessons = 0
        course_steps = 0
        course_completed_lessons = 0
        course_completed_steps = 0
        course_time_spent = 0
        
        for module in modules:
            lessons = lessons_by_module.get(module.id, [])
            
            for lesson in lessons:
                course_lessons += 1
                total_lessons += 1
                
                # Step count for this lesson (from a single batched grouped query).
                steps_count = steps_count_by_lesson.get(lesson.id, 0)
                course_steps += steps_count
                total_steps += steps_count
                
                # This student's step-progress rows for this lesson (from one batched query).
                step_progress = progress_by_lesson.get(lesson.id, [])
                
                lesson_completed_steps = len([sp for sp in step_progress if sp.status == "completed"])
                course_completed_steps += lesson_completed_steps
                completed_steps += lesson_completed_steps
                
                # Calculate lesson completion (if all steps are completed, lesson is completed)
                if steps_count > 0 and lesson_completed_steps == steps_count:
                    course_completed_lessons += 1
                    completed_lessons += 1
                
                # Add time spent
                lesson_time = sum(sp.time_spent_minutes for sp in step_progress)
                course_time_spent += lesson_time
                total_time_spent += lesson_time
        
        # Calculate course completion percentage
        course_completion_percentage = 0
        if course_steps > 0:
            course_completion_percentage = (course_completed_steps / course_steps) * 100
        

        # Teacher name comes from the batched teacher_names map (no per-course query).
        
        course_progress.append({
            "course_id": course.id,
            "course_title": course.title,
            "teacher_id": course.teacher_id,
            "teacher_name": teacher_names.get(course.teacher_id, "Unknown"),
            "cover_image_url": course.cover_image_url,
            "total_lessons": course_lessons,
            "total_steps": course_steps,
            "completed_lessons": course_completed_lessons,
            "completed_steps": course_completed_steps,
            "completion_percentage": round(course_completion_percentage, 1),
            "time_spent_minutes": course_time_spent,
            "last_accessed": None  # TODO: Add last accessed tracking
        })
    
    # Calculate overall completion percentage
    overall_completion_percentage = 0
    if total_steps > 0:
        overall_completion_percentage = (completed_steps / total_steps) * 100
    
    return {
        "student_id": current_user.id,
        "student_name": current_user.name,
        "total_courses": total_courses,
        "total_lessons": total_lessons,
        "total_steps": total_steps,
        "completed_lessons": completed_lessons,
        "completed_steps": completed_steps,
        "overall_completion_percentage": round(overall_completion_percentage, 1),
        "total_time_spent_minutes": total_time_spent,
        "daily_streak": current_user.daily_streak or 0,
        "last_activity_date": current_user.last_activity_date,
        "courses": course_progress,
        "group_teachers": get_student_group_teachers(current_user.id, db)
    }

def get_student_group_teachers(student_id: int, db: Session) -> List[Dict[str, Any]]:
    """Helper to get teachers for all groups a student belongs to"""
    from src.schemas.models import Group, GroupStudent
    
    # Get all groups the student belongs to
    groups = db.query(Group).join(
        GroupStudent, Group.id == GroupStudent.group_id
    ).filter(
        GroupStudent.student_id == student_id,
        Group.is_active == True
    ).all()
    
    # De-dupe teacher ids preserving first-seen order, then resolve names in ONE query
    # (was one query per group).
    ordered_ids = list(dict.fromkeys(g.teacher_id for g in groups if g.teacher_id))
    if not ordered_ids:
        return []

    names = {
        t.id: t.name
        for t in db.query(UserInDB.id, UserInDB.name).filter(UserInDB.id.in_(ordered_ids)).all()
    }
    return [{"id": tid, "name": names[tid]} for tid in ordered_ids if tid in names]

def build_student_progress_overview(db: Session, student_id: int) -> dict:
    """Compute a student's cross-course progress overview. Shared by the
    teacher/admin endpoint and the parent portal endpoint — each applies its own
    access gate before calling this. Raises 404 if the student doesn't exist."""
    student = db.query(UserInDB).filter(UserInDB.id == student_id, UserInDB.role == "student").first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    courses = get_user_courses(student_id, db)

    # ---- Batch every per-course/module/lesson/step query up front (was a nested course -> module
    # -> lesson N+1 issuing a Step + StepProgress query per lesson and a teacher query per course). ----
    _course_ids = [c.id for c in courses]
    modules_by_course: Dict[int, List[Module]] = {}
    lessons_by_module: Dict[int, List[Lesson]] = {}
    steps_count_by_lesson: Dict[int, int] = {}
    progress_by_lesson: Dict[int, List[StepProgress]] = {}
    teacher_names: Dict[int, str] = {}
    if _course_ids:
        _modules = (
            db.query(Module).filter(Module.course_id.in_(_course_ids)).order_by(Module.order_index).all()
        )
        for _m in _modules:
            modules_by_course.setdefault(_m.course_id, []).append(_m)
        _module_ids = [_m.id for _m in _modules]
        if _module_ids:
            _lessons = (
                db.query(Lesson).filter(Lesson.module_id.in_(_module_ids)).order_by(Lesson.order_index).all()
            )
            for _l in _lessons:
                lessons_by_module.setdefault(_l.module_id, []).append(_l)
            _lesson_ids = [_l.id for _l in _lessons]
            if _lesson_ids:
                for _lid, _cnt in (
                    db.query(Step.lesson_id, func.count(Step.id))
                    .filter(Step.lesson_id.in_(_lesson_ids)).group_by(Step.lesson_id).all()
                ):
                    steps_count_by_lesson[_lid] = _cnt
                for _sp in (
                    db.query(StepProgress).filter(
                        StepProgress.user_id == student_id,
                        StepProgress.lesson_id.in_(_lesson_ids),
                    ).all()
                ):
                    progress_by_lesson.setdefault(_sp.lesson_id, []).append(_sp)
    _teacher_ids = {c.teacher_id for c in courses if c.teacher_id}
    if _teacher_ids:
        for _t in db.query(UserInDB.id, UserInDB.name).filter(UserInDB.id.in_(_teacher_ids)).all():
            teacher_names[_t.id] = _t.name

    # Calculate overall statistics
    total_courses = len(courses)
    total_lessons = 0
    total_steps = 0
    completed_lessons = 0
    completed_steps = 0
    total_time_spent = 0
    
    course_progress = []
    
    for course in courses:
        modules = modules_by_course.get(course.id, [])
        
        course_lessons = 0
        course_steps = 0
        course_completed_lessons = 0
        course_completed_steps = 0
        course_time_spent = 0
        
        for module in modules:
            lessons = lessons_by_module.get(module.id, [])
            
            for lesson in lessons:
                course_lessons += 1
                total_lessons += 1
                
                # Step count for this lesson (from a single batched grouped query).
                steps_count = steps_count_by_lesson.get(lesson.id, 0)
                course_steps += steps_count
                total_steps += steps_count
                
                # This student's step-progress rows for this lesson (from one batched query).
                step_progress = progress_by_lesson.get(lesson.id, [])
                
                lesson_completed_steps = len([sp for sp in step_progress if sp.status == "completed"])
                course_completed_steps += lesson_completed_steps
                completed_steps += lesson_completed_steps
                
                # Calculate lesson completion (if all steps are completed, lesson is completed)
                if steps_count > 0 and lesson_completed_steps == steps_count:
                    course_completed_lessons += 1
                    completed_lessons += 1
                
                # Add time spent
                lesson_time = sum(sp.time_spent_minutes for sp in step_progress)
                course_time_spent += lesson_time
                total_time_spent += lesson_time
        
        # Calculate course completion percentage
        course_completion_percentage = 0
        if course_steps > 0:
            course_completion_percentage = (course_completed_steps / course_steps) * 100
        
        # Teacher name comes from the batched teacher_names map (no per-course query).
        
        course_progress.append({
            "course_id": course.id,
            "course_title": course.title,
            "teacher_name": teacher_names.get(course.teacher_id, "Unknown"),
            "cover_image_url": course.cover_image_url,
            "total_lessons": course_lessons,
            "total_steps": course_steps,
            "completed_lessons": course_completed_lessons,
            "completed_steps": course_completed_steps,
            "completion_percentage": round(course_completion_percentage, 1),
            "time_spent_minutes": course_time_spent,
            "last_accessed": None  # TODO: Add last accessed tracking
        })
    
    # Calculate overall completion percentage
    overall_completion_percentage = 0
    if total_steps > 0:
        overall_completion_percentage = (completed_steps / total_steps) * 100
    
    return {
        "student_id": student_id,
        "student_name": student.name,
        "total_courses": total_courses,
        "total_lessons": total_lessons,
        "total_steps": total_steps,
        "completed_lessons": completed_lessons,
        "completed_steps": completed_steps,
        "overall_completion_percentage": round(overall_completion_percentage, 1),
        "total_time_spent_minutes": total_time_spent,
        "daily_streak": student.daily_streak or 0,
        "last_activity_date": student.last_activity_date,
        "courses": course_progress,
        "group_teachers": get_student_group_teachers(student_id, db)
    }


@router.get("/student/{student_id}/overview")
@cached(namespace="progress:overview-by-id", ttl=45, key_args=("student_id",))
def get_student_progress_overview_by_id(
    student_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить общий обзор прогресса конкретного студента (для учителей/админов)"""
    if current_user.role not in ["teacher", "admin"]:
        raise HTTPException(status_code=403, detail="Only teachers and admins can access this endpoint")

    student = db.query(UserInDB).filter(UserInDB.id == student_id, UserInDB.role == "student").first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    # Teachers may only view students in their own groups.
    if current_user.role == "teacher":
        from src.schemas.models import Group, GroupStudent
        group_student = db.query(GroupStudent).filter(
            GroupStudent.student_id == student_id,
            GroupStudent.group_id.in_(
                db.query(Group.id).filter(Group.teacher_id == current_user.id)
            )
        ).first()
        if not group_student:
            raise HTTPException(status_code=403, detail="Access denied to this student")

    return build_student_progress_overview(db, student_id)

# =============================================================================
# STEP PROGRESS TRACKING
# =============================================================================

@router.post("/step/{step_id}/start", response_model=StepProgressSchema)
def mark_step_started(
    step_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Отметить начало изучения шага"""
    # Получаем информацию о шаге
    # Получаем информацию о шаге, уроке и модуле одним запросом для оптимизации
    step = db.query(Step).options(
        joinedload(Step.lesson).joinedload(Lesson.module)
    ).filter(Step.id == step_id).first()
    
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")
    
    lesson = step.lesson
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
        
    module = lesson.module
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    # Проверяем существующий прогресс
    existing_progress = db.query(StepProgress).filter(
        StepProgress.user_id == current_user.id,
        StepProgress.step_id == step_id
    ).first()
    
    if existing_progress:
        # Если шаг уже начат, просто обновляем время посещения
        if existing_progress.started_at is None:
            existing_progress.started_at = datetime.now(timezone.utc)
            existing_progress.status = "in_progress"
        existing_progress.visited_at = datetime.now(timezone.utc)
        
        # Обновляем daily streak при посещении шага
        update_daily_streak(current_user, db)
        
        db.commit()
        db.refresh(existing_progress)
        return StepProgressSchema.from_orm(existing_progress)
    
    # Создаем новую запись прогресса
    step_progress = StepProgress(
        user_id=current_user.id,
        course_id=module.course_id,
        lesson_id=lesson.id,
        step_id=step_id,
        status="in_progress",
        started_at=datetime.now(timezone.utc),
        visited_at=datetime.now(timezone.utc),
        time_spent_minutes=0
    )
    
    db.add(step_progress)
    db.commit()
    db.refresh(step_progress)
    
    return StepProgressSchema.from_orm(step_progress)

@router.post("/step/{step_id}/visit", response_model=StepProgressSchema)
def mark_step_visited(
    step_id: int,
    step_data: StepProgressCreateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Отметить шаг как посещенный"""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can mark steps as visited")
    
    # Получаем информацию о шаге
    # Получаем информацию о шаге, уроке и модуле одним запросом
    step = db.query(Step).options(
        joinedload(Step.lesson).joinedload(Lesson.module)
    ).filter(Step.id == step_id).first()
    
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")
    
    lesson = step.lesson
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
        
    module = lesson.module
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    # Проверяем доступ к курсу
    if not check_course_access(module.course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this step")
    
    # Находим или создаем запись прогресса шага
    step_progress = db.query(StepProgress).filter(
        StepProgress.user_id == current_user.id,
        StepProgress.step_id == step_id
    ).first()
    
    if not step_progress:
        # Создаем новую запись прогресса (если шаг завершается без предварительного старта)
        step_progress = StepProgress(
            user_id=current_user.id,
            course_id=module.course_id,
            lesson_id=lesson.id,
            step_id=step_id,
            status="completed",
            started_at=datetime.now(timezone.utc),  # Устанавливаем время начала равным времени завершения
            visited_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            time_spent_minutes=step_data.time_spent_minutes
        )
        db.add(step_progress)
    else:
        # Обновляем существующую запись
        step_progress.status = "completed"
        step_progress.visited_at = datetime.now(timezone.utc)
        step_progress.completed_at = datetime.now(timezone.utc)
        
        # Если не было времени начала, устанавливаем его
        if step_progress.started_at is None:
            step_progress.started_at = datetime.now(timezone.utc)
        
        if step_progress.time_spent_minutes is None:
            step_progress.time_spent_minutes = 0
        step_progress.time_spent_minutes += step_data.time_spent_minutes
    
    # Обновляем общее время изучения пользователя
    if current_user.total_study_time_minutes is None:
        current_user.total_study_time_minutes = 0
    current_user.total_study_time_minutes += (step_data.time_spent_minutes or 0)
    
    # Обновляем daily streak при посещении шага
    update_daily_streak(current_user, db)
    
    # Обновляем общий прогресс студента по курсу
    # Обновляем общий прогресс в кэше (быстрее)
    update_student_course_summary(
        user_id=current_user.id,
        course_id=module.course_id,
        db=db,
        step_completed=True,
        time_spent_delta=step_data.time_spent_minutes,
        lesson_id=lesson.id,
        lesson_title=lesson.title
    )
    
    # Обновляем старый StudentProgress для совместимости (пока не мигрировали всё)
    update_student_progress(current_user.id, module.course_id, db)
    
    # Создаем снимок прогресса (если еще нет на сегодня)
    create_progress_snapshot(current_user.id, module.course_id, db)

    db.commit()
    db.refresh(step_progress)

    # SAT Checkpoints: lessons are normally finished step-by-step (no explicit lesson-complete call).
    from src.checkpoints import service as checkpoint_service
    try:
        checkpoint_service.sync_student_checkpoints(db, current_user.id, commit=True)
    except Exception:  # never fail an already-committed step visit because of checkpoint bookkeeping
        _progress_log.exception("checkpoint sync failed for user %s after step %s", current_user.id, step_id)
        db.rollback()

    return StepProgressSchema.from_orm(step_progress)

@router.get("/step/{step_id}", response_model=StepProgressSchema)
def get_step_progress(
    step_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить прогресс по конкретному шагу"""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can access step progress")
    
    # Получаем информацию о шаге
    step = db.query(Step).filter(Step.id == step_id).first()
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")
    
    # Получаем информацию об уроке и курсе
    lesson = db.query(Lesson).filter(Lesson.id == step.lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    # Проверяем доступ к курсу
    if not check_course_access(module.course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this step")
    
    # Получаем прогресс по шагу
    step_progress = db.query(StepProgress).filter(
        StepProgress.user_id == current_user.id,
        StepProgress.step_id == step_id
    ).first()
    
    if not step_progress:
        # Создаем запись с дефолтными значениями
        step_progress = StepProgress(
            user_id=current_user.id,
            course_id=module.course_id,
            lesson_id=lesson.id,
            step_id=step_id,
            status="not_started",
            time_spent_minutes=0
        )
        db.add(step_progress)
        db.commit()
        db.refresh(step_progress)
    
    return StepProgressSchema.from_orm(step_progress)

@router.get("/lesson/{lesson_id}/steps", response_model=List[StepProgressSchema])
def get_lesson_steps_progress(
    lesson_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить прогресс по всем шагам урока"""
    if current_user.role not in ["student", "teacher", "admin", "curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Получаем информацию об уроке с модулем одним запросом
    lesson = db.query(Lesson).options(
        joinedload(Lesson.module)
    ).filter(Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    module = lesson.module
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    # Проверяем доступ к курсу
    if not check_course_access(module.course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this lesson")
    
    # Получаем все шаги урока
    steps = db.query(Step).filter(Step.lesson_id == lesson_id).order_by(Step.order_index).all()
    
    # OPTIMIZATION: Batch fetch all step progress in a single query (fixes N+1)
    step_ids = [step.id for step in steps]
    existing_progress = db.query(StepProgress).filter(
        StepProgress.user_id == current_user.id,
        StepProgress.step_id.in_(step_ids)
    ).all() if step_ids else []
    
    # Create lookup dictionary for O(1) access
    progress_by_step_id = {p.step_id: p for p in existing_progress}
    
    # First pass: Create missing records
    new_records = []
    
    for step in steps:
        if step.id not in progress_by_step_id:
            # Создаем запись с дефолтными значениями
            new_progress = StepProgress(
                user_id=current_user.id,
                course_id=module.course_id,
                lesson_id=lesson.id,
                step_id=step.id,
                status="not_started",
                time_spent_minutes=0
            )
            new_records.append(new_progress)
            progress_by_step_id[step.id] = new_progress # Add to map so we can use it later

    # Batch save new records to generate IDs
    if new_records:
        db.add_all(new_records)
        db.flush() # This populates IDs without committing transaction
        for record in new_records:
            db.refresh(record) # Ensure all attributes like ID are available

    # Second pass: Build response using now-complete objects
    steps_progress = []
    for step in steps:
        step_progress = progress_by_step_id[step.id]
        steps_progress.append(StepProgressSchema.from_orm(step_progress))
    
    # Commit transaction
    if new_records:
        db.commit()
    
    return steps_progress

@router.get("/course/{course_id}/students/steps")
@cached(namespace="progress:course-students-steps", ttl=60, key_args=("course_id",))
def get_course_students_steps_progress(
    course_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить прогресс всех студентов по шагам курса (для учителей/кураторов/админов)"""
    
    if current_user.role not in ["teacher", "curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Проверяем существование курса
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Проверяем права доступа к курсу
    if current_user.role == "teacher" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied to this course")
    
    # Получаем всех студентов курса
    students_query = db.query(UserInDB).filter(UserInDB.role == "student", UserInDB.is_active == True)
    
    if current_user.role == "teacher":
        # Учителя видят только учеников своих курсов
        enrolled_student_ids = db.query(Enrollment.user_id).filter(
            Enrollment.course_id == course_id,
            Enrollment.is_active == True
        ).subquery()
        students_query = students_query.filter(UserInDB.id.in_(enrolled_student_ids))
    
    elif current_user.role == "curator":
        # Кураторы видят учеников из своей группы
        if current_user.group_id:
            group_student_ids = db.query(GroupStudent.student_id).filter(
                GroupStudent.group_id == current_user.group_id
            ).subquery()
            students_query = students_query.filter(UserInDB.id.in_(group_student_ids))
        else:
            students_query = students_query.filter(UserInDB.id == -1)  # Пустой результат
    
    students = students_query.all()
    
    # Получаем все модули и уроки курса
    modules = db.query(Module).filter(Module.course_id == course_id).order_by(Module.order_index).all()
    
    course_progress = {
        "course_id": course_id,
        "course_title": course.title,
        "total_students": len(students),
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
            steps = db.query(Step).filter(Step.lesson_id == lesson.id).order_by(Step.order_index).all()
            
            lesson_data = {
                "lesson_id": lesson.id,
                "lesson_title": lesson.title,
                "total_steps": len(steps),
                "students_progress": []
            }
            
            for student in students:
                # Получаем прогресс студента по всем шагам урока
                completed_steps = db.query(StepProgress).filter(
                    StepProgress.user_id == student.id,
                    StepProgress.lesson_id == lesson.id,
                    StepProgress.status == "completed"
                ).count()
                
                total_time = db.query(func.sum(StepProgress.time_spent_minutes)).filter(
                    StepProgress.user_id == student.id,
                    StepProgress.lesson_id == lesson.id
                ).scalar() or 0
                
                lesson_data["students_progress"].append({
                    "student_id": student.id,
                    "student_name": student.name,
                    "completed_steps": completed_steps,
                    "total_steps": len(steps),
                    "completion_percentage": (completed_steps / len(steps) * 100) if steps else 0,
                    "time_spent_minutes": total_time
                })
            
            module_data["lessons"].append(lesson_data)
        
        course_progress["modules"].append(module_data)
    
    return course_progress


def calculate_streak_multiplier(streak: int) -> float:
    """
    Calculate point multiplier based on daily streak.
    - < 5 days: 1.0x
    - 5 days: 1.1x
    - Every 2 additional days: +0.1x
    """
    if streak < 5:
        return 1.0
    
    # Base 1.1 at 5 days
    # streak 5-6 -> 0 steps -> 1.1
    # streak 7-8 -> 1 step -> 1.2
    steps = (streak - 5) // 2
    multiplier = 1.1 + (steps * 0.1)
    
    return round(multiplier, 1)


@router.get("/my-streak")
def get_my_daily_streak(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить информацию о daily streak текущего студента"""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can access streak information")
    
    today = date.today()
    yesterday = today - timedelta(days=1)
    
    # Определяем статус streak и актуальное значение счетчика
    streak_count = current_user.daily_streak or 0
    streak_status = "active"
    is_active_today = current_user.last_activity_date == today
    
    if current_user.last_activity_date is None:
        streak_status = "not_started"
        streak_count = 0
    elif current_user.last_activity_date < yesterday:
        # Streak is broken - reset counter to 0
        streak_status = "broken"
        streak_count = 0
    elif current_user.last_activity_date == yesterday:
        # Нужна активность сегодня чтобы сохранить streak
        streak_status = "at_risk"
    elif current_user.last_activity_date == today:
        # Active today
        streak_status = "active"
    
    # Calculate multiplier
    current_multiplier = calculate_streak_multiplier(streak_count)
    
    # Calculate next multiplier milestone
    # If < 5, next is at 5
    # If >= 5, next is at next even number relative to 5 (5->7, 6->7, 7->9)
    next_milestone = 5
    if streak_count >= 5:
        # (streak - 5) // 2 gives current steps
        # next step is current step + 1
        # days needed = 5 + (step + 1) * 2
        current_steps = (streak_count - 5) // 2
        next_milestone = 5 + (current_steps + 1) * 2
        
    return {
        "student_id": current_user.id,
        "student_name": current_user.name,
        "daily_streak": streak_count,
        "last_activity_date": current_user.last_activity_date,
        "streak_status": streak_status,
        "is_active_today": is_active_today,
        "total_study_time_minutes": current_user.total_study_time_minutes,
        "current_multiplier": current_multiplier,
        "next_multiplier_at": next_milestone
    }

# =============================================================================
# PROGRESS INITIALIZATION
# =============================================================================

@router.post("/initialize-progress")
def initialize_progress(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Инициализировать прогресс для всех студентов на основе существующих данных
    Только для администраторов
    """
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can initialize progress")
    
    try:
        # Получаем всех студентов
        students = db.query(UserInDB).filter(UserInDB.role == "student").all()
        
        # Получаем все курсы
        courses = db.query(Course).all()
        
        initialized_count = 0
        snapshots_created = 0
        
        for student in students:
            for course in courses:
                # Проверяем, записан ли студент на курс
                enrollment = db.query(Enrollment).filter(
                    Enrollment.user_id == student.id,
                    Enrollment.course_id == course.id
                ).first()
                
                if enrollment:
                    # Обновляем прогресс студента по курсу
                    update_student_progress(student.id, course.id, db)
                    initialized_count += 1
                    
                    # Создаем снимок прогресса
                    snapshot = create_progress_snapshot(student.id, course.id, db)
                    if snapshot:
                        snapshots_created += 1
        
        return {
            "message": "Progress initialization completed",
            "students_processed": len(students),
            "courses_processed": len(courses),
            "progress_records_updated": initialized_count,
            "snapshots_created": snapshots_created
        }
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to initialize progress: {str(e)}")

@router.post("/recalculate-progress/{course_id}")
def recalculate_course_progress(
    course_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Пересчитать прогресс всех студентов для конкретного курса
    Для администраторов и преподавателей курса
    """
    if current_user.role not in ["admin", "teacher"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Проверяем доступ к курсу
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")
    
    try:
        # Получаем всех студентов, записанных на курс
        enrollments = db.query(Enrollment).filter(Enrollment.course_id == course_id).all()
        
        updated_count = 0
        snapshots_created = 0
        
        for enrollment in enrollments:
            # Обновляем прогресс студента
            update_student_progress(enrollment.user_id, course_id, db)
            updated_count += 1
            
            # Создаем снимок прогресса
            snapshot = create_progress_snapshot(enrollment.user_id, course_id, db)
            if snapshot:
                snapshots_created += 1
        
        return {
            "message": f"Progress recalculated for course {course_id}",
            "students_updated": updated_count,
            "snapshots_created": snapshots_created
        }
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to recalculate progress: {str(e)}")


# =============================================================================
# QUIZ ATTEMPTS
# =============================================================================

def _get_manual_question_ids(step: Step | None) -> set[str]:
    if not step or not step.content_text:
        return set()
    try:
        parsed = json.loads(step.content_text)
    except Exception:
        return set()
    questions = parsed.get("questions") if isinstance(parsed, dict) else None
    if not isinstance(questions, list):
        return set()
    return {
        str(q.get("id"))
        for q in questions
        if isinstance(q, dict) and q.get("question_type") == "long_text" and q.get("id") is not None
    }


def _answered_question_ids(raw_answers: str | None) -> set[str]:
    if not raw_answers:
        return set()
    try:
        parsed = json.loads(raw_answers)
    except Exception:
        return set()
    if isinstance(parsed, list):
        result: set[str] = set()
        for item in parsed:
            if not (isinstance(item, list) and len(item) >= 2):
                continue
            qid = str(item[0])
            value = item[1]
            if value is None:
                continue
            if isinstance(value, str) and value.strip() == "":
                continue
            if isinstance(value, list) and len(value) == 0:
                continue
            result.add(qid)
        return result
    if isinstance(parsed, dict):
        return {str(k) for k in parsed.keys()}
    return set()


def _forbid_special_group_manual_quiz(step_id: int, raw_answers: str | None, current_user: UserInDB, db: Session) -> None:
    # Keep permissive: special-group students can save long_text answers without blocking submission.
    return

@router.post("/quiz-attempt", response_model=QuizAttemptSchema)
def create_quiz_attempt(
    attempt_data: QuizAttemptCreateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Сохранить попытку прохождения квиза или обновить черновик"""
    from src.checkpoints import service as checkpoint_service
    checkpoint_definition = checkpoint_service.checkpoint_definition_for_step(db, attempt_data.step_id)
    if checkpoint_definition is not None:
        if current_user.role != "student":
            raise HTTPException(status_code=403, detail="Only students take checkpoints")
        checkpoint_service.assert_can_submit(db, current_user.id, checkpoint_definition)

    try:
        _forbid_special_group_manual_quiz(attempt_data.step_id, attempt_data.answers, current_user, db)

        # Draft expiration cutoff (7 days)
        stale_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        
        # Delete stale drafts for this step (cleanup)
        db.query(QuizAttempt).filter(
            QuizAttempt.user_id == current_user.id,
            QuizAttempt.step_id == attempt_data.step_id,
            QuizAttempt.is_draft == True,
            QuizAttempt.created_at < stale_cutoff
        ).delete(synchronize_session=False)
        
        # Check if there's an existing valid (non-stale) draft for this step
        existing_draft = db.query(QuizAttempt).filter(
            QuizAttempt.user_id == current_user.id,
            QuizAttempt.step_id == attempt_data.step_id,
            QuizAttempt.is_draft == True,
            QuizAttempt.created_at >= stale_cutoff
        ).first()
        
        if existing_draft:
            # Check if quiz content hash matches (quiz hasn't changed)
            if attempt_data.quiz_content_hash and existing_draft.quiz_content_hash:
                if attempt_data.quiz_content_hash != existing_draft.quiz_content_hash:
                    # Quiz changed, invalidate old draft
                    db.delete(existing_draft)
                    db.flush()
                    existing_draft = None
        
        if existing_draft:
            # Update existing draft
            existing_draft.answers = attempt_data.answers
            existing_draft.current_question_index = attempt_data.current_question_index
            existing_draft.time_spent_seconds = attempt_data.time_spent_seconds
            existing_draft.updated_at = datetime.now(timezone.utc)
            
            if not attempt_data.is_draft:
                # Finalizing the quiz
                existing_draft.is_draft = False
                existing_draft.correct_answers = attempt_data.correct_answers
                existing_draft.score_percentage = attempt_data.score_percentage
                existing_draft.is_graded = attempt_data.is_graded
                existing_draft.completed_at = datetime.now(timezone.utc)

            db.commit()
            db.refresh(existing_draft)
            if not attempt_data.is_draft:
                if checkpoint_definition is not None:
                    try:
                        checkpoint_service.record_submission(db, current_user.id, existing_draft)
                    except Exception:
                        _progress_log.exception("checkpoint record_submission failed for attempt %s", existing_draft.id)
                        db.rollback()
                _maybe_award_course_quiz_points(
                    db,
                    user_id=existing_draft.user_id,
                    step_id=existing_draft.step_id,
                    course_id=existing_draft.course_id,
                    score_percentage=existing_draft.score_percentage,
                    is_graded=bool(existing_draft.is_graded),
                    attempt_id=existing_draft.id,
                )
            return existing_draft
        
        # Create new quiz attempt record
        quiz_attempt = QuizAttempt(
            user_id=current_user.id,
            step_id=attempt_data.step_id,
            course_id=attempt_data.course_id,
            lesson_id=attempt_data.lesson_id,
            quiz_title=attempt_data.quiz_title,
            total_questions=attempt_data.total_questions,
            correct_answers=attempt_data.correct_answers,
            score_percentage=attempt_data.score_percentage,
            answers=attempt_data.answers,
            time_spent_seconds=attempt_data.time_spent_seconds,
            is_graded=attempt_data.is_graded,
            is_draft=attempt_data.is_draft,
            current_question_index=attempt_data.current_question_index,
            quiz_content_hash=attempt_data.quiz_content_hash,
            completed_at=None if attempt_data.is_draft else datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc)
        )
        
        db.add(quiz_attempt)
        db.commit()
        db.refresh(quiz_attempt)

        if not attempt_data.is_draft:
            if checkpoint_definition is not None:
                try:
                    checkpoint_service.record_submission(db, current_user.id, quiz_attempt)
                except Exception:
                    _progress_log.exception("checkpoint record_submission failed for attempt %s", quiz_attempt.id)
                    db.rollback()
            _maybe_award_course_quiz_points(
                db,
                user_id=quiz_attempt.user_id,
                step_id=quiz_attempt.step_id,
                course_id=quiz_attempt.course_id,
                score_percentage=quiz_attempt.score_percentage,
                is_graded=bool(quiz_attempt.is_graded),
                attempt_id=quiz_attempt.id,
            )

        return quiz_attempt
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save quiz attempt: {str(e)}")


@router.patch("/quiz-attempts/{attempt_id}", response_model=QuizAttemptSchema)
def update_quiz_attempt(
    attempt_id: int,
    update_data: QuizAttemptUpdateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Update a quiz draft (auto-save progress)"""
    attempt = db.query(QuizAttempt).filter(
        QuizAttempt.id == attempt_id,
        QuizAttempt.user_id == current_user.id
    ).first()
    
    if not attempt:
        raise HTTPException(status_code=404, detail="Quiz attempt not found")

    # SAT Checkpoints: the web player autosaves a draft (POST) and finalizes through THIS route,
    # so the gate has to live here too (locked -> 403, already completed -> 409). The deadline
    # is soft: a late finalize is accepted and recorded as late.
    from src.checkpoints import service as checkpoint_service
    checkpoint_definition = checkpoint_service.checkpoint_definition_for_step(db, attempt.step_id)
    if checkpoint_definition is not None:
        if current_user.role != "student":
            raise HTTPException(status_code=403, detail="Only students take checkpoints")
        checkpoint_service.assert_can_submit(db, current_user.id, checkpoint_definition)

    _forbid_special_group_manual_quiz(
        attempt.step_id,
        update_data.answers if update_data.answers is not None else attempt.answers,
        current_user,
        db,
    )

    try:
        was_draft = attempt.is_draft
        if update_data.answers is not None:
            attempt.answers = update_data.answers
        if update_data.current_question_index is not None:
            attempt.current_question_index = update_data.current_question_index
        if update_data.time_spent_seconds is not None:
            attempt.time_spent_seconds = update_data.time_spent_seconds

        # Handle finalization
        if update_data.is_draft is not None and not update_data.is_draft:
            attempt.is_draft = False
            attempt.completed_at = datetime.now(timezone.utc)
            if update_data.total_questions is not None:
                attempt.total_questions = update_data.total_questions
            if update_data.correct_answers is not None:
                attempt.correct_answers = update_data.correct_answers
            if update_data.score_percentage is not None:
                attempt.score_percentage = update_data.score_percentage
            if update_data.is_graded is not None:
                attempt.is_graded = update_data.is_graded

        attempt.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(attempt)

        if was_draft and not attempt.is_draft:
            if checkpoint_definition is not None:
                try:
                    checkpoint_service.record_submission(db, current_user.id, attempt)
                except Exception:
                    _progress_log.exception("checkpoint record_submission failed for attempt %s", attempt.id)
                    db.rollback()
            _maybe_award_course_quiz_points(
                db,
                user_id=attempt.user_id,
                step_id=attempt.step_id,
                course_id=attempt.course_id,
                score_percentage=attempt.score_percentage,
                is_graded=bool(attempt.is_graded),
                attempt_id=attempt.id,
            )

        return attempt
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update quiz attempt: {str(e)}")


@router.put("/quiz-attempts/{attempt_id}/grade", response_model=QuizAttemptSchema)
def grade_quiz_attempt(
    attempt_id: int,
    grade_data: QuizAttemptGradeSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Grade a quiz attempt (for manual grading) — teachers, head curators, admins."""
    # Curators are deliberately excluded from grading.
    if current_user.role not in ["teacher", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
        
    attempt = db.query(QuizAttempt).filter(QuizAttempt.id == attempt_id).first()
    if not attempt:
        raise HTTPException(status_code=404, detail="Quiz attempt not found")
        
    # Check course access
    # Check course access
    if not check_course_access(attempt.course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")
            
    try:
        attempt.score_percentage = grade_data.score_percentage
        attempt.correct_answers = grade_data.correct_answers
        attempt.feedback = grade_data.feedback
        attempt.is_graded = True
        attempt.graded_by = current_user.id
        attempt.graded_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(attempt)
        _maybe_award_course_quiz_points(
            db,
            user_id=attempt.user_id,
            step_id=attempt.step_id,
            course_id=attempt.course_id,
            score_percentage=attempt.score_percentage,
            is_graded=bool(attempt.is_graded),
            attempt_id=attempt.id,
        )
        return attempt
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to grade quiz attempt: {str(e)}")


@router.delete("/quiz-attempts/{attempt_id}")
def delete_quiz_attempt(
    attempt_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Delete a quiz attempt (allow resubmission)"""
    if current_user.role not in ["teacher", "admin"]:
        raise HTTPException(status_code=403, detail="Access denied")
        
    attempt = db.query(QuizAttempt).filter(QuizAttempt.id == attempt_id).first()
    if not attempt:
        raise HTTPException(status_code=404, detail="Quiz attempt not found")
        
    # Check course access
    # Check course access
    if not check_course_access(attempt.course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")
            
    try:
        db.delete(attempt)
        db.commit()
        return {"detail": "Quiz attempt deleted successfully"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to delete quiz attempt: {str(e)}")


@router.get("/quiz-attempts/step/{step_id}", response_model=List[QuizAttemptSchema])
def get_step_quiz_attempts(
    step_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить все попытки прохождения квиза для конкретного степа текущего пользователя"""
    attempts = db.query(QuizAttempt).filter(
        QuizAttempt.user_id == current_user.id,
        QuizAttempt.step_id == step_id
    ).order_by(desc(QuizAttempt.completed_at)).all()
    
    return attempts


@router.get("/quiz-attempts/course/{course_id}", response_model=List[QuizAttemptSchema])
def get_course_quiz_attempts(
    course_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить все попытки прохождения квизов для курса текущего пользователя"""
    # Check if user has access to this course
    check_student_access(current_user, course_id, db)
    
    attempts = db.query(QuizAttempt).filter(
        QuizAttempt.user_id == current_user.id,
        QuizAttempt.course_id == course_id
    ).order_by(desc(QuizAttempt.completed_at)).all()
    
    return attempts


@router.get("/quiz-attempts/analytics/course/{course_id}")
def get_course_quiz_analytics(
    course_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить аналитику по квизам для курса (для учителей/админов)"""
    if current_user.role not in ["teacher", "admin", "curator", "head_curator"]:
        raise HTTPException(status_code=403, detail="Only teachers, curators and admins can access quiz analytics")
    
    # Get course to verify access
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Teachers can only see their own courses
    if current_user.role == "teacher" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only view analytics for your own courses")
    
    # Get all quiz attempts for this course
    attempts = db.query(QuizAttempt).filter(
        QuizAttempt.course_id == course_id
    ).all()
    
    # OPTIMIZATION: Batch fetch all users to avoid N+1 query
    user_ids = list(set(a.user_id for a in attempts))
    users_map = {u.id: u for u in db.query(UserInDB).filter(UserInDB.id.in_(user_ids)).all()} if user_ids else {}
    
    # Group by student
    student_attempts = {}
    for attempt in attempts:
        if attempt.user_id not in student_attempts:
            user = users_map.get(attempt.user_id)
            student_attempts[attempt.user_id] = {
                "user_id": attempt.user_id,
                "user_name": user.name if user else "Unknown",
                "attempts": []
            }
        
        student_attempts[attempt.user_id]["attempts"].append({
            "id": attempt.id,
            "step_id": attempt.step_id,
            "lesson_id": attempt.lesson_id,
            "quiz_title": attempt.quiz_title,
            "total_questions": attempt.total_questions,
            "correct_answers": attempt.correct_answers,
            "score_percentage": attempt.score_percentage,
            "time_spent_seconds": attempt.time_spent_seconds,
            "completed_at": attempt.completed_at.isoformat() if attempt.completed_at else None
        })

    
    # Calculate statistics
    total_attempts = len(attempts)
    if total_attempts > 0:
        avg_score = sum(a.score_percentage for a in attempts) / total_attempts
        avg_time = sum(a.time_spent_seconds or 0 for a in attempts) / total_attempts if any(a.time_spent_seconds for a in attempts) else 0
    else:
        avg_score = 0
        avg_time = 0
    
    return {
        "course_id": course_id,
        "course_title": course.title,
        "total_attempts": total_attempts,
        "unique_students": len(student_attempts),
        "average_score": round(avg_score, 2),
        "average_time_seconds": round(avg_time, 2),
        "student_attempts": list(student_attempts.values())
    }


@router.get("/quiz-attempts/analytics/student/{student_id}")
def get_student_quiz_analytics(
    student_id: int,
    course_id: Optional[int] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить аналитику по квизам для конкретного студента (для учителей/админов/родителей)"""
    if current_user.role not in ["teacher", "admin", "curator", "head_curator", "parent"]:
        raise HTTPException(status_code=403, detail="Only teachers, curators, admins and parents can access student analytics")
    # Parents may only see their linked children (this endpoint has no group scope otherwise).
    if current_user.role == "parent":
        from src.utils.permissions import check_student_access
        if not check_student_access(student_id, current_user, db):
            raise HTTPException(status_code=403, detail="Access denied to this student")

    # Build query
    query = db.query(QuizAttempt).filter(QuizAttempt.user_id == student_id)
    
    if course_id:
        query = query.filter(QuizAttempt.course_id == course_id)
    
    attempts = query.order_by(desc(QuizAttempt.completed_at)).all()
    
    # Get student info
    student = db.query(UserInDB).filter(UserInDB.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    # Group attempts by quiz (step_id)
    quiz_attempts = {}
    for attempt in attempts:
        if attempt.step_id not in quiz_attempts:
            quiz_attempts[attempt.step_id] = {
                "step_id": attempt.step_id,
                "lesson_id": attempt.lesson_id,
                "course_id": attempt.course_id,
                "quiz_title": attempt.quiz_title,
                "attempts": [],
                "best_score": 0,
                "latest_score": 0,
                "total_attempts": 0
            }
        
        quiz_attempts[attempt.step_id]["attempts"].append({
            "id": attempt.id,
            "score_percentage": attempt.score_percentage,
            "correct_answers": attempt.correct_answers,
            "total_questions": attempt.total_questions,
            "time_spent_seconds": attempt.time_spent_seconds,
            "completed_at": attempt.completed_at.isoformat() if attempt.completed_at else None
        })
        quiz_attempts[attempt.step_id]["total_attempts"] += 1
        quiz_attempts[attempt.step_id]["best_score"] = max(
            quiz_attempts[attempt.step_id]["best_score"], 
            attempt.score_percentage
        )
    
    # Set latest score for each quiz
    for step_id, quiz_data in quiz_attempts.items():
        if quiz_data["attempts"]:
            quiz_data["latest_score"] = quiz_data["attempts"][0]["score_percentage"]
    
    return {
        "student_id": student_id,
        "student_name": student.name,
        "total_attempts": len(attempts),
        "quizzes": list(quiz_attempts.values())
    }


@router.get("/lessons/{lesson_id}/quiz-summary")
def get_lesson_quiz_summary(
    lesson_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get quiz summary for a lesson showing all quizzes and latest attempt results"""
    # Verify lesson exists
    lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    # Get module and course for access check
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    # Check course access
    if not check_course_access(module.course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")
    
    # Get all quiz steps in this lesson
    quiz_steps = db.query(Step).filter(
        Step.lesson_id == lesson_id,
        Step.content_type == 'quiz'
    ).order_by(Step.order_index).all()
    
    # OPTIMIZATION: Batch fetch latest attempts for all quiz steps to avoid N+1
    step_ids = [s.id for s in quiz_steps]
    latest_attempts_map = {}
    
    if step_ids:
        # Get all attempts for these steps by this user, then pick latest per step
        all_attempts = db.query(QuizAttempt).filter(
            QuizAttempt.user_id == current_user.id,
            QuizAttempt.step_id.in_(step_ids),
            QuizAttempt.completed_at.isnot(None)  # Only completed attempts
        ).order_by(QuizAttempt.step_id, desc(QuizAttempt.completed_at)).all()
        
        # Build map of step_id -> latest attempt (first one per step since ordered by desc completed_at)
        for attempt in all_attempts:
            if attempt.step_id not in latest_attempts_map:
                latest_attempts_map[attempt.step_id] = attempt
    
    quizzes_summary = []
    total_questions = 0
    total_correct = 0
    
    for step in quiz_steps:
        # Get the latest attempt from our pre-fetched map
        latest_attempt = latest_attempts_map.get(step.id)
        
        # Parse quiz data to get title
        quiz_title = step.title
        if step.content_text:
            try:
                import json
                quiz_data = json.loads(step.content_text)
                if 'title' in quiz_data:
                    quiz_title = quiz_data['title']
            except:
                pass
        
        quiz_item = {
            "step_id": step.id,
            "quiz_title": quiz_title,
            "order_index": step.order_index,
            "last_attempt": None
        }
        
        if latest_attempt:
            quiz_item["last_attempt"] = {
                "score": latest_attempt.correct_answers,
                "total": latest_attempt.total_questions,
                "percentage": round(latest_attempt.score_percentage, 1),
                "completed_at": latest_attempt.completed_at.isoformat() if latest_attempt.completed_at else None
            }
            total_questions += latest_attempt.total_questions
            total_correct += latest_attempt.correct_answers
        
        quizzes_summary.append(quiz_item)

    
    # Calculate overall statistics
    average_percentage = 0
    if total_questions > 0:
        average_percentage = round((total_correct / total_questions) * 100, 1)
    
    return {
        "quizzes": quizzes_summary,
        "overall_stats": {
            "average_percentage": average_percentage,
            "total_questions": total_questions,
            "total_correct": total_correct
        }
    }


@router.get("/quiz-attempts/ungraded")
def get_ungraded_attempts(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
    graded: Optional[bool] = None  # None = ungraded only (default), True = graded only, False = ungraded only
):
    """Get quiz attempts for teachers/curators/admins. By default returns ungraded only.

    Scoping: teachers and curators see their own groups' students only; admins
    and head curators see everything (head_curator is a platform-wide role — it
    has no group-ownership column, and check_course_access grants it every
    course). Curators keep read-only visibility here — they can no longer grade.
    """
    if current_user.role not in ["teacher", "admin", "curator", "head_curator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    query = db.query(QuizAttempt)
    
    # Filter by graded status
    if graded is None or graded == False:
        query = query.filter(QuizAttempt.is_graded == False)
    else:
        query = query.filter(QuizAttempt.is_graded == True)
    
    if current_user.role == "teacher":
        # Filter by teacher's groups - only show attempts from students in teacher's groups
        from src.schemas.models import Group, GroupStudent, CourseGroupAccess
        
        # Get teacher's groups
        teacher_group_ids = db.query(Group.id).filter(
            Group.teacher_id == current_user.id,
            Group.is_active == True
        ).subquery()
        
        # Get students from teacher's groups
        teacher_student_ids = db.query(GroupStudent.student_id).filter(
            GroupStudent.group_id.in_(teacher_group_ids)
        ).subquery()
        
        # Get courses that teacher's groups have access to
        teacher_course_ids = db.query(CourseGroupAccess.course_id).filter(
            CourseGroupAccess.group_id.in_(teacher_group_ids),
            CourseGroupAccess.is_active == True
        ).subquery()
        
        # Filter attempts by teacher's students AND teacher's courses
        query = query.filter(
            QuizAttempt.user_id.in_(teacher_student_ids),
            QuizAttempt.course_id.in_(teacher_course_ids)
        )
    elif current_user.role == "curator":
        # Same shape as the teacher branch, keyed on Group.curator_id — mirrors
        # the curator arm of check_course_access (course access comes from the
        # curator's own groups). Read-only: curators cannot grade.
        from src.schemas.models import Group, GroupStudent, CourseGroupAccess

        curator_group_ids = db.query(Group.id).filter(
            Group.curator_id == current_user.id,
            Group.is_active == True
        ).subquery()

        curator_student_ids = db.query(GroupStudent.student_id).filter(
            GroupStudent.group_id.in_(curator_group_ids)
        ).subquery()

        curator_course_ids = db.query(CourseGroupAccess.course_id).filter(
            CourseGroupAccess.group_id.in_(curator_group_ids),
            CourseGroupAccess.is_active == True
        ).subquery()

        query = query.filter(
            QuizAttempt.user_id.in_(curator_student_ids),
            QuizAttempt.course_id.in_(curator_course_ids)
        )

    # Admins and head curators are intentionally unfiltered — both are
    # platform-wide roles here, consistent with check_course_access and
    # get_accessible_groups in src/utils/permissions.py.

    attempts = query.order_by(QuizAttempt.created_at.desc()).all()
    
    if not attempts:
        return []
    
    # OPTIMIZATION: Batch fetch all related entities to avoid N+1 queries
    user_ids = list(set(a.user_id for a in attempts))
    step_ids = list(set(a.step_id for a in attempts))
    lesson_ids = list(set(a.lesson_id for a in attempts if a.lesson_id))
    course_ids = list(set(a.course_id for a in attempts))
    
    users_map = {u.id: u for u in db.query(UserInDB).filter(UserInDB.id.in_(user_ids)).all()} if user_ids else {}
    steps_map = {s.id: s for s in db.query(Step).filter(Step.id.in_(step_ids)).all()} if step_ids else {}
    lessons_map = {l.id: l for l in db.query(Lesson).filter(Lesson.id.in_(lesson_ids)).all()} if lesson_ids else {}
    courses_map = {c.id: c for c in db.query(Course).filter(Course.id.in_(course_ids)).all()} if course_ids else {}
    
    # Also fetch lessons for steps that have lesson_ids not in attempts (fallback)
    step_lesson_ids = list(set(s.lesson_id for s in steps_map.values() if s.lesson_id and s.lesson_id not in lessons_map))
    if step_lesson_ids:
        extra_lessons = {l.id: l for l in db.query(Lesson).filter(Lesson.id.in_(step_lesson_ids)).all()}
        lessons_map.update(extra_lessons)
    
    # Enrich response with user and step info (using lookup maps)
    results = []
    for attempt in attempts:
        user = users_map.get(attempt.user_id)
        step = steps_map.get(attempt.step_id)
        
        # Try to get lesson - fallback to getting it from the step
        lesson = lessons_map.get(attempt.lesson_id) if attempt.lesson_id else None
        if not lesson and step:
            lesson = lessons_map.get(step.lesson_id)
        
        course = courses_map.get(attempt.course_id)

        
        # Get quiz questions from step content
        quiz_answers = []
        has_long_text = False
        
        if step and step.content_text:
            import json
            try:
                content = json.loads(step.content_text) if isinstance(step.content_text, str) else step.content_text
                questions = content.get('questions', [])
                
                # Check for global passage (for text_based quizzes)
                global_passage = ''
                if content.get('quiz_type') == 'text_based' or content.get('quiz_media_type') == 'text':
                    global_passage = content.get('quiz_media_url', '')
                
                # Parse saved answers
                answers_map = {}
                if attempt.answers:
                    try:
                        parsed_answers = json.loads(attempt.answers) if isinstance(attempt.answers, str) else attempt.answers
                        # Handle both array [[id, value], ...] and object {id: value} formats
                        if isinstance(parsed_answers, list):
                            answers_map = {str(item[0]): item[1] for item in parsed_answers if isinstance(item, list) and len(item) >= 2}
                        elif isinstance(parsed_answers, dict):
                            answers_map = {str(k): v for k, v in parsed_answers.items()}
                    except Exception as e:
                        print(f"Error parsing answers: {e}")
                
                # Process all questions
                for q in questions:
                    try:
                        q_id = str(q.get('id', ''))
                        q_type = q.get('question_type', 'single_choice')
                        raw_answer = answers_map.get(q_id, '')
                        
                        student_answer_text = str(raw_answer)
                        is_correct = False
                        correct_answer_text = ""
                        
                        # Flag if this attempts has long text that needs grading
                        if q_type == 'long_text':
                            has_long_text = True
                        
                        # Resolve answer text for choice questions
                        if q_type in ['single_choice', 'multiple_choice', 'media_question']:
                            options = q.get('options', []) or []
                            
                            # Get correct answer text
                            correct_idx = q.get('correct_answer')
                            if isinstance(correct_idx, int) and 0 <= correct_idx < len(options):
                                correct_answer_text = options[correct_idx].get('text', '')
                            elif isinstance(correct_idx, list):
                                correct_texts = []
                                for idx in correct_idx:
                                    if isinstance(idx, int) and 0 <= idx < len(options):
                                        correct_texts.append(options[idx].get('text', ''))
                                correct_answer_text = ", ".join(correct_texts)
                                
                            # Resolve student answer text and check correctness
                            try:
                                if q_type == 'multiple_choice':
                                    # Answer might be list of indices
                                    if isinstance(raw_answer, list):
                                        selected_texts = []
                                        for idx in raw_answer:
                                            if isinstance(idx, int) and 0 <= idx < len(options):
                                                selected_texts.append(options[idx].get('text', ''))
                                        student_answer_text = ", ".join(selected_texts) if selected_texts else "No answer"
                                        
                                        if isinstance(correct_idx, list):
                                            # Convert both to sets of integers for comparison to handle potential mixed types
                                            raw_set = {int(x) for x in raw_answer if str(x).isdigit()}
                                            correct_set = {int(x) for x in correct_idx if str(x).isdigit()}
                                            is_correct = raw_set == correct_set
                                else:
                                    # Single choice, answer is index
                                    idx = int(raw_answer) if str(raw_answer).isdigit() else -1
                                    if 0 <= idx < len(options):
                                        student_answer_text = options[idx].get('text', '')
                                        is_correct = (idx == correct_idx)
                                    else:
                                        student_answer_text = "No answer" if not raw_answer else str(raw_answer)
                            except Exception as e:
                                print(f"Error resolving answer for Q {q_id}: {e}")
                        
                        elif q_type in ['short_answer', 'fill_blank', 'text_completion']:
                            # Simple string comparison
                             correct = q.get('correct_answer', '')
                             correct_answer_text = str(correct)
                             if isinstance(correct, list):
                                 is_correct = str(raw_answer).strip().lower() in [str(a).strip().lower() for a in correct]
                                 correct_answer_text = ", ".join([str(a) for a in correct])
                             else:
                                 is_correct = str(raw_answer).strip().lower() == str(correct).strip().lower()

                        # Determine content text (passage)
                        passage = q.get('content_text', '')
                        if not passage and global_passage:
                            passage = global_passage
                            
                        quiz_answers.append({
                            "question_id": q_id,
                            "question_text": q.get('question_text', 'No question text'),
                            "question_type": q_type,
                            "content_text": passage,  # Passage if exists
                            "student_answer": student_answer_text,
                            "is_correct": is_correct,
                            "correct_answer": correct_answer_text,
                            "max_points": q.get('points', 1)
                        })
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        print(f"Error parsing question {q.get('id')} in step {step.id}: {e}")
                        # Continue to next question instead of failing entire quiz
            except Exception as e:
                print(f"Error parsing step content: {e}")
        
        # Only include attempts that need grading (have long text answers)
        # BUT return full context
        if has_long_text:
            results.append({
                "id": attempt.id,
                "user_id": attempt.user_id,
                "user_name": user.name if user else "Unknown",
                "user_email": user.email if user else "Unknown",
                "step_id": attempt.step_id,
                "step_title": step.title if step else "Unknown Step",
                "lesson_id": attempt.lesson_id,
                "lesson_title": lesson.title if lesson else "Unknown Lesson",
                "course_id": attempt.course_id,
                "course_title": course.title if course else "Unknown Course",
                "created_at": attempt.created_at,
                "quiz_title": attempt.quiz_title,
                "score_percentage": attempt.score_percentage,
                "is_graded": attempt.is_graded if attempt.is_graded is not None else False,
                "feedback": attempt.feedback,
                "quiz_answers": quiz_answers,
                "quiz_media_type": content.get('quiz_media_type'),
                "quiz_media_url": content.get('quiz_media_url'),
                "type": "quiz"  # To distinguish from assignment submissions
            })
        
    return results

@router.post("/manual-unlock", response_model=List[ManualLessonUnlockSchema])
def manual_unlock_lesson(
    unlock_data: ManualLessonUnlockCreateSchema,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """
    Manually unlock a lesson for a student, a group, or all groups taught by the teacher.
    """
    lesson = db.query(Lesson).filter(Lesson.id == unlock_data.lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")

    target_group_ids = []
    if unlock_data.unlock_all_teacher_groups:
        if current_user.role == "admin":
             # For admin, "all teacher groups" doesn't make much sense without a teacher_id, 
             # but let's assume it means all groups in the course? 
             # Actually, simpler: if admin wants all, they can specify groups.
             # User specifically mentioned "all groups which he teaches".
             teacher_groups = db.query(Group).all() # Admin sees everything
             target_group_ids = [g.id for g in teacher_groups]
        elif current_user.role == "head_teacher":
            from src.utils.permissions import get_head_teacher_group_ids
            target_group_ids = get_head_teacher_group_ids(current_user, db)
        else:
            teacher_groups = db.query(Group).filter(Group.teacher_id == current_user.id).all()
            target_group_ids = [g.id for g in teacher_groups]
    elif unlock_data.group_id:
        target_group_ids = [unlock_data.group_id]
    
    results = []
    
    # 1. Handle group unlocks
    for group_id in target_group_ids:
        # Check permission to unlock for this group
        if current_user.role != "admin":
            from src.utils.permissions import check_group_access
            if not check_group_access(group_id, current_user, db):
                continue
        
        # Check if already exists
        existing = db.query(ManualLessonUnlock).filter(
            ManualLessonUnlock.lesson_id == unlock_data.lesson_id,
            ManualLessonUnlock.group_id == group_id
        ).first()
        
        if not existing:
            new_unlock = ManualLessonUnlock(
                lesson_id=unlock_data.lesson_id,
                group_id=group_id,
                granted_by=current_user.id
            )
            db.add(new_unlock)
            results.append(new_unlock)
        else:
            results.append(existing)

    # 2. Handle individual student unlock
    if unlock_data.user_id:
        # Check permission to unlock for this student
        if not check_student_access(unlock_data.user_id, current_user, db):
            raise HTTPException(status_code=403, detail="Access denied to this student")
            
        existing = db.query(ManualLessonUnlock).filter(
            ManualLessonUnlock.lesson_id == unlock_data.lesson_id,
            ManualLessonUnlock.user_id == unlock_data.user_id
        ).first()
        
        if not existing:
            new_unlock = ManualLessonUnlock(
                lesson_id=unlock_data.lesson_id,
                user_id=unlock_data.user_id,
                granted_by=current_user.id
            )
            db.add(new_unlock)
            results.append(new_unlock)
        else:
            results.append(existing)
            
    db.commit()
    for r in results:
        db.refresh(r)
        
    return results

@router.post("/manual-lock")
def manual_lock_lesson(
    lock_data: ManualLessonUnlockCreateSchema, # Reuse schema, but ignoring unlock_all
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """
    Remove a manual unlock for a student or a group.
    """
    query = db.query(ManualLessonUnlock).filter(
        ManualLessonUnlock.lesson_id == lock_data.lesson_id
    )
    
    if lock_data.user_id:
        query = query.filter(ManualLessonUnlock.user_id == lock_data.user_id)
    elif lock_data.group_id:
        query = query.filter(ManualLessonUnlock.group_id == lock_data.group_id)
    else:
        raise HTTPException(status_code=400, detail="Either user_id or group_id must be provided")
        
    unlock_record = query.first()
    if unlock_record:
        # Check permission
        if current_user.role != "admin" and unlock_record.granted_by != current_user.id:
            # Also allow if it's the teacher of the group/student
            has_access = False
            if unlock_record.user_id:
                has_access = check_student_access(unlock_record.user_id, current_user, db)
            elif unlock_record.group_id:
                from src.utils.permissions import check_group_access
                has_access = check_group_access(unlock_record.group_id, current_user, db)
            
            if not has_access:
                 raise HTTPException(status_code=403, detail="Access denied to remove this unlock")
        
        db.delete(unlock_record)
        db.commit()
        return {"detail": "Manual unlock removed"}
    
    return {"detail": "No manual unlock found"}

@router.get("/manual-unlocks", response_model=List[ManualLessonUnlockSchema])
def get_manual_unlocks(
    lesson_id: Optional[int] = None,
    user_id: Optional[int] = None,
    group_id: Optional[int] = None,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """
    List manual unlocks based on filters.
    """
    query = db.query(ManualLessonUnlock)
    
    if lesson_id:
        query = query.filter(ManualLessonUnlock.lesson_id == lesson_id)
    if user_id:
        query = query.filter(ManualLessonUnlock.user_id == user_id)
    if group_id:
        query = query.filter(ManualLessonUnlock.group_id == group_id)
        
    return query.all()


class CompleteLessonsRequest(BaseModel):
    course_id: int
    lesson_ids: List[int]
    user_id: Optional[int] = None
    group_id: Optional[int] = None

    @model_validator(mode="after")
    def validate_target(self):
        if bool(self.user_id) == bool(self.group_id):
            raise ValueError("Specify exactly one of user_id or group_id")
        if not self.lesson_ids:
            raise ValueError("lesson_ids must not be empty")
        return self


@router.post("/complete-lessons", status_code=200)
def complete_lessons_for_target(
    request: CompleteLessonsRequest,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db),
):
    """Mark all steps in selected lessons as completed for a student or all students in a group."""
    course = db.query(Course).filter(Course.id == request.course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    if request.user_id:
        if not check_student_access(request.user_id, current_user, db):
            raise HTTPException(status_code=403, detail="Access denied to this student")

        stats = complete_steps_for_user(
            db,
            request.user_id,
            request.course_id,
            lesson_ids=request.lesson_ids,
        )
        db.commit()

        return {
            "success": True,
            "target_type": "user",
            "target_id": request.user_id,
            "statistics": stats,
        }

    if not check_group_access(request.group_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this group")

    student_ids = [
        gs.student_id
        for gs in db.query(GroupStudent).filter(GroupStudent.group_id == request.group_id).all()
    ]

    if not student_ids:
        raise HTTPException(status_code=400, detail="Group has no students")

    per_student_stats = []
    for student_id in student_ids:
        stats = complete_steps_for_user(
            db,
            student_id,
            request.course_id,
            lesson_ids=request.lesson_ids,
        )
        per_student_stats.append({"user_id": student_id, **stats})

    db.commit()

    return {
        "success": True,
        "target_type": "group",
        "target_id": request.group_id,
        "student_count": len(student_ids),
        "lesson_ids": request.lesson_ids,
        "students": per_student_stats,
    }


@router.post("/reset-lessons", status_code=200)
def reset_lessons_for_target(
    request: CompleteLessonsRequest,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db),
):
    """Reset step progress for selected lessons for a student or all students in a group."""
    course = db.query(Course).filter(Course.id == request.course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    if request.user_id:
        if not check_student_access(request.user_id, current_user, db):
            raise HTTPException(status_code=403, detail="Access denied to this student")

        stats = reset_steps_for_user(
            db,
            request.user_id,
            request.course_id,
            lesson_ids=request.lesson_ids,
        )
        db.commit()

        return {
            "success": True,
            "target_type": "user",
            "target_id": request.user_id,
            "statistics": stats,
        }

    if not check_group_access(request.group_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this group")

    student_ids = [
        gs.student_id
        for gs in db.query(GroupStudent).filter(GroupStudent.group_id == request.group_id).all()
    ]

    if not student_ids:
        raise HTTPException(status_code=400, detail="Group has no students")

    per_student_stats = []
    for student_id in student_ids:
        stats = reset_steps_for_user(
            db,
            student_id,
            request.course_id,
            lesson_ids=request.lesson_ids,
        )
        per_student_stats.append({"user_id": student_id, **stats})

    db.commit()

    return {
        "success": True,
        "target_type": "group",
        "target_id": request.group_id,
        "student_count": len(student_ids),
        "lesson_ids": request.lesson_ids,
        "students": per_student_stats,
    }


@router.get("/lesson-progress-summary")
def get_lesson_progress_summary(
    course_id: int,
    user_id: Optional[int] = None,
    group_id: Optional[int] = None,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db),
):
    """Get per-lesson completion summary for a student or group."""
    if bool(user_id) == bool(group_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Specify exactly one of user_id or group_id",
        )

    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")

    if user_id:
        if not check_student_access(user_id, current_user, db):
            raise HTTPException(status_code=403, detail="Access denied to this student")
        return get_user_lesson_progress_summary(db, user_id, course_id)

    if not check_group_access(group_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this group")

    from src.utils.permissions import get_group_course_ids

    group_course_ids = get_group_course_ids(db, group_id)
    if group_course_ids and course_id not in group_course_ids:
        raise HTTPException(
            status_code=400,
            detail="This group is not linked to the selected course",
        )

    return get_group_lesson_progress_summary(db, group_id, course_id)

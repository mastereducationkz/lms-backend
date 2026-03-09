"""
Admin endpoints for managing student progress
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

from src.config import get_db
from src.schemas.models import (
    UserInDB, StepProgress, Step, Lesson, Course, Module
)
from src.utils.permissions import require_admin

router = APIRouter()


class CompleteStepsRequest(BaseModel):
    user_id: int
    course_id: int
    lesson_ids: Optional[List[int]] = None  # Если None, то все уроки курса
    step_ids: Optional[List[int]] = None    # Если указаны, то только эти шаги


@router.post("/complete-steps-for-user", status_code=200)
def admin_complete_steps_for_user(
    request: CompleteStepsRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """
    Админ может пометить шаги как завершенные за студента.
    
    Варианты использования:
    1. Указать lesson_ids - завершит все шаги в этих уроках
    2. Указать step_ids - завершит только эти конкретные шаги
    3. Не указывать ни то, ни то - завершит все шаги во всем курсе
    """
    
    # Проверяем, что пользователь существует
    user = db.query(UserInDB).filter(UserInDB.id == request.user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Пользователь с ID {request.user_id} не найден"
        )
    
    # Проверяем, что курс существует
    course = db.query(Course).filter(Course.id == request.course_id).first()
    if not course:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Курс с ID {request.course_id} не найден"
        )
    
    # Получаем шаги для завершения
    query = db.query(Step).join(Lesson).join(Module).filter(
        Module.course_id == request.course_id
    )
    
    if request.step_ids:
        # Завершаем конкретные шаги
        query = query.filter(Step.id.in_(request.step_ids))
    elif request.lesson_ids:
        # Завершаем все шаги в указанных уроках
        query = query.filter(Lesson.id.in_(request.lesson_ids))
    # Иначе завершаем все шаги курса
    
    steps = query.all()
    
    if not steps:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Не найдено шагов для завершения"
        )
    
    completed_count = 0
    updated_count = 0
    skipped_count = 0
    
    now = datetime.utcnow()
    
    for step in steps:
        # Проверяем, есть ли уже запись прогресса
        progress = db.query(StepProgress).filter(
            StepProgress.user_id == request.user_id,
            StepProgress.step_id == step.id
        ).first()
        
        if progress:
            if progress.status == "completed":
                # Уже завершен, пропускаем
                skipped_count += 1
            else:
                # Обновляем существующую запись
                progress.status = "completed"
                progress.completed_at = now
                if not progress.visited_at:
                    progress.visited_at = now
                updated_count += 1
        else:
            # Создаем новую запись прогресса
            new_progress = StepProgress(
                user_id=request.user_id,
                course_id=request.course_id,
                lesson_id=step.lesson_id,
                step_id=step.id,
                status="completed",
                visited_at=now,
                completed_at=now,
                time_spent_minutes=0
            )
            db.add(new_progress)
            completed_count += 1
    
    db.commit()
    
    return {
        "success": True,
        "message": f"Прогресс обновлен для пользователя {user.name}",
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email
        },
        "course": {
            "id": course.id,
            "title": course.title
        },
        "statistics": {
            "total_steps": len(steps),
            "newly_completed": completed_count,
            "updated": updated_count,
            "already_completed": skipped_count
        }
    }


@router.post("/reset-steps-for-user", status_code=200)
def admin_reset_steps_for_user(
    request: CompleteStepsRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """
    Админ может сбросить прогресс по шагам для студента.
    """
    
    # Проверяем пользователя
    user = db.query(UserInDB).filter(UserInDB.id == request.user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Пользователь с ID {request.user_id} не найден"
        )
    
    # Получаем записи прогресса для удаления
    query = db.query(StepProgress).filter(
        StepProgress.user_id == request.user_id,
        StepProgress.course_id == request.course_id
    )
    
    if request.step_ids:
        query = query.filter(StepProgress.step_id.in_(request.step_ids))
    elif request.lesson_ids:
        query = query.filter(StepProgress.lesson_id.in_(request.lesson_ids))
    
    deleted_count = query.delete(synchronize_session=False)
    db.commit()
    
    return {
        "success": True,
        "message": f"Прогресс сброшен для пользователя {user.name}",
        "deleted_records": deleted_count
    }


@router.get("/user-progress-summary/{user_id}/{course_id}")
def get_user_progress_summary(
    user_id: int,
    course_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin())
):
    """
    Получить краткую сводку прогресса студента по курсу (для админа).
    """
    
    # Проверяем пользователя
    user = db.query(UserInDB).filter(UserInDB.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Пользователь с ID {user_id} не найден"
        )
    
    # Проверяем курс
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Курс с ID {course_id} не найден"
        )
    
    # Получаем все модули курса
    modules = db.query(Module).filter(Module.course_id == course_id).all()
    
    lessons_summary = []
    total_steps = 0
    completed_steps = 0
    
    for module in modules:
        lessons = db.query(Lesson).filter(Lesson.module_id == module.id).all()
        
        for lesson in lessons:
            steps = db.query(Step).filter(Step.lesson_id == lesson.id).all()
            lesson_total = len(steps)
            total_steps += lesson_total
            
            # Получаем прогресс по этому уроку
            progress_records = db.query(StepProgress).filter(
                StepProgress.user_id == user_id,
                StepProgress.lesson_id == lesson.id,
                StepProgress.status == "completed"
            ).count()
            
            completed_steps += progress_records
            
            lessons_summary.append({
                "lesson_id": lesson.id,
                "lesson_title": lesson.title,
                "module_title": module.title,
                "total_steps": lesson_total,
                "completed_steps": progress_records,
                "completion_percentage": round((progress_records / lesson_total * 100), 1) if lesson_total > 0 else 0
            })
    
    overall_percentage = round((completed_steps / total_steps * 100), 1) if total_steps > 0 else 0
    
    return {
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email
        },
        "course": {
            "id": course.id,
            "title": course.title
        },
        "overall": {
            "total_steps": total_steps,
            "completed_steps": completed_steps,
            "completion_percentage": overall_percentage
        },
        "lessons": lessons_summary
    }

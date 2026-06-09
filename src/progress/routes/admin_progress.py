"""
Admin endpoints for managing student progress
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from src.config import get_db
from src.schemas.models import UserInDB, Course
from src.utils.permissions import require_admin
from src.progress.services.lesson_completion import (
    complete_steps_for_user,
    reset_steps_for_user,
    get_user_lesson_progress_summary,
)

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
    
    statistics = complete_steps_for_user(
        db,
        request.user_id,
        request.course_id,
        lesson_ids=request.lesson_ids,
        step_ids=request.step_ids,
    )

    if statistics["total_steps"] == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Не найдено шагов для завершения"
        )

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
        "statistics": statistics
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
    
    reset_stats = reset_steps_for_user(
        db,
        request.user_id,
        request.course_id,
        lesson_ids=request.lesson_ids,
        step_ids=request.step_ids,
    )
    db.commit()

    return {
        "success": True,
        "message": f"Прогресс сброшен для пользователя {user.name}",
        "deleted_records": reset_stats["deleted_step_records"],
        "deleted_lesson_records": reset_stats["deleted_lesson_records"],
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
    
    return get_user_lesson_progress_summary(db, user_id, course_id)

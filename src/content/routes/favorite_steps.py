"""Routes for favoriting (bookmarking) specific lesson steps."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from src.schemas.models import (
    FavoriteStep,
    FavoriteStepCreateSchema,
    FavoriteStepItemSchema,
    UserInDB,
    Step,
    Lesson,
    Module,
    Course,
)
from src.routes.auth import get_current_user_dependency
from src.config import get_db

router = APIRouter()


def _resolve_lesson_and_course(db: Session, step_id: int):
    """Return (lesson_id, course_id) for a step, or None if the step does not exist."""
    row = (
        db.query(Lesson.id, Module.course_id)
        .select_from(Step)
        .join(Lesson, Step.lesson_id == Lesson.id)
        .join(Module, Lesson.module_id == Module.id)
        .filter(Step.id == step_id)
        .first()
    )
    if not row:
        return None
    return row[0], row[1]


def _enriched_query(db: Session):
    return (
        db.query(
            FavoriteStep.id,
            FavoriteStep.step_id,
            FavoriteStep.lesson_id,
            FavoriteStep.course_id,
            FavoriteStep.created_at,
            Course.title,
            Lesson.title,
            Step.order_index,
            Step.title,
            Step.content_type,
        )
        .join(Step, FavoriteStep.step_id == Step.id)
        .join(Lesson, FavoriteStep.lesson_id == Lesson.id)
        .join(Course, FavoriteStep.course_id == Course.id)
    )


def _row_to_item(r) -> FavoriteStepItemSchema:
    return FavoriteStepItemSchema(
        id=r[0], step_id=r[1], lesson_id=r[2], course_id=r[3], created_at=r[4],
        course_title=r[5], lesson_title=r[6], order_index=r[7],
        step_title=r[8], content_type=r[9],
    )


@router.post("", response_model=FavoriteStepItemSchema, status_code=status.HTTP_201_CREATED)
def add_favorite_step(
    payload: FavoriteStepCreateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Favorite a specific step for the current user (idempotent)."""
    resolved = _resolve_lesson_and_course(db, payload.step_id)
    if not resolved:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Step not found")
    lesson_id, course_id = resolved

    existing = db.query(FavoriteStep).filter(
        FavoriteStep.user_id == current_user.id,
        FavoriteStep.step_id == payload.step_id,
    ).first()
    if existing:
        fav_id = existing.id
    else:
        fav = FavoriteStep(
            user_id=current_user.id,
            step_id=payload.step_id,
            lesson_id=lesson_id,
            course_id=course_id,
        )
        db.add(fav)
        db.commit()
        db.refresh(fav)
        fav_id = fav.id

    row = _enriched_query(db).filter(FavoriteStep.id == fav_id).first()
    return _row_to_item(row)


@router.get("", response_model=List[FavoriteStepItemSchema])
def get_favorite_steps(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """List the current user's favorite steps, newest first, enriched for display."""
    rows = (
        _enriched_query(db)
        .filter(FavoriteStep.user_id == current_user.id)
        .order_by(FavoriteStep.created_at.desc())
        .all()
    )
    return [_row_to_item(r) for r in rows]


@router.get("/check/{step_id}")
def check_step_is_favorite(
    step_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Return whether the given step is favorited by the current user."""
    fav = db.query(FavoriteStep).filter(
        FavoriteStep.user_id == current_user.id,
        FavoriteStep.step_id == step_id,
    ).first()
    return {"is_favorite": fav is not None, "favorite_id": fav.id if fav else None}


@router.delete("/{step_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_favorite_step(
    step_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Remove the current user's favorite for the given step."""
    fav = db.query(FavoriteStep).filter(
        FavoriteStep.user_id == current_user.id,
        FavoriteStep.step_id == step_id,
    ).first()
    if not fav:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Favorite step not found")
    db.delete(fav)
    db.commit()
    return None

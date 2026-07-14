from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from src.config import get_db
from src.routes.auth import get_current_user_dependency
from src.schemas.models import UserInDB
from src.messages.models import Notification
from src.messages.schemas import NotificationSchema

router = APIRouter()


@router.get("", response_model=List[NotificationSchema])
def list_notifications(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    unread_only: bool = False,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Current user's notifications, newest first."""
    query = db.query(Notification).filter(Notification.user_id == current_user.id)
    if unread_only:
        query = query.filter(Notification.is_read == False)
    rows = query.order_by(desc(Notification.created_at)).offset(skip).limit(limit).all()
    return [NotificationSchema.from_orm(n) for n in rows]


@router.get("/unread-count")
def unread_notification_count(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    count = db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.is_read == False,
    ).count()
    return {"count": count}


@router.put("/read-all")
def mark_all_notifications_read(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    updated = db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.is_read == False,
    ).update({Notification.is_read: True})
    db.commit()
    return {"updated": updated}


@router.put("/{notification_id}/read")
def mark_notification_read(
    notification_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    notification = db.query(Notification).filter(
        Notification.id == notification_id,
        Notification.user_id == current_user.id,
    ).first()
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification.is_read = True
    db.commit()
    return {"detail": "marked read"}

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional

from src.config import get_db
from src.routes.auth import get_current_user_dependency
from src.schemas.models import UserInDB
from src.messages import group_service
from src.messages.group_schemas import PostGroupMessage

router = APIRouter()


@router.get("/groups")
def list_group_conversations(current_user: UserInDB = Depends(get_current_user_dependency),
                             db: Session = Depends(get_db)):
    return group_service.list_conversations(db, current_user.id)


@router.get("/groups/{conversation_id}")
def get_group_messages(conversation_id: int,
                       limit: int = Query(50, le=100),
                       before_id: Optional[int] = None,
                       current_user: UserInDB = Depends(get_current_user_dependency),
                       db: Session = Depends(get_db)):
    try:
        return group_service.get_messages(db, current_user.id, conversation_id, limit, before_id)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not a member of this conversation")


@router.post("/groups/{conversation_id}")
def post_group_message(conversation_id: int,
                       payload: PostGroupMessage,
                       current_user: UserInDB = Depends(get_current_user_dependency),
                       db: Session = Depends(get_db)):
    try:
        msg = group_service.post_message(db, current_user.id, conversation_id,
                                         payload.content, payload.file_url)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not a member of this conversation")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Push fan-out to all group members (mirrors the socket `group:message:send`
    # handler in socket_messages.py for parity). Realtime socket emit is
    # intentionally not done here, matching the 1:1 REST path's behavior.
    from src.messages.group_service import member_ids, _title
    from src.schemas.models import GroupConversation
    from src.utils.push_notifications import send_group_message_push
    conv = db.query(GroupConversation).filter_by(id=conversation_id).first()
    title = _title(db, conv) if conv else "Group chat"
    send_group_message_push(
        db,
        member_ids=member_ids(db, conversation_id),
        sender_name=msg["sender_name"],
        conversation_id=conversation_id,
        title=title,
        message_preview=msg["content"] or "📎 Attachment",
        sender_id=current_user.id,
    )

    return msg

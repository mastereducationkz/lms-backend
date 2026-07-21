"""Business logic for group chat: shared by the REST router and socket handlers."""
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from src.schemas.models import (
    UserInDB, Group, GroupConversation, GroupConversationMember, GroupMessage,
)


def _membership(db: Session, user_id: int, conversation_id: int):
    return (db.query(GroupConversationMember)
              .filter_by(conversation_id=conversation_id, user_id=user_id).first())


def _require_member(db: Session, user_id: int, conversation_id: int) -> GroupConversationMember:
    m = _membership(db, user_id, conversation_id)
    if m is None:
        raise PermissionError("Not a member of this conversation")
    return m


def _can_post(db: Session, user: UserInDB, conv: GroupConversation) -> bool:
    # Special-group students are read-only in group channels (mirrors the 1:1 is_special rule).
    if user.role == "student":
        group = db.query(Group).filter_by(id=conv.group_id).first()
        if group is not None and group.is_special:
            return False
    return True


def _message_dict(msg: GroupMessage, sender: UserInDB) -> dict:
    return {
        "id": msg.id,
        "conversation_id": msg.conversation_id,
        "from_user_id": msg.from_user_id,
        "sender_name": sender.name if sender else "Unknown",
        "content": msg.content,
        "file_url": msg.file_url,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }


def _title(db: Session, conv: GroupConversation) -> str:
    group = db.query(Group).filter_by(id=conv.group_id).first()
    gname = group.name if group else f"Group {conv.group_id}"
    return f"{gname} · {'Parents' if conv.kind == 'parents' else 'Class'}"


def list_conversations(db: Session, user_id: int) -> list:
    members = db.query(GroupConversationMember).filter_by(user_id=user_id).all()
    out = []
    for m in members:
        conv = db.query(GroupConversation).filter_by(id=m.conversation_id).first()
        if conv is None:
            continue
        last = (db.query(GroupMessage).filter_by(conversation_id=conv.id)
                  .order_by(GroupMessage.created_at.desc()).first())
        unread_q = db.query(GroupMessage).filter(GroupMessage.conversation_id == conv.id,
                                                 GroupMessage.from_user_id != user_id)
        if m.last_read_at is not None:
            unread_q = unread_q.filter(GroupMessage.created_at > m.last_read_at)
        sender = db.query(UserInDB).filter_by(id=last.from_user_id).first() if last else None
        out.append({
            "id": conv.id,
            "group_id": conv.group_id,
            "kind": conv.kind,
            "title": _title(db, conv),
            "last_message": _message_dict(last, sender) if last else None,
            "unread_count": unread_q.count(),
        })
    out.sort(key=lambda c: (c["last_message"] or {}).get("created_at") or "", reverse=True)
    return out


def get_messages(db: Session, user_id: int, conversation_id: int,
                 limit: int = 50, before_id: int = None) -> list:
    _require_member(db, user_id, conversation_id)
    q = db.query(GroupMessage).filter_by(conversation_id=conversation_id)
    if before_id:
        q = q.filter(GroupMessage.id < before_id)
    rows = q.order_by(GroupMessage.id.desc()).limit(limit).all()
    rows.reverse()
    sender_ids = {r.from_user_id for r in rows}
    senders = {u.id: u for u in db.query(UserInDB).filter(UserInDB.id.in_(sender_ids)).all()} if sender_ids else {}
    return [_message_dict(r, senders.get(r.from_user_id)) for r in rows]


def post_message(db: Session, user_id: int, conversation_id: int,
                 content: str, file_url: str = None) -> dict:
    member = _require_member(db, user_id, conversation_id)
    conv = db.query(GroupConversation).filter_by(id=conversation_id).first()
    user = db.query(UserInDB).filter_by(id=user_id).first()
    content = (content or "").strip()
    if not content and not file_url:
        raise ValueError("Message must have content or an attachment")
    if not _can_post(db, user, conv):
        raise ValueError("You do not have permission to post in this conversation")
    msg = GroupMessage(conversation_id=conversation_id, from_user_id=user_id,
                       content=content, file_url=file_url)
    db.add(msg)
    member.last_read_at = datetime.now(timezone.utc)
    db.commit(); db.refresh(msg)
    return _message_dict(msg, user)


def mark_read(db: Session, user_id: int, conversation_id: int) -> None:
    member = _require_member(db, user_id, conversation_id)
    member.last_read_at = datetime.now(timezone.utc)
    db.commit()


def member_ids(db: Session, conversation_id: int) -> list:
    rows = db.query(GroupConversationMember.user_id).filter_by(conversation_id=conversation_id)
    return [r[0] for r in rows]

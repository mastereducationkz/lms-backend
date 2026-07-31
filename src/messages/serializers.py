"""Serialization helpers that produce the ONE canonical message payload shared by
the REST endpoints and the Socket.IO handlers, so the web and mobile clients see
identical shapes whether a message arrives over HTTP or the socket.

Payload shape (``serialize_message``)::

    {
      "id": int,
      "from_user_id": int,
      "to_user_id": int,
      "sender_name": str,
      "recipient_name": str,
      "content": str,
      "file_url": str | None,
      "is_read": bool,
      "delivered_at": iso8601 | None,
      "read_at": iso8601 | None,
      "reply_to_message_id": int | None,
      "reply_preview": { id, content, file_url, from_user_id, sender_name } | None,
      "reactions": [ { user_id, user_name, emoji }, ... ],
      "created_at": iso8601,
    }
"""
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from src.messages.models import Message, MessageReaction
from src.auth.models import UserInDB


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _name_map(db: Session, user_ids) -> Dict[int, str]:
    ids = {i for i in user_ids if i is not None}
    if not ids:
        return {}
    rows = db.query(UserInDB.id, UserInDB.name).filter(UserInDB.id.in_(ids)).all()
    return {uid: name for uid, name in rows}


def _reactions_map(db: Session, message_ids) -> Dict[int, List[dict]]:
    """message_id -> [ {user_id, user_name, emoji}, ... ] for the given messages."""
    ids = [i for i in message_ids if i is not None]
    if not ids:
        return {}
    rows = (
        db.query(MessageReaction, UserInDB.name)
        .outerjoin(UserInDB, UserInDB.id == MessageReaction.user_id)
        .filter(MessageReaction.message_id.in_(ids))
        .order_by(MessageReaction.created_at.asc())
        .all()
    )
    out: Dict[int, List[dict]] = {}
    for reaction, user_name in rows:
        out.setdefault(reaction.message_id, []).append(
            {"user_id": reaction.user_id, "user_name": user_name, "emoji": reaction.emoji}
        )
    return out


def serialize_messages(db: Session, messages: List[Message]) -> List[dict]:
    """Batch-serialize a list of messages with a bounded number of queries
    (names, reactions, reply previews) — no per-message N+1."""
    if not messages:
        return []

    reply_ids = [m.reply_to_message_id for m in messages if m.reply_to_message_id]
    replies: Dict[int, Message] = {}
    if reply_ids:
        for r in db.query(Message).filter(Message.id.in_(set(reply_ids))).all():
            replies[r.id] = r

    # Collect every user id we need a display name for (participants + reply authors).
    user_ids = set()
    for m in messages:
        user_ids.add(m.from_user_id)
        user_ids.add(m.to_user_id)
    for r in replies.values():
        user_ids.add(r.from_user_id)
    names = _name_map(db, user_ids)

    reactions = _reactions_map(db, [m.id for m in messages])

    def reply_preview(m: Message) -> Optional[dict]:
        if not m.reply_to_message_id:
            return None
        r = replies.get(m.reply_to_message_id)
        if not r:
            return None
        return {
            "id": r.id,
            "content": r.content,
            "file_url": r.file_url,
            "from_user_id": r.from_user_id,
            "sender_name": names.get(r.from_user_id, "Unknown"),
        }

    return [
        {
            "id": m.id,
            "from_user_id": m.from_user_id,
            "to_user_id": m.to_user_id,
            "sender_name": names.get(m.from_user_id, "Unknown"),
            "recipient_name": names.get(m.to_user_id, "Unknown"),
            "content": m.content,
            "file_url": m.file_url,
            "is_read": bool(m.is_read),
            "delivered_at": _iso(m.delivered_at),
            "read_at": _iso(m.read_at),
            "reply_to_message_id": m.reply_to_message_id,
            "reply_preview": reply_preview(m),
            "reactions": reactions.get(m.id, []),
            "created_at": _iso(m.created_at),
        }
        for m in messages
    ]


def serialize_message(db: Session, message: Message) -> dict:
    """Serialize a single message into the canonical payload."""
    return serialize_messages(db, [message])[0]


def reactions_payload(db: Session, message_id: int) -> dict:
    """The ``message:reaction`` broadcast body: the full reaction set for a message."""
    return {
        "message_id": message_id,
        "reactions": _reactions_map(db, [message_id]).get(message_id, []),
    }

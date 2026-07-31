"""Business logic for the WhatsApp-style chat features (reactions, delivery/read
receipts, reply validation, per-conversation mute). Shared by the REST routes
(``routes/messages.py``) and the Socket.IO handlers (``routes/socket_messages.py``)
so both entry points behave identically.

All functions take an open ``Session`` and commit their own writes; the caller is
responsible for broadcasting the resulting change over Socket.IO.
"""
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from src.messages.models import Message, MessageReaction, ConversationMute


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_participant(message: Message, user_id: int) -> bool:
    return user_id in (message.from_user_id, message.to_user_id)


# --- Reactions ---------------------------------------------------------------

def set_reaction(db: Session, user_id: int, message: Message, emoji: str) -> None:
    """Toggle/replace ``user_id``'s reaction on ``message``.

    - No existing reaction  -> add ``emoji``.
    - Same ``emoji`` again   -> remove it (toggle off).
    - Different ``emoji``    -> replace (one reaction per user per message).
    """
    existing = (
        db.query(MessageReaction)
        .filter(MessageReaction.message_id == message.id, MessageReaction.user_id == user_id)
        .first()
    )
    if existing is None:
        db.add(MessageReaction(message_id=message.id, user_id=user_id, emoji=emoji))
    elif existing.emoji == emoji:
        db.delete(existing)
    else:
        existing.emoji = emoji
    db.commit()


def remove_reaction(db: Session, user_id: int, message: Message) -> None:
    (
        db.query(MessageReaction)
        .filter(MessageReaction.message_id == message.id, MessageReaction.user_id == user_id)
        .delete()
    )
    db.commit()


# --- Reply validation --------------------------------------------------------

def validate_reply(db: Session, from_user_id: int, to_user_id: int,
                   reply_to_message_id: Optional[int]) -> bool:
    """A reply target is valid only when it is a message in the *same* 1:1
    conversation (between these two users). Returns True when there is nothing to
    validate (no reply) or the target is in-conversation."""
    if not reply_to_message_id:
        return True
    target = db.query(Message).filter(Message.id == reply_to_message_id).first()
    if not target:
        return False
    participants = {from_user_id, to_user_id}
    return {target.from_user_id, target.to_user_id} == participants


# --- Delivery / read receipts ------------------------------------------------

def mark_delivered(db: Session, user_id: int, partner_id: int) -> List[int]:
    """Mark every message ``partner_id`` -> ``user_id`` that has not yet been
    delivered as delivered now. Returns the affected message ids."""
    msgs = (
        db.query(Message)
        .filter(
            Message.from_user_id == partner_id,
            Message.to_user_id == user_id,
            Message.delivered_at.is_(None),
        )
        .all()
    )
    if not msgs:
        return []
    now = _now()
    for m in msgs:
        m.delivered_at = now
    db.commit()
    return [m.id for m in msgs]


def mark_read_all(db: Session, user_id: int, partner_id: int) -> List[int]:
    """Mark every unread message ``partner_id`` -> ``user_id`` as read (which also
    implies delivered). Returns the affected message ids."""
    msgs = (
        db.query(Message)
        .filter(
            Message.from_user_id == partner_id,
            Message.to_user_id == user_id,
            Message.read_at.is_(None),
        )
        .all()
    )
    if not msgs:
        return []
    now = _now()
    for m in msgs:
        m.is_read = True
        m.read_at = now
        if m.delivered_at is None:
            m.delivered_at = now
    db.commit()
    return [m.id for m in msgs]


def mark_read_one(db: Session, user_id: int, message: Message) -> bool:
    """Mark a single incoming message as read. Returns True when a change was made."""
    if message.to_user_id != user_id:
        return False
    if message.read_at is not None and message.is_read:
        return False
    now = _now()
    message.is_read = True
    message.read_at = now
    if message.delivered_at is None:
        message.delivered_at = now
    db.commit()
    return True


# --- Mute --------------------------------------------------------------------

def is_muted(db: Session, user_id: int, partner_id: int) -> bool:
    return (
        db.query(ConversationMute)
        .filter(ConversationMute.user_id == user_id, ConversationMute.partner_id == partner_id)
        .first()
        is not None
    )


def muted_partner_ids(db: Session, user_id: int) -> List[int]:
    rows = (
        db.query(ConversationMute.partner_id)
        .filter(ConversationMute.user_id == user_id)
        .all()
    )
    return [r[0] for r in rows]


def set_mute(db: Session, user_id: int, partner_id: int, muted: bool) -> None:
    existing = (
        db.query(ConversationMute)
        .filter(ConversationMute.user_id == user_id, ConversationMute.partner_id == partner_id)
        .first()
    )
    if muted and existing is None:
        db.add(ConversationMute(user_id=user_id, partner_id=partner_id))
        db.commit()
    elif not muted and existing is not None:
        db.delete(existing)
        db.commit()

"""Which Telegram chat belongs to which LMS group, and which lesson invitations were sent.

The Telegram bot and its registry of group chats live in the Support platform; the LMS owns
lessons, groups and Meet links. The link between the two worlds is the LMS's to keep: an admin
confirms, once per group, which approved chat is that group's (the LMS suggests matches by
name). The invitation job reads it every minute; Support only carries the message.
"""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from src.models.base import Base


def _now():
    return datetime.now(timezone.utc)


class TelegramGroupLink(Base):
    """One LMS group ↔ its Telegram group chat (Support's telegram_groups.id)."""

    __tablename__ = "telegram_group_links"

    id = Column(Integer, primary_key=True)
    lms_group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, unique=True)
    # Support's registry id, not Telegram's chat id: Support resolves and re-checks it on
    # every send (approved, still active), so a chat that kicked the bot fails loudly there.
    support_group_id = Column(Integer, nullable=False, index=True)
    # The chat's title when it was linked — for display only; Support's registry is the truth.
    chat_title = Column(String, nullable=True)
    linked_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    linked_at = Column(DateTime, nullable=False, default=_now)


class TelegramLessonInvitation(Base):
    """One invitation for one lesson to one group's chat — the job's memory and its audit log.

    The unique (event_id, lms_group_id) row is written *before* the send, so two ticks (or two
    scheduler containers) can never both send; Support's idempotency key covers the retry of a
    send whose answer was lost.
    """

    __tablename__ = "telegram_lesson_invitations"

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    lms_group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    support_group_id = Column(Integer, nullable=False)
    # pending → sent | failed (retried while there is time) | skipped (chat not approved/unknown)
    status = Column(String, nullable=False, default="pending")
    telegram_message_id = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=_now)
    sent_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("event_id", "lms_group_id", name="uq_lesson_invitation_event_group"),
    )

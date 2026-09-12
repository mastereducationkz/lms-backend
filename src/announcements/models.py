"""Which Telegram chat belongs to which LMS group, and which lesson invitations were sent.

The Telegram bot and its registry of group chats live in the Support platform; the LMS owns
lessons, groups and Meet links. The link between the two worlds is the LMS's to keep: an admin
confirms, once per group, which approved chat is that group's (the LMS suggests matches by
name). The invitation job reads it every minute; Support only carries the message.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
)

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


class TelegramGroupQuestion(Base):
    """One question asked of the bot in a group's chat, and what it answered.

    Every request is written here, answered or not: it is the only place a person can read what
    the bot has been telling students, and the only record when a question is handed to a curator.
    The question text is kept as it was typed — including a personal one, which is answered with
    "ask me in private" and never with any data.
    """

    __tablename__ = "telegram_group_questions"

    id = Column(Integer, primary_key=True)
    lms_group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    support_group_id = Column(Integer, nullable=False)
    # Telegram's own ids, which outgrow a 32-bit integer: a supergroup is -100…
    telegram_chat_id = Column(BigInteger, nullable=True)
    chat_title = Column(String, nullable=True)
    message_id = Column(Integer, nullable=True)
    asker_telegram_id = Column(BigInteger, nullable=True)
    asker_username = Column(String, nullable=True)
    asker_name = Column(String, nullable=True)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=True)
    #: The question was personal, so the answer only pointed at the bot's private chat.
    private_hint = Column(Boolean, nullable=False, default=False)
    #: The facts did not cover it; the group's curator was notified.
    handed_to_curator = Column(Boolean, nullable=False, default=False)
    #: Which model wrote the answer, or "facts" when the plain template did.
    model = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)


class TelegramLessonChangeNotice(Base):
    """One notice to one group's chat that an approved lesson request moved, cancelled, or
    handed a lesson to a substitute (owner, 2026-09-12).

    Queued inside the same transaction that applies the change — see
    :mod:`src.lesson_requests.helpers` — so a notice exists if and only if the change it
    describes was actually committed. A lesson shared by several groups (``EventGroup``) gets
    one row per linked chat; the ``(lesson_request_id, lms_group_id)`` row is the job's memory
    and its audit log, the same shape as :class:`TelegramLessonInvitation`.

    The old time/teacher is snapshotted here because by the time this is sent the event already
    reads as its new self — the row is the only place "what changed" still exists.
    """

    __tablename__ = "telegram_lesson_change_notices"

    id = Column(Integer, primary_key=True)
    lesson_request_id = Column(Integer, ForeignKey("lesson_requests.id", ondelete="CASCADE"), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True)
    lms_group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    support_group_id = Column(Integer, nullable=False)
    # "rescheduled" | "cancelled" | "substituted"
    change_type = Column(String(16), nullable=False)
    old_start_datetime = Column(DateTime, nullable=True)
    new_start_datetime = Column(DateTime, nullable=True)
    old_teacher_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    new_teacher_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # pending → sent | failed (retried while there is time) | skipped (chat not approved/unknown)
    status = Column(String, nullable=False, default="pending")
    telegram_message_id = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=_now)
    sent_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("lesson_request_id", "lms_group_id", name="uq_lesson_change_notice_request_group"),
    )

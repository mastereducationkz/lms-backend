from sqlalchemy import Column, String, Integer, DateTime, Boolean, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from src.models.base import Base


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    from_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    to_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False)
    file_url = Column(String, nullable=True)  # optional attachment (image/file), served from /uploads
    # WhatsApp-style reply: points at the quoted message in the same 1:1 conversation.
    # ondelete SET NULL so deleting the quoted message never cascades away the reply.
    reply_to_message_id = Column(
        Integer, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    is_read = Column(Boolean, default=False)
    # Delivery receipts: delivered_at = reached the recipient's device/session,
    # read_at = recipient opened the thread. is_read is kept in sync with read_at
    # for backward compatibility with existing callers.
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    sender = relationship("UserInDB", foreign_keys=[from_user_id], back_populates="sent_messages")
    recipient = relationship("UserInDB", foreign_keys=[to_user_id], back_populates="received_messages")
    reply_to = relationship("Message", remote_side=[id], foreign_keys=[reply_to_message_id])
    reactions = relationship(
        "MessageReaction", back_populates="message",
        cascade="all, delete-orphan", passive_deletes=True,
    )


class MessageReaction(Base):
    """A single emoji reaction on a 1:1 message. One reaction per user per message
    (the unique constraint) — reacting again with a different emoji replaces it,
    reacting with the same emoji toggles it off (handled in the service layer)."""
    __tablename__ = "message_reactions"
    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    emoji = Column(String(16), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_message_reaction_user"),
    )

    message = relationship("Message", back_populates="reactions")
    user = relationship("UserInDB", foreign_keys=[user_id])


class ConversationMute(Base):
    """Per-user mute flag for a 1:1 conversation. ``user_id`` muted the conversation
    with ``partner_id``; while present, new-message pushes to ``user_id`` from
    ``partner_id`` are suppressed and the thread shows a muted icon."""
    __tablename__ = "conversation_mutes"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    partner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "partner_id", name="uq_conversation_mute"),
    )


class MessageReport(Base):
    """User-flagged chat message awaiting admin review (App Store guideline 1.2)."""
    __tablename__ = "message_reports"
    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True)
    reporter_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    reason = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="open")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint('message_id', 'reporter_id', name='uq_message_report_reporter'),
    )

    message = relationship("Message")
    reporter = relationship("UserInDB", foreign_keys=[reporter_id])


class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    notification_type = Column(String, nullable=False)
    related_id = Column(Integer, nullable=True)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("UserInDB", back_populates="notifications")

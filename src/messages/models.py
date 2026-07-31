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
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    sender = relationship("UserInDB", foreign_keys=[from_user_id], back_populates="sent_messages")
    recipient = relationship("UserInDB", foreign_keys=[to_user_id], back_populates="received_messages")


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

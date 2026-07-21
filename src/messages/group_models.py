from sqlalchemy import (
    Column, String, Integer, DateTime, Text, ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from src.models.base import Base


class GroupConversation(Base):
    """A chat channel derived from a Group. kind='class' (students+staff) or 'parents' (parents+staff)."""
    __tablename__ = "group_conversations"
    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(16), nullable=False)  # 'class' | 'parents'
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    members = relationship("GroupConversationMember", back_populates="conversation",
                           cascade="all, delete-orphan")
    messages = relationship("GroupMessage", back_populates="conversation",
                            cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("group_id", "kind", name="uq_group_conversation_group_kind"),
    )


class GroupConversationMember(Base):
    __tablename__ = "group_conversation_members"
    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("group_conversations.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    last_read_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    conversation = relationship("GroupConversation", back_populates="members")
    user = relationship("UserInDB", back_populates="group_chat_memberships")

    __table_args__ = (
        UniqueConstraint("conversation_id", "user_id", name="uq_group_conv_member"),
    )


class GroupMessage(Base):
    __tablename__ = "group_messages"
    id = Column(Integer, primary_key=True, index=True)
    # No single-column index=True here: the composite (conversation_id, created_at) index below
    # covers conversation_id equality lookups, and this keeps the model in sync with the migration
    # (avoids spurious alembic autogenerate drift).
    conversation_id = Column(Integer, ForeignKey("group_conversations.id", ondelete="CASCADE"),
                             nullable=False)
    from_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content = Column(Text, nullable=False)
    file_url = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    conversation = relationship("GroupConversation", back_populates="messages")
    sender = relationship("UserInDB", back_populates="sent_group_messages")

    __table_args__ = (
        Index("idx_group_messages_conv_created", "conversation_id", "created_at"),
    )

from sqlalchemy import Column, String, Integer, DateTime, Boolean, ForeignKey, Text, JSON, Index, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from src.models.base import Base


class CuratorTaskTemplate(Base):
    """
    Template defining a type of curator task.
    Admin creates templates; the scheduler generates instances from them.
    """
    __tablename__ = "curator_task_templates"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    task_type = Column(String, nullable=False)
    scope = Column(String, nullable=False, default="student")
    recurrence_rule = Column(JSON, nullable=True)
    deadline_rule = Column(JSON, nullable=True)
    order_index = Column(Integer, default=0)
    applicable_from_week = Column(Integer, nullable=True)
    applicable_to_week = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    instances = relationship("CuratorTaskInstance", back_populates="template",
                             cascade="all, delete-orphan")


class CuratorTaskInstance(Base):
    """
    A concrete task instance assigned to a curator.
    Created by scheduler (for recurring) or triggered by events (onboarding, renewal).
    """
    __tablename__ = "curator_task_instances"

    id = Column(Integer, primary_key=True, index=True)
    template_id = Column(Integer, ForeignKey("curator_task_templates.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    curator_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=True, index=True)
    status = Column(String, nullable=False, default="pending")
    due_date = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    result_text = Column(Text, nullable=True)
    screenshot_url = Column(String, nullable=True)
    week_reference = Column(String, nullable=True)
    program_week = Column(Integer, nullable=True)
    custom_title = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    template = relationship("CuratorTaskTemplate", back_populates="instances")
    curator = relationship("UserInDB", foreign_keys=[curator_id])
    student = relationship("UserInDB", foreign_keys=[student_id])
    group = relationship("Group", foreign_keys=[group_id])

    __table_args__ = (
        Index('ix_curator_task_instances_curator_status', 'curator_id', 'status'),
        Index('ix_curator_task_instances_week', 'week_reference'),
    )


class CuratorOnboarding(Base):
    """
    One onboarding card per (curator, student) pairing.
    Rows are created/retired by the onboarding reconciler based on the live
    (curator <-> student) relationship, NOT on account age.
    """
    __tablename__ = "curator_onboarding"

    id = Column(Integer, primary_key=True, index=True)
    curator_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="SET NULL"),
                      nullable=True)
    status = Column(String, nullable=False, default="new")  # new|in_progress|done|cancelled
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)
    completed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                          nullable=True)

    curator = relationship("UserInDB", foreign_keys=[curator_id])
    student = relationship("UserInDB", foreign_keys=[student_id])
    group = relationship("Group", foreign_keys=[group_id])

    __table_args__ = (
        UniqueConstraint("curator_id", "student_id", name="uq_curator_onboarding_pair"),
        Index("ix_curator_onboarding_curator_status", "curator_id", "status"),
    )

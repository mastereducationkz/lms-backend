from sqlalchemy import Column, String, Integer, DateTime, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from src.models.base import Base


class ParentStudent(Base):
    """Association between a parent user and a student user.

    A parent can be linked to multiple students; a student can have multiple
    parents/guardians. Access control for parent-facing endpoints is enforced by
    checking for the existence of a matching row here (see ``src/parents/routes.py``).
    """
    __tablename__ = "parent_students"

    id = Column(Integer, primary_key=True, index=True)
    parent_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # DB column name is "relationship" (per spec); Python attribute avoids shadowing
    # SQLAlchemy's relationship() helper.
    relationship_type = Column("relationship", String, nullable=True)  # mother | father | guardian | ...
    is_primary = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    parent = relationship("UserInDB", foreign_keys=[parent_id], back_populates="children_links")
    student = relationship("UserInDB", foreign_keys=[student_id], back_populates="parent_links")

    __table_args__ = (
        UniqueConstraint("parent_id", "student_id", name="uq_parent_students_parent_student"),
    )

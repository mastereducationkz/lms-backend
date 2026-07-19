from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Index, text
from sqlalchemy.dialects.postgresql import JSONB

from src.models.base import Base

TRIAL_ACTIVE = "active"
TRIAL_EXPIRED = "expired"
TRIAL_REVOKED = "revoked"
TRIAL_CONVERTED = "converted"


class TrialAccess(Base):
    """A sales-granted, time-boxed lesson-allowlist grant for a prospect user.

    The row IS the access: trial users have no enrollment/group/unlock rows,
    so deactivating this row (or passing expires_at) removes everything.
    """
    __tablename__ = "trial_accesses"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    course_id = Column(Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    lesson_ids = Column(JSONB, nullable=False)  # list[int], validated against course on write
    expires_at = Column(DateTime, nullable=False)  # naive UTC
    status = Column(String, nullable=False, default=TRIAL_ACTIVE, index=True)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    prospect_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    revoked_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index(
            "uq_trial_active_user_course", "user_id", "course_id",
            unique=True, postgresql_where=text("status = 'active'"),
        ),
    )

"""Windows of «no platform access», written by the CRM, read by the curator grid.

The CRM owns the decision: a student who did not renew has their LMS login turned off
(`users.is_active = false`), and turned back on when they pay or a manager returns them.
This table records *when* the login was off, because the leaderboard needs a date range —
a lesson inside the window is one the student could not attend, and counting it as an
absence would make a lapsed subscription look like a collapse in attendance. The same
reasoning the freeze mirror (:mod:`src.curator.freeze_mirror`) applies to freeze days.

One row per window. ``blocked_until`` is NULL while the block is open and becomes the day
access returned; that day itself counts again. Nothing here changes access — the CRM flips
``users.is_active`` in the same transaction it writes these rows.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable

from sqlalchemy import Column, Date, DateTime, Integer, String
from sqlalchemy.orm import Session

from src.models.base import Base

#: Why the login was off. Labels only — the grid renders the same cell for all three.
KIND_NOT_RENEWED = "not_renewed"
KIND_MANUAL = "manual"
KIND_EXPIRED = "expired"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class StudentAccessBlock(Base):
    __tablename__ = "student_access_blocks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    blocked_from = Column(Date, nullable=False)
    #: NULL = still blocked. Set to the day access came back; exclusive.
    blocked_until = Column(Date, nullable=True)
    kind = Column(String(32), nullable=False, default=KIND_MANUAL)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow)


def is_within_block(row: StudentAccessBlock, day: date | None) -> bool:
    """``blocked_from <= day < blocked_until`` (open-ended when ``blocked_until`` is NULL)."""
    if day is None or row.blocked_from is None or day < row.blocked_from:
        return False
    return row.blocked_until is None or day < row.blocked_until


class AccessBlockIndex:
    """All block rows for a page, answering "was this student blocked on this day".

    Built once per request, like :class:`src.curator.freeze_mirror.FreezeIndex`: the
    question is asked per grid cell, and a query per cell would be an N+1 on the busiest
    curator screen there is.
    """

    __slots__ = ("_by_student",)

    def __init__(self, rows: Iterable[StudentAccessBlock]) -> None:
        by_student: dict[int, list[StudentAccessBlock]] = {}
        for row in rows:
            by_student.setdefault(int(row.user_id), []).append(row)
        self._by_student = by_student

    def is_blocked_on(self, user_id: int, day: date | None) -> bool:
        return any(is_within_block(row, day) for row in self._by_student.get(int(user_id), []))


def access_block_index(db: Session, user_ids: Iterable[int]) -> AccessBlockIndex:
    """Every block row for these students, in one query."""
    ids = sorted({int(u) for u in user_ids})
    if not ids:
        return AccessBlockIndex([])
    return AccessBlockIndex(
        db.query(StudentAccessBlock).filter(StudentAccessBlock.user_id.in_(ids)).all()
    )

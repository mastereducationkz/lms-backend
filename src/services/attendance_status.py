"""Single source of truth for attendance-status vocabulary in the LMS.

This is the LMS-side twin of ``crm-master``'s ``src/attendance/status.py``. The two files
must agree value-for-value: the CRM reads this database directly, so a status the LMS counts
as "marked" and the CRM does not is a lesson that is payable on one screen and unpaid on the
other. That divergence is what this module exists to prevent, and it is the reason the tuples
below are duplicated rather than each side keeping its own private list inline.

Vocabulary notes (kept identical to the CRM copy on purpose):

- The LMS writes ``present`` / ``late`` / ``absent``. The remaining aliases are legacy or
  imported values that predate the current writer.
- ``late`` counts as *present*: the student attended, just not on time.
- ``attended`` is deliberately NOT included. In the LMS it is a presentation-layer label
  produced by :func:`~src.services.attendance_service.attendance_status_to_ui`, not a value
  stored in ``attendance.status``. Adding it here would silently widen the payable set.
- ``registered`` is NOT marked. It is the row created when a student is enrolled on a lesson,
  before anybody has taken the register — treating it as marked would make every lesson
  payable the moment it was scheduled.

Every read path that asks "was attendance taken for this lesson?" must classify through this
module. Do not re-inline these tuples.
"""
from __future__ import annotations

from typing import Optional

#: Student was in the lesson. ``late`` counts as present — they attended, just not on time.
PRESENT_STATUSES = frozenset({"present", "presented", "1", "yes", "late"})

#: Student was expected but did not attend.
ABSENT_STATUSES = frozenset({"absent", "0", "no", "missed"})

#: Student was taken off this lesson's roster; the lesson is neither present nor absent for
#: them and must not make the lesson count as "marked".
REMOVED_STATUSES = frozenset({"removed", "excluded"})

#: Statuses that mean somebody actually took the register for this student.
#:
#: A lesson counts as "attendance marked" when at least one row carries one of these, and a
#: lesson is payable only when it is marked — see :mod:`src.services.payable_lessons`.
MARKED_STATUSES = PRESENT_STATUSES | ABSENT_STATUSES


def normalize_status(raw_status: Optional[str]) -> str:
    """Map a raw stored status to one of: present | absent | removed | unknown."""
    status = (raw_status or "").strip().lower()
    if status in PRESENT_STATUSES:
        return "present"
    if status in ABSENT_STATUSES:
        return "absent"
    if status in REMOVED_STATUSES:
        return "removed"
    return "unknown"


def is_marked(raw_status: Optional[str]) -> bool:
    """True when this row means attendance was actually taken for the student."""
    return (raw_status or "").strip().lower() in MARKED_STATUSES


def marked_statuses_for_sql() -> list[str]:
    """Deterministically ordered list for SQL ``IN (...)`` comparisons.

    Callers must compare against ``func.lower(...)`` since these are all lower-case.
    """
    return sorted(MARKED_STATUSES)

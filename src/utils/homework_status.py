"""Shared homework-timeliness helper.

Used by the curator leaderboard grid and the per-student homework list so both agree
on what "submitted late" means: a submission counts as late only when it was submitted
strictly after the effective deadline (a per-student extension overrides the
assignment's due date). All datetimes are naive UTC, as stored.
"""
from datetime import datetime
from typing import Optional


def is_submission_late(
    submitted_at: Optional[datetime],
    due_date: Optional[datetime],
    extended_deadline: Optional[datetime] = None,
) -> bool:
    if submitted_at is None:
        return False
    deadline = extended_deadline or due_date
    if deadline is None:
        return False
    return submitted_at > deadline

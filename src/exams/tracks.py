"""One answer to "which exam tracks is this student on?".

This existed in two places that disagreed, and a student saw the contradiction on a
single screen: the dashboard countdown showed SAT and IELTS while the platform-link
tiles offered IELTS only. The two rules differed in three ways -

  * the countdown looked at EVERY group membership; the tiles only at live groups
  * the countdown counted special groups; the tiles excluded them
  * the countdown matched "sat" as a substring (so "Saturday" qualified); the tiles
    used a word boundary

Both features now call :func:`resolve_student_tracks`, so the dashboard cannot
contradict itself again.

The rule
--------
A track applies when EITHER

  * the student is in a live, non-special group for that program - the authoritative
    signal, since the group is what says "this student is preparing for this exam"; or
  * the student has an upcoming planned date for that exam. Someone sitting SAT in
    December is preparing for SAT even if the group that taught them has already ended,
    and cutting off their platform access on the day the group closes is wrong.

Special-group-only students are excluded entirely: those are internal cohorts, not exam
tracks.
"""
import re
from datetime import date
from typing import List, Optional, Set

from sqlalchemy.orm import Session

from src.assignments.models import AssignmentZeroSubmission
from src.auth.models import UserInDB
from src.courses.models import Group, GroupStudent

# Programs that map to a track. general_english is deliberately absent - it has no exam,
# no platform and no countdown.
TRACKS = ("sat", "nuet", "ielts")

# Word boundaries, so a "Saturday English" group is not read as SAT. The stored
# program_type is preferred; this only catches groups the backfill never labelled
# (NUET in particular was never backfilled).
_NAME_PATTERNS = {
    "sat": re.compile(r"\bsat\b", re.IGNORECASE),
    "nuet": re.compile(r"\bnuet\b", re.IGNORECASE),
    # "iealts" is a real, recurring misspelling in existing group names.
    "ielts": re.compile(r"\b(ielts|iealts)\b", re.IGNORECASE),
}


def _group_track(group: Group) -> Optional[str]:
    stored = (group.program_type or "").strip().lower()
    if stored in TRACKS:
        return stored
    name = group.name or ""
    for track, pattern in _NAME_PATTERNS.items():
        if pattern.search(name):
            return track
    return None


def resolve_student_tracks(db: Session, user: UserInDB) -> List[str]:
    """The student's exam tracks, in a stable SAT -> NUET -> IELTS order."""
    rows = (
        db.query(Group)
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .filter(GroupStudent.student_id == user.id)
        .all()
    )

    live_groups = [
        g for g in rows
        if g.is_active and not g.is_over and not g.is_special
    ]

    tracks: Set[str] = set()
    for group in live_groups:
        track = _group_track(group)
        if track:
            tracks.add(track)

    # An upcoming planned date keeps the track alive past the group's end date.
    submission = (
        db.query(AssignmentZeroSubmission)
        .filter(AssignmentZeroSubmission.user_id == user.id)
        .first()
    )
    if submission is not None:
        today = date.today()
        if submission.sat_planned_test_date and submission.sat_planned_test_date >= today:
            tracks.add("sat")
        if submission.ielts_planned_test_date and submission.ielts_planned_test_date >= today:
            tracks.add("ielts")

    return [t for t in TRACKS if t in tracks]

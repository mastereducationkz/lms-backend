"""Trial access decision logic.

Pure functions (grant_is_active / lesson_in_grant / evaluate_trial_lesson_access)
carry the semantics and are unit-tested without a DB; thin DB helpers wrap them.
"Active" ALWAYS means status == "active" AND now < expires_at — never trust
status alone (the background job that flips statuses is bookkeeping only).
"""
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from src.trials.models import TrialAccess, TRIAL_ACTIVE, TRIAL_EXPIRED

REASON_ENDED = "Your trial has ended"
REASON_NOT_INCLUDED = "Not included in your trial"
REASON_NO_COURSE = "You do not have access to this course"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def grant_is_active(grant, now: Optional[datetime] = None) -> bool:
    if grant is None or grant.status != TRIAL_ACTIVE:
        return False
    ref = _as_utc_naive(now) if now is not None else utcnow()
    return _as_utc_naive(grant.expires_at) > ref


def lesson_in_grant(grant, lesson_id: int) -> bool:
    try:
        allowed = {int(x) for x in (grant.lesson_ids or [])}
    except (TypeError, ValueError):
        return False
    return int(lesson_id) in allowed


def evaluate_trial_lesson_access(
    grant, lesson_id: int, now: Optional[datetime] = None
) -> Tuple[bool, Optional[str]]:
    if grant is None:
        return False, REASON_NO_COURSE
    if not grant_is_active(grant, now):
        return False, REASON_ENDED
    if not lesson_in_grant(grant, lesson_id):
        return False, REASON_NOT_INCLUDED
    return True, None


def get_active_trials(db: Session, user_id: int) -> List[TrialAccess]:
    rows = db.query(TrialAccess).filter(
        TrialAccess.user_id == user_id,
        TrialAccess.status == TRIAL_ACTIVE,
    ).all()
    return [g for g in rows if grant_is_active(g)]


def get_active_trial(db: Session, user_id: int, course_id: int) -> Optional[TrialAccess]:
    grant = db.query(TrialAccess).filter(
        TrialAccess.user_id == user_id,
        TrialAccess.course_id == course_id,
        TrialAccess.status == TRIAL_ACTIVE,
    ).first()
    return grant if grant_is_active(grant) else None


def trial_course_ids(db: Session, user_id: int) -> List[int]:
    return [g.course_id for g in get_active_trials(db, user_id)]


def trial_lesson_access(db: Session, user_id: int, lesson_id: int) -> Tuple[bool, Optional[str]]:
    from src.schemas.models import Lesson, Module

    lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
    if not lesson:
        return False, "Lesson not found"
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not module:
        return False, "Module not found"
    grant = get_active_trial(db, user_id, module.course_id)
    return evaluate_trial_lesson_access(grant, lesson_id)


def earliest_active_expiry(db: Session, user_id: int) -> Optional[datetime]:
    grants = get_active_trials(db, user_id)
    if not grants:
        return None
    return min(_as_utc_naive(g.expires_at) for g in grants)


def expire_stale_trials(db: Session) -> int:
    """Bookkeeping: flip active→expired past deadline. Enforcement never needs this."""
    count = db.query(TrialAccess).filter(
        TrialAccess.status == TRIAL_ACTIVE,
        TrialAccess.expires_at <= utcnow(),
    ).update({TrialAccess.status: TRIAL_EXPIRED}, synchronize_session=False)
    db.commit()
    return count

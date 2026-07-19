from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, model_validator

from src.trials.services import grant_is_active
from src.trials.models import TRIAL_ACTIVE, TRIAL_EXPIRED


def effective_status(grant) -> str:
    """Display status: an 'active' row past its deadline reads as expired."""
    if grant.status == TRIAL_ACTIVE and not grant_is_active(grant):
        return TRIAL_EXPIRED
    return grant.status


class TrialCourseSelection(BaseModel):
    """One course in a (possibly multi-course) trial grant, with its unlocked lessons."""
    course_id: int
    lesson_ids: List[int]


class TrialCreateRequest(BaseModel):
    """Grant one prospect trial access to one OR MORE courses in a single request.

    New clients send ``courses`` (a list of per-course lesson selections). The legacy
    single-course fields (``course_id`` / ``lesson_ids``) are still accepted so an older
    frontend keeps working across a deploy skew; ``_normalize`` folds them into ``courses``.
    """
    email: str
    name: str
    expires_at: datetime
    prospect_note: Optional[str] = None
    send_invite: bool = True

    courses: Optional[List[TrialCourseSelection]] = None
    # Legacy single-course shape (deprecated; kept for backward compatibility).
    course_id: Optional[int] = None
    lesson_ids: Optional[List[int]] = None

    @model_validator(mode="after")
    def _normalize(self):
        sels = list(self.courses or [])
        if not sels and self.course_id is not None:
            sels = [TrialCourseSelection(course_id=self.course_id, lesson_ids=self.lesson_ids or [])]
        if not sels:
            raise ValueError("Select at least one course")
        seen = set()
        for s in sels:
            if s.course_id in seen:
                raise ValueError(f"Course {s.course_id} is listed more than once")
            seen.add(s.course_id)
        self.courses = sels
        return self

    @property
    def selections(self) -> List[TrialCourseSelection]:
        return self.courses or []


class TrialUpdateRequest(BaseModel):
    expires_at: Optional[datetime] = None
    lesson_ids: Optional[List[int]] = None
    prospect_note: Optional[str] = None


class TrialSchema(BaseModel):
    id: int
    user_id: int
    user_email: str
    user_name: str
    course_id: int
    course_title: str
    lesson_ids: List[int]
    expires_at: datetime
    status: str
    granted_by: Optional[int] = None
    granted_by_name: Optional[str] = None
    prospect_note: Optional[str] = None
    created_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class TrialCreateResponse(BaseModel):
    # All grants created by this request (one per course).
    trials: List[TrialSchema]
    # Legacy alias (= trials[0]) so an older frontend reading `.trial` keeps working.
    trial: Optional[TrialSchema] = None
    generated_password: Optional[str] = None

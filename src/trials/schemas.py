from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

from src.trials.services import grant_is_active
from src.trials.models import TRIAL_ACTIVE, TRIAL_EXPIRED


def effective_status(grant) -> str:
    """Display status: an 'active' row past its deadline reads as expired."""
    if grant.status == TRIAL_ACTIVE and not grant_is_active(grant):
        return TRIAL_EXPIRED
    return grant.status


class TrialCreateRequest(BaseModel):
    email: str
    name: str
    course_id: int
    lesson_ids: List[int]
    expires_at: datetime
    prospect_note: Optional[str] = None
    send_invite: bool = True


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
    trial: TrialSchema
    generated_password: Optional[str] = None

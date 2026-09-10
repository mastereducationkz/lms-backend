from pydantic import BaseModel
from datetime import datetime, date
from typing import Optional, List


class Token(BaseModel):
    access_token: str
    refresh_token: str
    type: str


class UserSchema(BaseModel):
    id: int
    email: str
    name: str
    role: str
    avatar_url: Optional[str] = None
    is_active: bool
    student_id: Optional[str] = None
    teacher_name: Optional[str] = None
    curator_name: Optional[str] = None
    group_ids: Optional[List[int]] = None
    total_study_time_minutes: Optional[int] = 0
    daily_streak: Optional[int] = 0
    last_activity_date: Optional[date] = None
    onboarding_completed: Optional[bool] = False
    onboarding_completed_at: Optional[datetime] = None
    assignment_zero_completed: Optional[bool] = False
    assignment_zero_completed_at: Optional[datetime] = None
    # Computed on /auth/me: student in active groups and all groups are special (not a DB column)
    special_group_only_student: Optional[bool] = False
    activity_points: Optional[int] = 0
    no_substitutions: Optional[bool] = False
    course_ids: Optional[List[int]] = []
    created_at: Optional[datetime] = None
    is_analytics_hidden: Optional[bool] = False
    is_trial: Optional[bool] = False
    # Computed on /auth/me for trial users: earliest active grant deadline (None = no active grant)
    trial_expires_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CurrentUserSchema(UserSchema):
    """The signed-in user's own record — the response of ``/auth/me``.

    Holds fields that belong to the viewer alone. They are declared here and not on
    ``UserSchema`` because ``UserSchema`` also serialises *other* people (user lists,
    profile updates made by admins): FastAPI renders each route through its own
    ``response_model``, so a field that exists only on this subclass is dropped from every
    route typed as ``UserSchema`` and reaches nobody but its owner.
    """

    # The Google Workspace address (…@mastereducation.kz). The frontend adds it to Meet
    # links as ``authuser`` so Meet joins on the work account instead of whichever Google
    # account the browser defaults to. It matters because a teacher on a personal account
    # counts as outside the organisation — she cannot record, cannot remove participants,
    # and auto-recording does not start until someone from the organisation joins. First
    # observed live on 2026-09-10: the lesson only recorded because an admin happened to
    # join at 18:59:59.
    workspace_email: Optional[str] = None


class PointHistorySchema(BaseModel):
    id: int
    user_id: int
    amount: int
    reason: str
    description: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True

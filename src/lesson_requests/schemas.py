from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class LessonRequestSchema(BaseModel):
    id: int
    request_type: str
    status: str
    requester_id: int
    requester_name: Optional[str] = None
    lesson_schedule_id: Optional[int] = None
    event_id: Optional[int] = None
    group_id: int
    group_name: Optional[str] = None
    original_datetime: datetime
    substitute_teacher_id: Optional[int] = None
    substitute_teacher_name: Optional[str] = None
    substitute_teacher_ids: Optional[list] = None
    substitute_teacher_names: Optional[list] = None
    confirmed_teacher_id: Optional[int] = None
    confirmed_teacher_name: Optional[str] = None
    new_datetime: Optional[datetime] = None
    reason: Optional[str] = None
    admin_comment: Optional[str] = None
    created_at: datetime
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[int] = None
    # Who decided, in words. `resolved_by` alone is an integer the UI printed as an id or,
    # more often, not at all — so an approved request named nobody accountable for it.
    resolver_name: Optional[str] = None
    resolver_role: Optional[str] = None

    # ── the live schedule, not just what was asked for ──────────────────────────────────
    #
    # A request is a decision; the Event is what actually happens. They can disagree — that
    # is exactly the bug this wave repairs — and a page that shows only the request cannot
    # reveal it. These say what the schedule *currently* holds.
    lesson_title: Optional[str] = None
    #: The teacher the lesson is actually assigned to right now.
    current_event_teacher_id: Optional[int] = None
    current_event_teacher_name: Optional[str] = None
    #: The group's regular teacher — the owner, who does not change on a substitution.
    group_teacher_id: Optional[int] = None
    group_teacher_name: Optional[str] = None
    #: Who owes the register for this lesson. The actual lesson teacher, by definition.
    attendance_owner_id: Optional[int] = None
    attendance_owner_name: Optional[str] = None
    #: Whether the register has already been taken.
    attendance_marked: Optional[bool] = None
    #: True when an approved request's decision is reflected in the Event. False means the
    #: schedule disagrees with an approval — shown to staff as a red consistency warning.
    is_applied: Optional[bool] = None
    #: A human sentence explaining a False `is_applied`, for the people who are not staff.
    consistency_note: Optional[str] = None
    #: Whether the lesson is still active (a cancel request that took effect sets this False).
    lesson_is_active: Optional[bool] = None

    class Config:
        from_attributes = True
        # Datetimes are stored as naive UTC; emit them with a trailing 'Z' so
        # clients parse them as UTC (matches EventSchema) and render in Asia/Almaty.
        json_encoders = {
            datetime: lambda v: v.isoformat() + 'Z' if v else None
        }


class CreateLessonRequestSchema(BaseModel):
    request_type: str
    lesson_schedule_id: Optional[int] = None
    event_id: Optional[int] = None
    group_id: int
    original_datetime: datetime
    substitute_teacher_ids: Optional[list] = None
    substitute_teacher_id: Optional[int] = None
    new_datetime: Optional[datetime] = None
    reason: Optional[str] = None


class ResolveLessonRequestSchema(BaseModel):
    admin_comment: Optional[str] = None

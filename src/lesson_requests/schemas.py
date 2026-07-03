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

from pydantic import BaseModel, field_validator
from datetime import datetime, date, timezone
from typing import Optional, List


class EventSchema(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    event_type: str
    start_datetime: datetime
    end_datetime: datetime
    location: Optional[str] = None
    is_online: bool
    meeting_url: Optional[str] = None
    created_by: int
    creator_name: Optional[str] = None
    is_active: bool
    is_recurring: bool
    recurrence_pattern: Optional[str] = None
    recurrence_end_date: Optional[date] = None
    max_participants: Optional[int] = None
    lesson_id: Optional[int] = None
    teacher_id: Optional[int] = None
    teacher_name: Optional[str] = None
    participant_count: int = 0
    groups: Optional[List[str]] = None
    courses: Optional[List[str]] = None
    group_ids: Optional[List[int]] = None
    course_ids: Optional[List[int]] = None
    # Tolerate NULL timestamps: events inserted directly by CRM historically
    # lacked these, and a single bad row must never 500 the whole calendar month.
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    is_substitution: bool = False

    class Config:
        from_attributes = True
        json_encoders = {
            datetime: lambda v: v.isoformat() + 'Z' if v else None
        }


class CreateEventRequest(BaseModel):
    title: str
    description: Optional[str] = None
    event_type: str
    start_datetime: datetime
    end_datetime: datetime

    @field_validator("start_datetime", "end_datetime", mode="after")
    @classmethod
    def ensure_utc_naive(cls, v):
        if v is None:
            return v
        if isinstance(v, datetime) and v.tzinfo:
            return v.astimezone(timezone.utc).replace(tzinfo=None)
        return v
    location: Optional[str] = None
    is_online: bool = True
    meeting_url: Optional[str] = None
    is_recurring: bool = False
    recurrence_pattern: Optional[str] = None
    recurrence_end_date: Optional[date] = None
    max_participants: Optional[int] = None
    teacher_id: Optional[int] = None
    group_ids: List[int] = []
    course_ids: List[int] = []


class UpdateEventRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    event_type: Optional[str] = None
    start_datetime: Optional[datetime] = None
    end_datetime: Optional[datetime] = None

    @field_validator("start_datetime", "end_datetime", mode="after")
    @classmethod
    def ensure_utc_naive(cls, v):
        if v is None:
            return v
        if isinstance(v, datetime) and v.tzinfo:
            return v.astimezone(timezone.utc).replace(tzinfo=None)
        return v
    location: Optional[str] = None
    is_online: Optional[bool] = None
    meeting_url: Optional[str] = None
    is_active: Optional[bool] = None
    is_recurring: Optional[bool] = None
    recurrence_pattern: Optional[str] = None
    recurrence_end_date: Optional[date] = None
    max_participants: Optional[int] = None
    teacher_id: Optional[int] = None
    group_ids: Optional[List[int]] = None
    course_ids: Optional[List[int]] = None


class EventGroupSchema(BaseModel):
    id: int
    event_id: int
    group_id: int
    group_name: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class EventParticipantSchema(BaseModel):
    id: int
    event_id: int
    user_id: int
    user_name: Optional[str] = None
    registration_status: str
    registered_at: datetime
    attended_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AttendanceRecord(BaseModel):
    student_id: int
    status: str
    activity_score: Optional[float] = None


class AttendanceBulkUpdateSchema(BaseModel):
    attendance: List[AttendanceRecord]


class EventStudentSchema(BaseModel):
    student_id: int
    name: str
    attendance_status: Optional[str] = "registered"
    activity_score: Optional[float] = None
    last_updated: Optional[datetime] = None


class SubstitutionLessonSchema(BaseModel):
    event_id: int
    title: str
    topic: Optional[str] = None
    start_datetime: datetime
    end_datetime: datetime
    group_id: int
    group_name: str
    is_online: bool = True
    location: Optional[str] = None
    meeting_url: Optional[str] = None
    # The group's regular teacher — i.e. who the current user is covering for.
    original_teacher_name: Optional[str] = None
    # Whether any attendance record already exists for this lesson.
    marked: bool = False


class LessonScheduleSchema(BaseModel):
    id: int
    group_id: int
    lesson_id: int
    scheduled_at: datetime
    week_number: int
    is_active: bool

    class Config:
        from_attributes = True


class AttendanceSchema(BaseModel):
    id: int
    lesson_schedule_id: int
    user_id: int
    status: str
    score: int
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

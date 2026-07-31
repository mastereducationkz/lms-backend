from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List


class ReactionSchema(BaseModel):
    user_id: int
    user_name: Optional[str] = None
    emoji: str


class ReplyPreviewSchema(BaseModel):
    id: int
    content: str
    file_url: Optional[str] = None
    from_user_id: int
    sender_name: Optional[str] = None


class MessageSchema(BaseModel):
    id: int
    from_user_id: int
    to_user_id: int
    sender_name: Optional[str] = None
    recipient_name: Optional[str] = None
    content: str
    file_url: Optional[str] = None
    is_read: bool
    delivered_at: Optional[datetime] = None
    read_at: Optional[datetime] = None
    reply_to_message_id: Optional[int] = None
    reply_preview: Optional[ReplyPreviewSchema] = None
    reactions: List[ReactionSchema] = []
    created_at: datetime

    class Config:
        from_attributes = True


class SendMessageSchema(BaseModel):
    to_user_id: int
    content: str = ""          # may be empty when an attachment is sent
    file_url: Optional[str] = None
    reply_to_message_id: Optional[int] = None


class ReactionRequestSchema(BaseModel):
    emoji: str


class ReportMessageSchema(BaseModel):
    reason: Optional[str] = None


class DashboardStatsSchema(BaseModel):
    user: dict
    stats: dict
    recent_courses: List[dict]


class CourseProgressSchema(BaseModel):
    course_id: int
    course_title: str
    teacher_name: str
    cover_image_url: Optional[str] = None
    total_modules: int
    completion_percentage: int
    status: str
    last_accessed: Optional[datetime] = None


class NotificationSchema(BaseModel):
    id: int
    user_id: int
    title: str
    content: str
    notification_type: str
    related_id: Optional[int] = None
    is_read: bool
    created_at: datetime

    class Config:
        from_attributes = True

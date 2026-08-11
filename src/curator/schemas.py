from pydantic import BaseModel, Field
from datetime import datetime
from typing import List, Optional


class CuratorTaskTemplateSchema(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    task_type: str
    scope: str
    recurrence_rule: Optional[dict] = None
    deadline_rule: Optional[dict] = None
    order_index: int = 0
    applicable_from_week: Optional[int] = None
    applicable_to_week: Optional[int] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CuratorTaskTemplateCreateSchema(BaseModel):
    title: str
    description: Optional[str] = None
    task_type: str
    scope: str = "student"
    recurrence_rule: Optional[dict] = None
    deadline_rule: Optional[dict] = None
    order_index: int = 0
    applicable_from_week: Optional[int] = None
    applicable_to_week: Optional[int] = None


class CuratorTaskInstanceSchema(BaseModel):
    id: int
    template_id: int
    template_title: Optional[str] = None
    template_description: Optional[str] = None
    task_type: Optional[str] = None
    scope: Optional[str] = None
    curator_id: int
    curator_name: Optional[str] = None
    student_id: Optional[int] = None
    student_name: Optional[str] = None
    group_id: Optional[int] = None
    group_name: Optional[str] = None
    status: str
    due_date: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result_text: Optional[str] = None
    screenshot_url: Optional[str] = None
    week_reference: Optional[str] = None
    program_week: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CuratorTaskInstanceUpdateSchema(BaseModel):
    status: Optional[str] = None
    result_text: Optional[str] = None
    screenshot_url: Optional[str] = None


class OnboardingStatusUpdate(BaseModel):
    status: str  # new | in_progress | done


class OnboardingCard(BaseModel):
    id: int
    student_id: int
    student_name: str
    group_id: Optional[int] = None
    group_name: Optional[str] = None
    curator_id: int
    curator_name: Optional[str] = None
    telegram_id: Optional[str] = None
    telegram_link: Optional[str] = None
    phone_number: Optional[str] = None
    parent_phone_number: Optional[str] = None
    status: str
    created_at: Optional[datetime] = None


class CuratorTaskBulkCreateSchema(BaseModel):
    """Create the same task for many curator × group combinations (cartesian product)."""

    template_id: int
    curator_ids: List[int] = Field(..., min_length=1)
    group_ids: Optional[List[int]] = None
    student_id: Optional[int] = None
    due_date: Optional[datetime] = None
    week: Optional[str] = None
    program_week: Optional[int] = None
    custom_title: Optional[str] = None

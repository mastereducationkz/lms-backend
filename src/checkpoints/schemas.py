from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class RequiredUnitInput(BaseModel):
    lesson_id: int
    kind: Literal["verbal", "math"]


class DefinitionUpdate(BaseModel):
    title: Optional[str] = None
    is_active: Optional[bool] = None
    quiz_lesson_id: Optional[int] = None
    total_questions: Optional[int] = Field(default=None, ge=1)
    required_units: Optional[List[RequiredUnitInput]] = None


class GroupSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    start_number: Optional[int] = Field(default=None, ge=1)


class OpenRequest(BaseModel):
    student_ids: Optional[List[int]] = None   # None = whole group
    deadline: Optional[datetime] = None       # None = now + 24h


class DeadlineUpdate(BaseModel):
    deadline: datetime

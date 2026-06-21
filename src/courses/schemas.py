from pydantic import BaseModel, field_validator
from datetime import datetime
from typing import Optional, List, Literal
import json

GroupTypeLiteral = Literal["group", "individual"]
CourseTypeLiteral = Literal["sat", "ielts", "general_english", "nuet"]
# Та же шкала, что у курса — хранится на группе для удобного поиска/фильтрации
ProgramTypeLiteral = CourseTypeLiteral


class GroupSchema(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    teacher_id: Optional[int] = None
    teacher_name: Optional[str] = None
    curator_id: Optional[int] = None
    curator_name: Optional[str] = None
    student_count: int = 0
    students: Optional[List["UserSchema"]] = None
    created_at: datetime
    is_active: bool
    is_special: bool = False
    is_over: bool = False
    group_type: GroupTypeLiteral = "group"
    program_type: ProgramTypeLiteral = "general_english"
    schedule_config: Optional[dict] = None
    current_week: Optional[int] = None
    max_week: Optional[int] = None
    max_open_lessons: Optional[int] = None
    course_id: Optional[int] = None
    course_ids: Optional[List[int]] = None

    class Config:
        from_attributes = True


class GroupStudentSchema(BaseModel):
    id: int
    group_id: int
    student_id: int
    student_name: Optional[str] = None
    student_email: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class StepSchema(BaseModel):
    id: int
    lesson_id: int
    title: str
    content_type: str
    video_url: Optional[str] = None
    content_text: Optional[str] = None
    original_image_url: Optional[str] = None
    attachments: Optional[str] = None
    order_index: int
    created_at: datetime
    content_hash: Optional[str] = None
    is_completed: Optional[bool] = False
    is_optional: Optional[bool] = False

    class Config:
        from_attributes = True


class StepCreateSchema(BaseModel):
    title: str
    content_type: str = "text"
    video_url: Optional[str] = None
    content_text: Optional[str] = None
    original_image_url: Optional[str] = None
    attachments: Optional[str] = None
    order_index: int = 0
    content_hash: Optional[str] = None
    is_optional: Optional[bool] = False


class CourseSchema(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    cover_image_url: Optional[str] = None
    teacher_id: Optional[int] = None
    teacher_name: Optional[str] = None
    estimated_duration_minutes: int
    total_modules: int = 0
    is_active: bool
    created_at: datetime
    status: Optional[str] = None
    release_schedule: str = "all"  # "all" | "weekly"
    course_type: CourseTypeLiteral = "general_english"

    class Config:
        from_attributes = True


class CourseCreateSchema(BaseModel):
    title: str
    description: Optional[str] = None
    cover_image_url: Optional[str] = None
    estimated_duration_minutes: int = 0
    teacher_id: Optional[int] = None
    release_schedule: str = "all"
    course_type: CourseTypeLiteral = "general_english"


class CourseGroupAccessSchema(BaseModel):
    id: int
    course_id: int
    group_id: int
    group_name: Optional[str] = None
    student_count: int = 0
    granted_by: int
    granted_by_name: Optional[str] = None
    granted_at: datetime
    is_active: bool

    class Config:
        from_attributes = True


class CourseTeacherAccessSchema(BaseModel):
    id: int
    course_id: int
    teacher_id: int
    teacher_name: Optional[str] = None
    teacher_email: Optional[str] = None
    granted_by: int
    granted_by_name: Optional[str] = None
    granted_at: datetime
    is_active: bool

    class Config:
        from_attributes = True


class ModuleSchema(BaseModel):
    id: int
    course_id: int
    title: str
    description: Optional[str] = None
    order_index: int
    total_lessons: int = 0
    lessons: Optional[List[dict]] = None
    created_at: datetime
    is_completed: Optional[bool] = False
    week_number: Optional[int] = None  # For weekly release: which week (1-based) this module unlocks

    class Config:
        from_attributes = True


class ModuleCreateSchema(BaseModel):
    title: str
    description: Optional[str] = None
    order_index: int = 0
    week_number: Optional[int] = None


class BaseLessonSchema(BaseModel):
    id: int
    module_id: int
    course_id: Optional[int] = None
    title: str
    description: Optional[str] = None
    duration_minutes: int
    order_index: int
    created_at: datetime
    next_lesson_id: Optional[int] = None
    is_initially_unlocked: Optional[bool] = False
    steps: Optional[List[StepSchema]] = None
    is_completed: Optional[bool] = False

    class Config:
        from_attributes = True


class LessonSchema(BaseLessonSchema):
    total_steps: int = 0


class LegacyLessonSchema(BaseModel):
    id: int
    module_id: int
    title: str
    description: Optional[str] = None
    content_type: str
    video_url: Optional[str] = None
    content_text: Optional[str] = None
    duration_minutes: int
    order_index: int
    created_at: datetime
    quiz_data: Optional[dict] = None

    @field_validator('quiz_data', mode='before')
    @classmethod
    def parse_quiz_data(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, ValueError):
                return None
        return v

    class Config:
        from_attributes = True


class LessonCreateSchema(BaseModel):
    title: str
    description: Optional[str] = None
    duration_minutes: int = 0
    order_index: int = 0
    next_lesson_id: Optional[int] = None
    is_initially_unlocked: bool = False


class LessonMaterialSchema(BaseModel):
    id: int
    lesson_id: int
    title: str
    file_type: str
    file_url: str
    file_size_bytes: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ManualLessonUnlockSchema(BaseModel):
    id: int
    lesson_id: int
    user_id: Optional[int] = None
    group_id: Optional[int] = None
    granted_by: int
    created_at: datetime

    class Config:
        from_attributes = True


class ManualLessonUnlockCreateSchema(BaseModel):
    lesson_id: int
    user_id: Optional[int] = None
    group_id: Optional[int] = None
    unlock_all_teacher_groups: Optional[bool] = False


class EnrollmentSchema(BaseModel):
    id: int
    user_id: int
    course_id: int
    enrolled_at: datetime
    completed_at: Optional[datetime] = None
    is_active: bool

    class Config:
        from_attributes = True


# Resolve forward references: GroupSchema.students uses "UserSchema" which lives in auth.schemas.
# model_rebuild() looks up the name in the module's global namespace, so the import
# must use the exact name "UserSchema" (not an alias).
from src.auth.schemas import UserSchema  # noqa: E402, F401
GroupSchema.model_rebuild()

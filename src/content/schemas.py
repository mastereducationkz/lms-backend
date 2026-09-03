from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List, Union


class QuestionOption(BaseModel):
    id: str
    text: str
    is_correct: bool = False


class MatchingPair(BaseModel):
    left: str
    right: str


class QuizQuestion(BaseModel):
    id: str
    assignment_id: str = ""
    question_text: str
    question_type: str
    options: Optional[List[QuestionOption]] = None
    correct_answer: Union[str, List[str]] = ""
    points: int = 1
    order_index: int = 0
    media_url: Optional[str] = None
    media_type: Optional[str] = None
    expected_length: Optional[int] = None
    keywords: Optional[List[str]] = None
    matching_pairs: Optional[List[MatchingPair]] = None
    # SAT Checkpoints: easy | medium | hard (5/5/5 per unit, ТЗ §7). Optional everywhere else.
    difficulty: Optional[str] = None


class QuizData(BaseModel):
    title: str
    questions: List[QuizQuestion]
    time_limit_minutes: Optional[int] = None
    max_score: Optional[int] = None


class TaskItem(BaseModel):
    """Individual task within a multi-task assignment"""
    id: str
    task_type: str
    title: str
    description: Optional[str] = None
    order_index: int
    points: int = 10
    content: dict


class MultiTaskContent(BaseModel):
    """Content structure for multi-task assignments"""
    tasks: List[TaskItem]
    total_points: int
    instructions: Optional[str] = None


class FlashcardItem(BaseModel):
    id: str
    front_text: str
    back_text: str
    front_image_url: Optional[str] = None
    back_image_url: Optional[str] = None
    difficulty: str = "normal"
    tags: Optional[List[str]] = None
    order_index: int = 0


class FlashcardSet(BaseModel):
    title: str
    description: Optional[str] = None
    cards: List[FlashcardItem]
    study_mode: str = "sequential"
    auto_flip: bool = False
    show_progress: bool = True


class FavoriteFlashcardSchema(BaseModel):
    id: int
    user_id: int
    step_id: Optional[int] = None
    flashcard_id: str
    lesson_id: Optional[int] = None
    course_id: Optional[int] = None
    flashcard_data: str
    created_at: datetime

    class Config:
        from_attributes = True


class FavoriteFlashcardCreateSchema(BaseModel):
    step_id: Optional[int] = None
    flashcard_id: str
    lesson_id: Optional[int] = None
    course_id: Optional[int] = None
    flashcard_data: str


class FavoriteStepCreateSchema(BaseModel):
    step_id: int


class FavoriteStepItemSchema(BaseModel):
    id: int
    step_id: int
    lesson_id: int
    course_id: int
    course_title: str
    lesson_title: str
    order_index: int
    step_title: str
    content_type: str
    created_at: Optional[datetime] = None

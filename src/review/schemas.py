"""Response models for review mode.

Lists are never null — an empty list beats a null the frontend has to guard.
`answers` is the stored blob verbatim; the backend does not parse it.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class ReviewQuizStep(BaseModel):
    step_id: int
    title: str
    question_count: int
    submitted_count: int


class ReviewUnit(BaseModel):
    lesson_id: int
    title: str
    completed_count: int = 0
    quizzes: List[ReviewQuizStep] = []


class ReviewQuizzesResponse(BaseModel):
    course_id: int
    group_id: int
    roster_count: int
    units: List[ReviewUnit] = []


class ReviewStepInfo(BaseModel):
    step_id: int
    title: str
    lesson_id: int
    lesson_title: str
    course_id: int
    content: Dict[str, Any] = {}


class ReviewStudentRef(BaseModel):
    student_id: int
    full_name: str


class ReviewAttempt(BaseModel):
    student_id: int
    attempt_id: int
    correct_answers: int
    total_questions: int
    score_percentage: float
    time_spent_seconds: Optional[int] = None
    completed_at: Optional[datetime] = None
    answers: Optional[str] = None


class ReviewSessionResponse(BaseModel):
    step: ReviewStepInfo
    roster: List[ReviewStudentRef] = []
    attempts: List[ReviewAttempt] = []
    not_submitted: List[ReviewStudentRef] = []

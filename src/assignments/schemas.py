from pydantic import BaseModel, field_validator
from datetime import datetime, date, timezone
from typing import Optional, List, Dict
import json


class AssignmentSchema(BaseModel):
    id: int
    lesson_id: Optional[int] = None
    group_id: Optional[int] = None
    group_name: Optional[str] = None
    event_id: Optional[int] = None
    lesson_number: Optional[int] = None
    title: str
    description: Optional[str] = None
    assignment_type: str
    content: dict
    max_score: int
    time_limit_minutes: Optional[int] = None
    due_date: Optional[datetime] = None
    event_start_datetime: Optional[datetime] = None
    file_url: Optional[str] = None
    allowed_file_types: Optional[List[str]] = None
    max_file_size_mb: int = 10
    is_active: bool
    is_hidden: Optional[bool] = False
    late_penalty_enabled: Optional[bool] = False
    late_penalty_multiplier: Optional[float] = 0.6
    created_at: datetime

    @field_validator('content', mode='before')
    @classmethod
    def parse_content(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return {}
        return v

    @field_validator('due_date', 'event_start_datetime', 'created_at', mode='after')
    @classmethod
    def ensure_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    class Config:
        from_attributes = True


class AssignmentCreateSchema(BaseModel):
    title: str
    description: Optional[str] = None
    assignment_type: str
    content: dict
    correct_answers: Optional[dict] = None
    max_score: int = 100
    time_limit_minutes: Optional[int] = None
    due_date: Optional[datetime] = None
    group_id: Optional[int] = None
    group_ids: Optional[List[int]] = None
    event_id: Optional[int] = None
    event_mapping: Optional[Dict[int, int]] = None
    lesson_number_mapping: Optional[Dict[int, int]] = None
    due_date_mapping: Optional[Dict[int, datetime]] = None
    allowed_file_types: Optional[List[str]] = None
    max_file_size_mb: int = 10
    late_penalty_enabled: bool = False
    late_penalty_multiplier: float = 0.6

    @field_validator('content')
    @classmethod
    def validate_multi_task_content(cls, v, info):
        if info.data.get('assignment_type') == 'multi_task':
            if 'tasks' not in v:
                raise ValueError("multi_task assignment must have 'tasks' array in content")
            if not isinstance(v['tasks'], list) or len(v['tasks']) == 0:
                raise ValueError("multi_task assignment must have at least one task")
        return v


class AssignmentSubmissionSchema(BaseModel):
    id: int
    assignment_id: int
    user_id: int
    user_name: Optional[str] = None
    answers: dict
    file_url: Optional[str] = None
    submitted_file_name: Optional[str] = None
    score: Optional[int] = None
    max_score: int
    is_graded: bool
    is_hidden: Optional[bool] = False
    feedback: Optional[str] = None
    graded_by: Optional[int] = None
    grader_name: Optional[str] = None
    submitted_at: datetime
    is_late: Optional[bool] = False
    graded_at: Optional[datetime] = None

    @field_validator('answers', mode='before')
    @classmethod
    def parse_answers(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return {}
        return v

    class Config:
        from_attributes = True


class GradeSubmissionSchema(BaseModel):
    score: int
    feedback: Optional[str] = None


class SubmitAssignmentSchema(BaseModel):
    answers: dict
    file_url: Optional[str] = None
    submitted_file_name: Optional[str] = None


class AssignmentExtensionSchema(BaseModel):
    id: int
    assignment_id: int
    student_id: int
    student_name: Optional[str] = None
    extended_deadline: datetime
    reason: Optional[str] = None
    granted_by: int
    granter_name: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class GrantExtensionSchema(BaseModel):
    student_id: int
    extended_deadline: datetime
    reason: Optional[str] = None


class GroupAssignmentSchema(BaseModel):
    id: int
    group_id: int
    assignment_id: int
    lesson_schedule_id: Optional[int] = None
    assigned_at: datetime
    due_date: Optional[datetime] = None
    is_active: bool

    class Config:
        from_attributes = True

    @field_validator('assigned_at', 'due_date', mode='after')
    @classmethod
    def ensure_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


class AssignmentZeroSubmissionSchema(BaseModel):
    id: int
    user_id: int
    full_name: str
    phone_number: str
    parent_phone_number: str
    telegram_id: str
    email: str
    college_board_email: str
    college_board_password: str
    birthday_date: date
    city: str
    school_type: str
    group_name: str
    sat_target_date: str
    sat_planned_test_date: Optional[date] = None
    sat_result_score: Optional[str] = None
    sat_result_test_date: Optional[date] = None
    has_passed_sat_before: bool
    previous_sat_score: Optional[str] = None
    recent_practice_test_score: str
    bluebook_practice_test_5_score: str
    screenshot_url: Optional[str] = None
    grammar_punctuation: Optional[int] = None
    grammar_noun_clauses: Optional[int] = None
    grammar_relative_clauses: Optional[int] = None
    grammar_verb_forms: Optional[int] = None
    grammar_comparisons: Optional[int] = None
    grammar_transitions: Optional[int] = None
    grammar_synthesis: Optional[int] = None
    reading_word_in_context: Optional[int] = None
    reading_text_structure: Optional[int] = None
    reading_cross_text: Optional[int] = None
    reading_central_ideas: Optional[int] = None
    reading_inferences: Optional[int] = None
    passages_literary: Optional[int] = None
    passages_social_science: Optional[int] = None
    passages_humanities: Optional[int] = None
    passages_science: Optional[int] = None
    passages_poetry: Optional[int] = None
    math_topics: Optional[List[str]] = None
    ielts_target_date: Optional[str] = None
    ielts_planned_test_date: Optional[date] = None
    ielts_result_score: Optional[str] = None
    ielts_result_test_date: Optional[date] = None
    ielts_last_date_prompted_at: Optional[datetime] = None
    has_passed_ielts_before: Optional[bool] = False
    previous_ielts_score: Optional[str] = None
    ielts_target_score: Optional[str] = None
    ielts_listening_main_idea: Optional[int] = None
    ielts_listening_details: Optional[int] = None
    ielts_listening_opinion: Optional[int] = None
    ielts_listening_accents: Optional[int] = None
    ielts_reading_skimming: Optional[int] = None
    ielts_reading_scanning: Optional[int] = None
    ielts_reading_vocabulary: Optional[int] = None
    ielts_reading_inference: Optional[int] = None
    ielts_reading_matching: Optional[int] = None
    ielts_writing_task1_graphs: Optional[int] = None
    ielts_writing_task1_process: Optional[int] = None
    ielts_writing_task2_structure: Optional[int] = None
    ielts_writing_task2_arguments: Optional[int] = None
    ielts_writing_grammar: Optional[int] = None
    ielts_writing_vocabulary: Optional[int] = None
    ielts_speaking_fluency: Optional[int] = None
    ielts_speaking_vocabulary: Optional[int] = None
    ielts_speaking_grammar: Optional[int] = None
    ielts_speaking_pronunciation: Optional[int] = None
    ielts_speaking_part2: Optional[int] = None
    ielts_speaking_part3: Optional[int] = None
    ielts_weak_topics: Optional[List[str]] = None
    additional_comments: Optional[str] = None
    is_draft: bool = False
    last_saved_step: int = 1
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AssignmentZeroSubmitSchema(BaseModel):
    full_name: str
    phone_number: str
    parent_phone_number: str
    telegram_id: str
    email: str
    college_board_email: str
    college_board_password: str
    birthday_date: date
    city: str
    school_type: str
    group_name: str
    sat_target_date: str
    sat_planned_test_date: Optional[date] = None
    sat_result_score: Optional[str] = None
    sat_result_test_date: Optional[date] = None
    has_passed_sat_before: bool = False
    previous_sat_score: Optional[str] = None
    recent_practice_test_score: str
    bluebook_practice_test_5_score: str
    screenshot_url: Optional[str] = None
    grammar_punctuation: Optional[int] = None
    grammar_noun_clauses: Optional[int] = None
    grammar_relative_clauses: Optional[int] = None
    grammar_verb_forms: Optional[int] = None
    grammar_comparisons: Optional[int] = None
    grammar_transitions: Optional[int] = None
    grammar_synthesis: Optional[int] = None
    reading_word_in_context: Optional[int] = None
    reading_text_structure: Optional[int] = None
    reading_cross_text: Optional[int] = None
    reading_central_ideas: Optional[int] = None
    reading_inferences: Optional[int] = None
    passages_literary: Optional[int] = None
    passages_social_science: Optional[int] = None
    passages_humanities: Optional[int] = None
    passages_science: Optional[int] = None
    passages_poetry: Optional[int] = None
    math_topics: Optional[List[str]] = None
    ielts_target_date: Optional[str] = None
    ielts_planned_test_date: Optional[date] = None
    ielts_result_score: Optional[str] = None
    ielts_result_test_date: Optional[date] = None
    ielts_last_date_prompted_at: Optional[datetime] = None
    has_passed_ielts_before: Optional[bool] = False
    previous_ielts_score: Optional[str] = None
    ielts_target_score: Optional[str] = None
    ielts_listening_main_idea: Optional[int] = None
    ielts_listening_details: Optional[int] = None
    ielts_listening_opinion: Optional[int] = None
    ielts_listening_accents: Optional[int] = None
    ielts_reading_skimming: Optional[int] = None
    ielts_reading_scanning: Optional[int] = None
    ielts_reading_vocabulary: Optional[int] = None
    ielts_reading_inference: Optional[int] = None
    ielts_reading_matching: Optional[int] = None
    ielts_writing_task1_graphs: Optional[int] = None
    ielts_writing_task1_process: Optional[int] = None
    ielts_writing_task2_structure: Optional[int] = None
    ielts_writing_task2_arguments: Optional[int] = None
    ielts_writing_grammar: Optional[int] = None
    ielts_writing_vocabulary: Optional[int] = None
    ielts_speaking_fluency: Optional[int] = None
    ielts_speaking_vocabulary: Optional[int] = None
    ielts_speaking_grammar: Optional[int] = None
    ielts_speaking_pronunciation: Optional[int] = None
    ielts_speaking_part2: Optional[int] = None
    ielts_speaking_part3: Optional[int] = None
    ielts_weak_topics: Optional[List[str]] = None
    additional_comments: Optional[str] = None


class AssignmentZeroSaveProgressSchema(BaseModel):
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    parent_phone_number: Optional[str] = None
    telegram_id: Optional[str] = None
    email: Optional[str] = None
    college_board_email: Optional[str] = None
    college_board_password: Optional[str] = None
    birthday_date: Optional[date] = None
    city: Optional[str] = None
    school_type: Optional[str] = None
    group_name: Optional[str] = None
    sat_target_date: Optional[str] = None
    sat_planned_test_date: Optional[date] = None
    sat_result_score: Optional[str] = None
    sat_result_test_date: Optional[date] = None
    has_passed_sat_before: Optional[bool] = None
    previous_sat_score: Optional[str] = None
    recent_practice_test_score: Optional[str] = None
    bluebook_practice_test_5_score: Optional[str] = None
    screenshot_url: Optional[str] = None
    grammar_punctuation: Optional[int] = None
    grammar_noun_clauses: Optional[int] = None
    grammar_relative_clauses: Optional[int] = None
    grammar_verb_forms: Optional[int] = None
    grammar_comparisons: Optional[int] = None
    grammar_transitions: Optional[int] = None
    grammar_synthesis: Optional[int] = None
    reading_word_in_context: Optional[int] = None
    reading_text_structure: Optional[int] = None
    reading_cross_text: Optional[int] = None
    reading_central_ideas: Optional[int] = None
    reading_inferences: Optional[int] = None
    passages_literary: Optional[int] = None
    passages_social_science: Optional[int] = None
    passages_humanities: Optional[int] = None
    passages_science: Optional[int] = None
    passages_poetry: Optional[int] = None
    math_topics: Optional[List[str]] = None
    ielts_target_date: Optional[str] = None
    ielts_planned_test_date: Optional[date] = None
    ielts_result_score: Optional[str] = None
    ielts_result_test_date: Optional[date] = None
    ielts_last_date_prompted_at: Optional[datetime] = None
    has_passed_ielts_before: Optional[bool] = None
    previous_ielts_score: Optional[str] = None
    ielts_target_score: Optional[str] = None
    ielts_listening_main_idea: Optional[int] = None
    ielts_listening_details: Optional[int] = None
    ielts_listening_opinion: Optional[int] = None
    ielts_listening_accents: Optional[int] = None
    ielts_reading_skimming: Optional[int] = None
    ielts_reading_scanning: Optional[int] = None
    ielts_reading_vocabulary: Optional[int] = None
    ielts_reading_inference: Optional[int] = None
    ielts_reading_matching: Optional[int] = None
    ielts_writing_task1_graphs: Optional[int] = None
    ielts_writing_task1_process: Optional[int] = None
    ielts_writing_task2_structure: Optional[int] = None
    ielts_writing_task2_arguments: Optional[int] = None
    ielts_writing_grammar: Optional[int] = None
    ielts_writing_vocabulary: Optional[int] = None
    ielts_speaking_fluency: Optional[int] = None
    ielts_speaking_vocabulary: Optional[int] = None
    ielts_speaking_grammar: Optional[int] = None
    ielts_speaking_pronunciation: Optional[int] = None
    ielts_speaking_part2: Optional[int] = None
    ielts_speaking_part3: Optional[int] = None
    ielts_weak_topics: Optional[List[str]] = None
    additional_comments: Optional[str] = None
    last_saved_step: Optional[int] = None


class AssignmentZeroPlannedDateUpdateSchema(BaseModel):
    exam_type: str
    planned_test_date: date


class AssignmentZeroExamResultUpdateSchema(BaseModel):
    exam_type: str
    result_score: str
    result_test_date: date


class DraftUpsertSchema(BaseModel):
    answers: Optional[dict] = None
    file_url: Optional[str] = None
    submitted_file_name: Optional[str] = None


class DraftSchema(BaseModel):
    answers: Optional[dict] = None
    file_url: Optional[str] = None
    submitted_file_name: Optional[str] = None
    updated_at: Optional[datetime] = None


class HomeworkUpdateSchema(BaseModel):
    """One item in the dashboard "homework updates" feed."""
    kind: str  # graded | assigned | due_soon
    assignment_id: int
    submission_id: Optional[int] = None
    title: str
    score: Optional[int] = None
    max_score: Optional[int] = None
    due_date: Optional[datetime] = None
    timestamp: datetime  # the kind's relevant date; primary sort key + relative label
    student_id: int
    student_name: Optional[str] = None

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() + "Z" if v else None
        }

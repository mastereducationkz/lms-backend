from sqlalchemy import Column, String, Integer, Float, DateTime, Date, Boolean, ForeignKey, Text, UniqueConstraint, ARRAY, JSON, Index
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from src.models.base import Base


class Assignment(Base):
    __tablename__ = "assignments"
    id = Column(Integer, primary_key=True, index=True)
    lesson_id = Column(Integer, ForeignKey("lessons.id", ondelete="CASCADE"), nullable=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="SET NULL"), nullable=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True)
    lesson_number = Column(Integer, nullable=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    assignment_type = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    correct_answers = Column(Text, nullable=True)
    max_score = Column(Integer, default=100)
    time_limit_minutes = Column(Integer, nullable=True)
    due_date = Column(DateTime, nullable=True)
    file_url = Column(String, nullable=True)
    allowed_file_types = Column(ARRAY(String), nullable=True)
    max_file_size_mb = Column(Integer, default=10)
    is_active = Column(Boolean, default=True)
    is_hidden = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    late_penalty_enabled = Column(Boolean, default=False)
    late_penalty_multiplier = Column(Float, default=0.6)

    lesson = relationship("Lesson", back_populates="assignments")
    group = relationship("Group")
    event = relationship("Event")
    submissions = relationship("AssignmentSubmission", back_populates="assignment", cascade="all, delete-orphan")


class AssignmentSubmission(Base):
    __tablename__ = "assignment_submissions"
    id = Column(Integer, primary_key=True, index=True)
    assignment_id = Column(Integer, ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    answers = Column(Text, nullable=False)
    file_url = Column(String, nullable=True)
    submitted_file_name = Column(String, nullable=True)
    score = Column(Integer, nullable=True)
    max_score = Column(Integer, nullable=False)
    is_graded = Column(Boolean, default=False)
    is_hidden = Column(Boolean, default=False)
    seen_by_student = Column(Boolean, default=False)
    feedback = Column(Text, nullable=True)
    graded_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    submitted_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_late = Column(Boolean, default=False)
    graded_at = Column(DateTime, nullable=True)

    assignment = relationship("Assignment", back_populates="submissions")
    user = relationship("UserInDB", foreign_keys=[user_id], back_populates="assignment_submissions")
    grader = relationship("UserInDB", foreign_keys=[graded_by])

    __table_args__ = (
        Index('idx_assignment_submissions_user_assignment', 'user_id', 'assignment_id'),
        Index('idx_assignment_submissions_assignment_submitted', 'assignment_id', 'submitted_at'),
        Index('idx_assignment_submissions_user_graded', 'user_id', 'is_graded'),
    )


class AssignmentLinkedLesson(Base):
    """Denormalized table for fast lookup of lessons linked to assignments"""
    __tablename__ = "assignment_linked_lessons"
    id = Column(Integer, primary_key=True, index=True)
    assignment_id = Column(Integer, ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False, index=True)
    lesson_id = Column(Integer, ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint('assignment_id', 'lesson_id', name='uq_assignment_lesson'),
    )

    assignment = relationship("Assignment", backref="linked_lessons_rel")
    lesson = relationship("Lesson")


class AssignmentExtension(Base):
    """Individual deadline extensions for students"""
    __tablename__ = "assignment_extensions"
    id = Column(Integer, primary_key=True, index=True)
    assignment_id = Column(Integer, ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    extended_deadline = Column(DateTime, nullable=False)
    reason = Column(Text, nullable=True)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint('assignment_id', 'student_id', name='uq_assignment_student_extension'),
    )

    assignment = relationship("Assignment", backref="extensions")
    student = relationship("UserInDB", foreign_keys=[student_id], backref="assignment_extensions")
    granter = relationship("UserInDB", foreign_keys=[granted_by])


class GroupAssignment(Base):
    __tablename__ = "group_assignments"
    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    assignment_id = Column(Integer, ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False)
    lesson_schedule_id = Column(Integer, ForeignKey("lesson_schedules.id", ondelete="SET NULL"), nullable=True)
    assigned_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    due_date = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    group = relationship("Group")
    assignment = relationship("Assignment")
    lesson_schedule = relationship("LessonSchedule")


class AssignmentZeroSubmission(Base):
    """Stores self-assessment questionnaire data for new students"""
    __tablename__ = "assignment_zero_submissions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    full_name = Column(String, nullable=False)
    phone_number = Column(String, nullable=False)
    parent_phone_number = Column(String, nullable=False)
    telegram_id = Column(String, nullable=False)
    email = Column(String, nullable=False)
    college_board_email = Column(String, nullable=False)
    college_board_password = Column(String, nullable=False)
    birthday_date = Column(Date, nullable=False)
    city = Column(String, nullable=False)
    school_type = Column(String, nullable=False)
    group_name = Column(String, nullable=False)
    sat_target_date = Column(String, nullable=False)
    sat_planned_test_date = Column(Date, nullable=True)
    sat_result_score = Column(String, nullable=True)
    sat_result_test_date = Column(Date, nullable=True)
    has_passed_sat_before = Column(Boolean, default=False)
    previous_sat_score = Column(String, nullable=True)
    recent_practice_test_score = Column(String, nullable=False)
    bluebook_practice_test_5_score = Column(String, nullable=False)
    screenshot_url = Column(String, nullable=True)
    grammar_punctuation = Column(Integer, nullable=True)
    grammar_noun_clauses = Column(Integer, nullable=True)
    grammar_relative_clauses = Column(Integer, nullable=True)
    grammar_verb_forms = Column(Integer, nullable=True)
    grammar_comparisons = Column(Integer, nullable=True)
    grammar_transitions = Column(Integer, nullable=True)
    grammar_synthesis = Column(Integer, nullable=True)
    reading_word_in_context = Column(Integer, nullable=True)
    reading_text_structure = Column(Integer, nullable=True)
    reading_cross_text = Column(Integer, nullable=True)
    reading_central_ideas = Column(Integer, nullable=True)
    reading_inferences = Column(Integer, nullable=True)
    passages_literary = Column(Integer, nullable=True)
    passages_social_science = Column(Integer, nullable=True)
    passages_humanities = Column(Integer, nullable=True)
    passages_science = Column(Integer, nullable=True)
    passages_poetry = Column(Integer, nullable=True)
    math_topics = Column(JSON, nullable=True)
    ielts_target_date = Column(String, nullable=True)
    ielts_planned_test_date = Column(Date, nullable=True)
    ielts_result_score = Column(String, nullable=True)
    ielts_result_test_date = Column(Date, nullable=True)
    ielts_last_date_prompted_at = Column(DateTime, nullable=True)
    has_passed_ielts_before = Column(Boolean, default=False)
    previous_ielts_score = Column(String, nullable=True)
    ielts_target_score = Column(String, nullable=True)
    ielts_listening_main_idea = Column(Integer, nullable=True)
    ielts_listening_details = Column(Integer, nullable=True)
    ielts_listening_opinion = Column(Integer, nullable=True)
    ielts_listening_accents = Column(Integer, nullable=True)
    ielts_reading_skimming = Column(Integer, nullable=True)
    ielts_reading_scanning = Column(Integer, nullable=True)
    ielts_reading_vocabulary = Column(Integer, nullable=True)
    ielts_reading_inference = Column(Integer, nullable=True)
    ielts_reading_matching = Column(Integer, nullable=True)
    ielts_writing_task1_graphs = Column(Integer, nullable=True)
    ielts_writing_task1_process = Column(Integer, nullable=True)
    ielts_writing_task2_structure = Column(Integer, nullable=True)
    ielts_writing_task2_arguments = Column(Integer, nullable=True)
    ielts_writing_grammar = Column(Integer, nullable=True)
    ielts_writing_vocabulary = Column(Integer, nullable=True)
    ielts_speaking_fluency = Column(Integer, nullable=True)
    ielts_speaking_vocabulary = Column(Integer, nullable=True)
    ielts_speaking_grammar = Column(Integer, nullable=True)
    ielts_speaking_pronunciation = Column(Integer, nullable=True)
    ielts_speaking_part2 = Column(Integer, nullable=True)
    ielts_speaking_part3 = Column(Integer, nullable=True)
    ielts_weak_topics = Column(JSON, nullable=True)
    additional_comments = Column(Text, nullable=True)
    is_draft = Column(Boolean, default=True)
    last_saved_step = Column(Integer, default=1)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    user = relationship("UserInDB", backref="assignment_zero_submission")

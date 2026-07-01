from sqlalchemy import Column, String, Integer, DateTime, Date, Boolean, ForeignKey, Text, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime, timezone

from src.models.base import Base


class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    curator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = Column(Boolean, default=True)
    is_special = Column(Boolean, default=False, nullable=False)
    is_over = Column(Boolean, default=False, nullable=False)
    schedule_config = Column(JSONB, nullable=True)
    # "group" = классическая группа; "individual" = индивидуальная
    group_type = Column(String(32), nullable=False, default="group")
    # SAT / IELTS / General English / NUET — для фильтрации и поиска
    program_type = Column(String(32), nullable=False, default="general_english")

    teacher = relationship("UserInDB", foreign_keys=[teacher_id], post_update=True)
    curator = relationship("UserInDB", foreign_keys=[curator_id], post_update=True)
    students = relationship("GroupStudent", back_populates="group", cascade="all, delete-orphan")


class GroupStudent(Base):
    __tablename__ = "group_students"
    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    student_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    group = relationship("Group", back_populates="students")
    student = relationship("UserInDB", back_populates="groups")

    __table_args__ = (
        UniqueConstraint('group_id', 'student_id', name='uq_group_student'),
        Index('idx_group_students_group_id', 'group_id'),
        Index('idx_group_students_student_id', 'student_id'),
    )


class Step(Base):
    __tablename__ = "steps"
    id = Column(Integer, primary_key=True, index=True)
    lesson_id = Column(Integer, ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False)
    title = Column(String, nullable=False)
    content_type = Column(String, nullable=False, default="text")
    video_url = Column(String, nullable=True)
    content_text = Column(Text, nullable=True)
    original_image_url = Column(String, nullable=True)
    attachments = Column(Text, nullable=True)
    order_index = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    content_hash = Column(String(64), nullable=True)
    is_optional = Column(Boolean, default=False)
    # Self-hosted HLS variants of the YouTube video (see VideoIngestJob). When set,
    # players stream these instead of embedding YouTube; video_url stays as fallback.
    hls_url = Column(String, nullable=True)       # primary/RU video → /uploads/videos/<id>/ru/master.m3u8
    hls_url_en = Column(String, nullable=True)    # EN variant (parsed from content_text metadata)
    video_status = Column(String(16), nullable=True)  # pending|processing|ready|failed (tracks primary video)

    lesson = relationship("Lesson", back_populates="steps")
    favorite_flashcards = relationship("FavoriteFlashcard", back_populates="step", cascade="all, delete-orphan", passive_deletes=True)


class VideoIngestJob(Base):
    """Queue row for downloading a step's YouTube video and transcoding it to HLS.

    Processed by the video ingest worker in the scheduler container. One row per
    (step, language). The (step_id, lang) unique constraint makes enqueue idempotent.
    """
    __tablename__ = "video_ingest_jobs"
    id = Column(Integer, primary_key=True, index=True)
    step_id = Column(Integer, ForeignKey("steps.id", ondelete="CASCADE"), nullable=False, index=True)
    lang = Column(String(8), nullable=False, default="ru")   # ru | en
    youtube_url = Column(String, nullable=False)
    status = Column(String(16), nullable=False, default="pending")  # pending|processing|done|failed
    attempts = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    step = relationship("Step")

    __table_args__ = (
        UniqueConstraint('step_id', 'lang', name='uq_video_ingest_step_lang'),
        Index('idx_video_ingest_status', 'status'),
    )


class Course(Base):
    __tablename__ = "courses"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    cover_image_url = Column(String, nullable=True)
    teacher_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    estimated_duration_minutes = Column(Integer, default=0)
    is_active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    # "all" = all modules open; "weekly" = modules unlock by week based on group start_date
    release_schedule = Column(String(20), default="all", nullable=False)
    # sat | ielts | general_english | nuet
    course_type = Column(String(32), nullable=False, default="general_english")

    teacher = relationship("UserInDB", back_populates="created_courses")
    modules = relationship("Module", back_populates="course", cascade="all, delete-orphan", order_by="Module.order_index")
    enrollments = relationship("Enrollment", back_populates="course")
    group_access = relationship("CourseGroupAccess", back_populates="course", cascade="all, delete-orphan")
    teacher_access = relationship("CourseTeacherAccess", back_populates="course", cascade="all, delete-orphan")
    head_teachers = relationship("UserInDB", secondary="course_head_teachers", back_populates="managed_courses")


class CourseHeadTeacher(Base):
    """Association table for Head Teachers managing Courses (M2M)."""
    __tablename__ = "course_head_teachers"
    course_id = Column(Integer, ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True)
    head_teacher_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class CourseGroupAccess(Base):
    __tablename__ = "course_group_access"
    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    granted_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = Column(Boolean, default=True)
    # For special groups: max lessons open per module (first N by order in each module; NULL = no cap)
    max_open_lessons = Column(Integer, nullable=True)

    course = relationship("Course", back_populates="group_access")
    group = relationship("Group")
    granted_by_user = relationship("UserInDB", foreign_keys=[granted_by])


class CourseTeacherAccess(Base):
    """Direct teacher access to a course (without requiring a group)."""
    __tablename__ = "course_teacher_access"
    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    teacher_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    granted_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = Column(Boolean, default=True)

    course = relationship("Course", back_populates="teacher_access")
    teacher = relationship("UserInDB", foreign_keys=[teacher_id])
    granted_by_user = relationship("UserInDB", foreign_keys=[granted_by])

    __table_args__ = (
        UniqueConstraint('course_id', 'teacher_id', name='uq_course_teacher_access'),
    )


class Module(Base):
    __tablename__ = "modules"
    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    order_index = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # For weekly release: which week (1-based) this module unlocks; if null, falls back to order_index
    week_number = Column(Integer, nullable=True)

    course = relationship("Course", back_populates="modules")
    lessons = relationship("Lesson", back_populates="module", cascade="all, delete-orphan", order_by="Lesson.order_index")


class Lesson(Base):
    __tablename__ = "lessons"
    id = Column(Integer, primary_key=True, index=True)
    module_id = Column(Integer, ForeignKey("modules.id"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    duration_minutes = Column(Integer, default=0)
    order_index = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    next_lesson_id = Column(Integer, ForeignKey("lessons.id"), nullable=True)
    is_initially_unlocked = Column(Boolean, default=False)

    module = relationship("Module", back_populates="lessons")
    materials = relationship("LessonMaterial", back_populates="lesson", cascade="all, delete-orphan")
    assignments = relationship("Assignment", back_populates="lesson", cascade="all, delete-orphan")
    steps = relationship("Step", back_populates="lesson", cascade="all, delete-orphan", order_by="Step.order_index")


class LessonMaterial(Base):
    __tablename__ = "lesson_materials"
    id = Column(Integer, primary_key=True, index=True)
    lesson_id = Column(Integer, ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False)
    title = Column(String, nullable=False)
    file_type = Column(String, nullable=False)
    file_url = Column(String, nullable=False)
    file_size_bytes = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    lesson = relationship("Lesson", back_populates="materials")


class Enrollment(Base):
    __tablename__ = "enrollments"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    enrolled_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)

    user = relationship("UserInDB", back_populates="enrollments")
    course = relationship("Course", back_populates="enrollments")

    __table_args__ = (
        Index('idx_enrollments_course_active', 'course_id', 'is_active'),
        Index('idx_enrollments_user_active', 'user_id', 'is_active'),
    )


class ManualLessonUnlock(Base):
    """Model for tracking units (lessons) manually unlocked by teachers for students or groups."""
    __tablename__ = "manual_lesson_unlocks"

    id = Column(Integer, primary_key=True, index=True)
    lesson_id = Column(Integer, ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=True, index=True)
    granted_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    lesson = relationship("Lesson")
    user = relationship("UserInDB", foreign_keys=[user_id])
    group = relationship("Group", foreign_keys=[group_id])
    granter = relationship("UserInDB", foreign_keys=[granted_by])

    __table_args__ = (
        UniqueConstraint('lesson_id', 'user_id', name='uq_manual_unlock_user'),
        UniqueConstraint('lesson_id', 'group_id', name='uq_manual_unlock_group'),
    )

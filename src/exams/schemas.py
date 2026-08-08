"""Pydantic schemas for the exam-results domain.

Two deliberate choices here:

1. **Totals are derived, never trusted.** SAT and Bluebook totals are computed from the
   validated section scores server-side. The reference spreadsheet defines
   ``Score = Verbal + Math`` for every row, and accepting a client-supplied total would
   let the UI (or a crafted request) record an internally inconsistent row.

2. **Response schemas are purpose-built and narrow.** They are NOT derived from
   ``AssignmentZeroSubmissionSchema``. That schema declares ``college_board_email`` and
   ``college_board_password`` and is inherited by ``CuratorUpcomingExamRow``, which is
   how credentials currently reach clients that never render them. Nothing in this
   module may reuse it.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ExamType = Literal["sat", "ielts", "nuet"]
ExamResultStatus = Literal["reported", "verified", "rejected"]
ExamResultSource = Literal["assignment_zero", "staff", "student"]

SAT_SECTION_MIN = 200
SAT_SECTION_MAX = 800
SAT_SECTION_STEP = 10

IELTS_BAND_MIN = Decimal("0")
IELTS_BAND_MAX = Decimal("9")

BLUEBOOK_MIN_TEST_NUMBER = 4
BLUEBOOK_MAX_TEST_NUMBER = 11


def _validate_sat_section(value: Optional[int], field_name: str) -> Optional[int]:
    if value is None:
        return None
    if not (SAT_SECTION_MIN <= value <= SAT_SECTION_MAX):
        raise ValueError(
            f"{field_name} must be between {SAT_SECTION_MIN} and {SAT_SECTION_MAX}"
        )
    if value % SAT_SECTION_STEP != 0:
        raise ValueError(f"{field_name} must be a multiple of {SAT_SECTION_STEP}")
    return value


def _validate_ielts_band(value: Optional[Decimal], field_name: str) -> Optional[Decimal]:
    if value is None:
        return None
    if not (IELTS_BAND_MIN <= value <= IELTS_BAND_MAX):
        raise ValueError(f"{field_name} must be between 0 and 9")
    # IELTS reports whole and half bands only.
    if (value * 2) % 1 != 0:
        raise ValueError(f"{field_name} must be a whole or half band (e.g. 6.5)")
    return value


class _ExamScoreFields(BaseModel):
    """Shared score shape + the per-exam-type consistency rules."""

    exam_type: ExamType
    test_date: date
    total_score: Optional[Decimal] = None

    verbal_score: Optional[int] = None
    math_score: Optional[int] = None

    listening_band: Optional[Decimal] = None
    reading_band: Optional[Decimal] = None
    writing_band: Optional[Decimal] = None
    speaking_band: Optional[Decimal] = None

    proof_url: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("verbal_score")
    @classmethod
    def _check_verbal(cls, v):
        return _validate_sat_section(v, "verbal_score")

    @field_validator("math_score")
    @classmethod
    def _check_math(cls, v):
        return _validate_sat_section(v, "math_score")

    @field_validator("listening_band")
    @classmethod
    def _check_listening(cls, v):
        return _validate_ielts_band(v, "listening_band")

    @field_validator("reading_band")
    @classmethod
    def _check_reading(cls, v):
        return _validate_ielts_band(v, "reading_band")

    @field_validator("writing_band")
    @classmethod
    def _check_writing(cls, v):
        return _validate_ielts_band(v, "writing_band")

    @field_validator("speaking_band")
    @classmethod
    def _check_speaking(cls, v):
        return _validate_ielts_band(v, "speaking_band")

    @field_validator("test_date")
    @classmethod
    def _not_in_the_future(cls, v: date):
        if v > date.today():
            raise ValueError("test_date cannot be in the future")
        return v

    @model_validator(mode="after")
    def _enforce_per_exam_shape(self):
        ielts_bands = (self.listening_band, self.reading_band,
                       self.writing_band, self.speaking_band)

        if self.exam_type == "sat":
            if any(b is not None for b in ielts_bands):
                raise ValueError("IELTS band fields are not valid for a SAT result")
            if self.verbal_score is None or self.math_score is None:
                raise ValueError("SAT results require both verbal_score and math_score")
            # Derived, never trusted from the client.
            object.__setattr__(self, "total_score",
                               Decimal(self.verbal_score + self.math_score))

        elif self.exam_type == "ielts":
            if self.verbal_score is not None or self.math_score is not None:
                raise ValueError("SAT section fields are not valid for an IELTS result")
            if self.total_score is None:
                raise ValueError("IELTS results require an overall total_score")
            _validate_ielts_band(self.total_score, "total_score")

        else:  # nuet - scoring model not yet defined; require only a total
            if self.total_score is None:
                raise ValueError("NUET results require a total_score")

        return self


class ExamResultCreate(_ExamScoreFields):
    student_id: int


class ExamResultUpdate(BaseModel):
    """Partial update. Score corrections go through a new attempt, not an edit,
    so that history survives; this covers verification and annotation only."""

    status: Optional[ExamResultStatus] = None
    notes: Optional[str] = None
    proof_url: Optional[str] = None
    is_superseded: Optional[bool] = None


class ExamResultOut(BaseModel):
    """Purpose-built read model. Contains no contact details and no credentials."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    student_id: int
    exam_type: ExamType
    test_date: date
    total_score: Decimal
    verbal_score: Optional[int] = None
    math_score: Optional[int] = None
    listening_band: Optional[Decimal] = None
    reading_band: Optional[Decimal] = None
    writing_band: Optional[Decimal] = None
    speaking_band: Optional[Decimal] = None
    status: ExamResultStatus
    source: ExamResultSource
    has_proof: bool = False
    recorded_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    is_superseded: bool = False
    notes: Optional[str] = None


class StudentContactOut(BaseModel):
    """Contact block for authorized staff grids and exports.

    Every field is Optional and rendered as absent when unknown. Missing contact data
    is reported honestly rather than synthesized - `users` has no phone or telegram
    column at all, so those come solely from Assignment Zero, and a parent's full name
    exists only where a linked parent account does.
    """

    student_id: int
    full_name: str
    student_phone: Optional[str] = None
    telegram_tag: Optional[str] = None
    parent_full_name: Optional[str] = None
    parent_phone: Optional[str] = None


class ExamResultRow(BaseModel):
    """One row of the authorized exam-results grid."""

    student: StudentContactOut
    group_id: Optional[int] = None
    group_name: Optional[str] = None
    planned_test_date: Optional[date] = None
    result: Optional[ExamResultOut] = None


class BluebookResultInput(BaseModel):
    """What a student submits for a Bluebook task, before it is projected.

    ``screenshot_url`` is optional at this layer because the Assignment Zero baseline
    backfill has no screenshot. Requiring evidence on a live homework submission is
    enforced in the submission flow, not here.
    """

    test_number: int = Field(..., ge=BLUEBOOK_MIN_TEST_NUMBER, le=BLUEBOOK_MAX_TEST_NUMBER)
    verbal_score: int
    math_score: int
    screenshot_url: Optional[str] = None
    taken_at: Optional[date] = None
    notes: Optional[str] = None

    @field_validator("verbal_score")
    @classmethod
    def _check_verbal(cls, v):
        return _validate_sat_section(v, "verbal_score")

    @field_validator("math_score")
    @classmethod
    def _check_math(cls, v):
        return _validate_sat_section(v, "math_score")

    @property
    def total_score(self) -> int:
        """Always Verbal + Math, matching the reference spreadsheet."""
        return self.verbal_score + self.math_score


class BluebookCell(BaseModel):
    """One Verbal | Math | Score cell group in the grid.

    ``state`` distinguishes the three reasons a cell can be empty. Collapsing them into
    a blank hides whether the gap is the student's (assigned, not submitted) or the
    teacher's (never assigned).
    """

    state: Literal["submitted", "not_submitted", "not_assigned"] = "not_assigned"
    verbal_score: Optional[int] = None
    math_score: Optional[int] = None
    total_score: Optional[int] = None
    taken_at: Optional[date] = None
    screenshot_url: Optional[str] = None
    source: Optional[str] = None
    # Change vs the previous non-empty column for this student: up | down | same.
    # Paired with an arrow in the UI so the signal is never colour-only.
    delta: Optional[int] = None
    trend: Optional[Literal["up", "down", "same"]] = None


class BluebookColumn(BaseModel):
    """One Bluebook test. Tests 4-11 are always present, assigned or not."""

    key: str
    label: str
    test_number: Optional[int] = None
    assignment_id: Optional[int] = None
    due_date: Optional[date] = None
    week_number: Optional[int] = None
    is_baseline: bool = False
    # False when no Bluebook homework exists for this test in this group.
    is_assigned: bool = False


class BluebookGroupStats(BaseModel):
    """Whole-group aggregates shown above the grid."""

    student_count: int
    tests_assigned: int
    tests_available: int
    submitted_count: int
    expected_count: int
    completion_rate: float
    average_latest_total: Optional[float] = None
    median_latest_total: Optional[float] = None
    average_best_total: Optional[float] = None
    highest_total: Optional[int] = None
    lowest_latest_total: Optional[int] = None
    average_improvement: Optional[float] = None
    improved_count: int = 0
    declined_count: int = 0
    students_with_no_results: int = 0


class BluebookColumnStats(BaseModel):
    submitted_count: int
    expected_count: int
    completion_rate: float
    mean_total: Optional[float] = None
    median_total: Optional[float] = None
    mean_verbal: Optional[float] = None
    mean_math: Optional[float] = None


class BluebookStudentRow(BaseModel):
    student_id: int
    full_name: str
    display_id: Optional[str] = None
    email: Optional[str] = None
    cells: dict
    # Per-student statistics. submitted/assigned exclude the Assignment Zero baseline,
    # which is diagnostic data rather than homework.
    submitted_count: int = 0
    assigned_count: int = 0
    best_total: Optional[int] = None
    latest_total: Optional[int] = None
    average_total: Optional[float] = None
    baseline_total: Optional[int] = None
    improvement_from_baseline: Optional[int] = None
    official_result: Optional[ExamResultOut] = None


class BluebookGridOut(BaseModel):
    group_id: int
    group_name: str
    teacher_name: Optional[str] = None
    start_date: Optional[date] = None
    finish_date: Optional[date] = None
    columns: List[BluebookColumn]
    rows: List[BluebookStudentRow]
    column_stats: dict
    group_stats: dict

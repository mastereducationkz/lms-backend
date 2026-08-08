"""Exam-results domain models.

Two tables:

``exam_results``
    Normalized official exam attempts. Replaces the single-attempt scalars on
    ``assignment_zero_submissions`` (which are DB-constrained to one row per student,
    so a retake overwrote the previous score and a reschedule nulled it outright).
    Existing Assignment Zero scalars are backfilled here and kept in place as a
    read-only "latest" cache so current consumers keep working.

``bluebook_results``
    A narrow, queryable projection of Bluebook practice-test results written at
    submission time. The submission of record still lives in the normal
    ``assignment_submissions.answers`` JSON; this table exists because that column is
    ``Text`` (not JSONB), unindexed, and keyed by random ``task_<timestamp>_<rand>``
    ids, which makes a students x tests grid a full scan of un-indexable JSON.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from src.models.base import Base

# Exam types this model can represent. SAT and IELTS are populated today; NUET is
# accepted so the model does not need widening when NUET results start arriving.
EXAM_TYPES = ("sat", "ielts", "nuet")

# reported  - entered by a student or curator, not yet checked
# verified  - a staff member confirmed it against proof
# rejected  - checked and found wrong; retained for audit, excluded from reporting
EXAM_RESULT_STATUSES = ("reported", "verified", "rejected")

# Where the row came from. assignment_zero rows are backfilled historical data.
EXAM_RESULT_SOURCES = ("assignment_zero", "staff", "student")

BLUEBOOK_SOURCES = ("homework", "assignment_zero")

# Testimonial lifecycle. Nothing reaches the sales team before "approved", and
# "revoked" is terminal - consent can always be withdrawn.
TESTIMONIAL_STATUSES = ("draft", "pending", "approved", "rejected", "revoked")

# Where an approved testimonial may be used. Consent is per-channel: agreeing to a
# quote on the website is not agreeing to appear in paid advertising.
TESTIMONIAL_CHANNELS = ("website", "social", "ads", "print", "internal")

# Bluebook practice tests a teacher may assign.
BLUEBOOK_MIN_TEST_NUMBER = 4
BLUEBOOK_MAX_TEST_NUMBER = 11


class ExamResult(Base):
    """One official exam attempt by one student.

    Multiple rows per student per exam type are expected and supported - that is the
    whole point of this table. ``is_superseded`` marks a row corrected by a later
    entry without destroying the original, since there is no audit-log infrastructure
    in this codebase to reconstruct it from.
    """

    __tablename__ = "exam_results"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)

    exam_type = Column(String(16), nullable=False)

    # The date the exam was actually sat. Distinct from the PLANNED date, which stays
    # on assignment_zero_submissions - this domain never duplicates that calendar.
    test_date = Column(Date, nullable=False, index=True)

    # SAT: 400-1600. IELTS: 0.0-9.0 in half bands. Numeric covers both without
    # a second column or a lossy int cast.
    # Precision 6 (not 5): scale 2 leaves only 3 integer digits at precision 5, which
    # overflows on any SAT score of 1000+.
    total_score = Column(Numeric(6, 2), nullable=False)

    # SAT sections (200-800 each). Null for non-SAT exams.
    verbal_score = Column(Integer, nullable=True)
    math_score = Column(Integer, nullable=True)

    # IELTS bands (0.0-9.0, half steps). Null for non-IELTS exams.
    listening_band = Column(Numeric(2, 1), nullable=True)
    reading_band = Column(Numeric(2, 1), nullable=True)
    writing_band = Column(Numeric(2, 1), nullable=True)
    speaking_band = Column(Numeric(2, 1), nullable=True)

    # Evidence. Private-prefixed storage key; never a public URL.
    proof_url = Column(String, nullable=True)
    proof_uploaded_at = Column(DateTime, nullable=True)

    status = Column(String(16), nullable=False, default="reported")
    source = Column(String(32), nullable=False, default="staff")

    # There is no audit-log subsystem in this codebase, so actor columns live on the
    # row itself - the same idiom as AssignmentSubmission.graded_by.
    recorded_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    recorded_at = Column(DateTime, nullable=False,
                         default=lambda: datetime.now(timezone.utc))
    verified_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    verified_at = Column(DateTime, nullable=True)

    # A corrected row stays for audit but drops out of "current results" reads.
    is_superseded = Column(Boolean, nullable=False, default=False)

    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    student = relationship("UserInDB", foreign_keys=[student_id])

    __table_args__ = (
        # The cohort query: "everyone sitting SAT on 2026-10-03".
        Index("ix_exam_results_type_date", "exam_type", "test_date"),
        # The per-student history query, newest first.
        Index("ix_exam_results_student_type_date", "student_id", "exam_type", "test_date"),
        # Guards double-submission of the same attempt without blocking genuine
        # same-day retakes of a *different* exam type.
        UniqueConstraint("student_id", "exam_type", "test_date",
                         name="uq_exam_results_student_exam_date"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<ExamResult {self.student_id} {self.exam_type} {self.test_date} {self.total_score}>"


class BluebookResult(Base):
    """Queryable projection of one Bluebook practice-test result.

    ``total_score`` is stored rather than computed on read purely so the group grid and
    its statistics can aggregate in SQL. It is always ``verbal_score + math_score`` -
    the API derives it and never accepts it from the client.
    """

    __tablename__ = "bluebook_results"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)

    # Null for the Assignment Zero baseline row, which predates any assignment.
    assignment_id = Column(Integer, ForeignKey("assignments.id", ondelete="CASCADE"),
                           nullable=True, index=True)
    submission_id = Column(Integer, ForeignKey("assignment_submissions.id", ondelete="CASCADE"),
                           nullable=True)

    # Denormalized so the grid can apply row scope without joining through
    # assignments -> groups on every read.
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"),
                      nullable=True, index=True)

    test_number = Column(Integer, nullable=False)

    verbal_score = Column(Integer, nullable=False)
    math_score = Column(Integer, nullable=False)
    total_score = Column(Integer, nullable=False)

    screenshot_url = Column(String, nullable=True)

    # The official College Board PDF the scores were parsed from. Private storage key.
    report_url = Column(String, nullable=True)
    # Name as printed on the report. Kept so staff can spot a report submitted on
    # someone else's behalf; a mismatch flags the row but never blocks submission,
    # because Latin/Cyrillic spelling differences are routine here.
    report_student_name = Column(String, nullable=True)
    report_name_matches = Column(Boolean, nullable=True)
    report_date = Column(Date, nullable=True)

    # Staff correction of a parsed value. Students cannot edit a parsed score at all;
    # this is the escape hatch for a genuine parse failure, and it records who did it.
    overridden_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    overridden_at = Column(DateTime, nullable=True)
    override_reason = Column(Text, nullable=True)

    # Null for Assignment Zero baseline rows: AZ stores a Bluebook 5 score but no
    # companion date. The grid dates that column from the group's start instead, and
    # marks it as a baseline rather than pretending to know the day.
    taken_at = Column(Date, nullable=True)

    source = Column(String(32), nullable=False, default="homework")

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    student = relationship("UserInDB", foreign_keys=[student_id])

    __table_args__ = (
        # The grid query: every result for a group, in column order.
        Index("ix_bluebook_results_group_taken", "group_id", "taken_at"),
        Index("ix_bluebook_results_student_test", "student_id", "test_number"),
        # One projection row per student per assignment. Resubmission updates in
        # place rather than duplicating a column in the grid.
        UniqueConstraint("student_id", "assignment_id",
                         name="uq_bluebook_results_student_assignment"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<BluebookResult {self.student_id} #{self.test_number} {self.total_score}>"


class StudentTestimonial(Base):
    """A student's photo and quote, for marketing use, with its consent record.

    Consent is modelled explicitly rather than assumed. These are frequently minors, and
    the material is used in advertising, so the row has to answer "who agreed, to what,
    when, and who recorded it" long after the conversation happened. Without that a
    photo in a paid campaign is indefensible if challenged.

    Nothing is visible to the sales team until ``status == 'approved'``, and approval
    requires consent to have been recorded first. Revocation is always possible and
    immediately removes the material from the marketing view.
    """

    __tablename__ = "student_testimonials"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    # Optional link to the result being celebrated, so a quote can be shown next to the
    # score it refers to.
    exam_result_id = Column(Integer, ForeignKey("exam_results.id", ondelete="SET NULL"),
                            nullable=True)

    quote = Column(Text, nullable=True)
    photo_url = Column(String, nullable=True)
    photo_uploaded_at = Column(DateTime, nullable=True)

    status = Column(String(16), nullable=False, default="draft")

    # --- consent record -----------------------------------------------------------
    consent_given = Column(Boolean, nullable=False, default=False)
    # Which channels the student agreed to, e.g. ["website", "social"].
    consent_channels = Column(JSON, nullable=True)
    # Whether a guardian's permission was obtained. Required for minors; the age check
    # is a product decision made at approval time, not enforced blindly here, because
    # birthday data is incomplete for many students.
    guardian_consent = Column(Boolean, nullable=False, default=False)
    consent_note = Column(Text, nullable=True)
    consent_recorded_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    consent_recorded_at = Column(DateTime, nullable=True)

    # --- moderation ---------------------------------------------------------------
    approved_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    rejected_reason = Column(Text, nullable=True)

    # --- revocation ---------------------------------------------------------------
    revoked_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    revoked_reason = Column(Text, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    student = relationship("UserInDB", foreign_keys=[student_id])

    __table_args__ = (
        Index("ix_student_testimonials_status", "status"),
        # One testimonial per student keeps the marketing view unambiguous; a new quote
        # replaces the old one rather than accumulating variants nobody has approved.
        UniqueConstraint("student_id", name="uq_student_testimonials_student"),
    )

    @property
    def is_marketing_ready(self) -> bool:
        """Usable by the sales team: approved, consented, and not withdrawn."""
        return (
            self.status == "approved"
            and bool(self.consent_given)
            and self.revoked_at is None
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<StudentTestimonial {self.student_id} {self.status}>"

"""exam results + bluebook practice-result projection

Revision ID: ex1_exam_results_bluebook
Revises: wa1_chat_whatsapp_features
Create Date: 2026-08-08 00:00:00.000000

Adds the exam-results domain:

  * exam_results     -> normalized official exam attempts (SAT / IELTS / NUET).
                        Replaces the single-attempt scalars on
                        assignment_zero_submissions, which are DB-constrained to one
                        row per student, so a retake overwrote the prior score and a
                        reschedule nulled it outright.
  * bluebook_results -> queryable projection of Bluebook practice-test results,
                        written at submission time so the group grid does not have to
                        scan assignment_submissions.answers (a Text column keyed by
                        random task ids, which cannot be indexed as JSON).

Backfill: existing Assignment Zero SAT and IELTS results are copied into exam_results
with source='assignment_zero', and the Bluebook 5 baseline score is parsed into
bluebook_results. The Assignment Zero scalars are LEFT IN PLACE and keep working as a
read-only "latest" cache, so every current consumer is unaffected.

The Bluebook 5 backfill only inserts rows where BOTH sections parse out of the
"Math NNN, Verbal NNN" string; a total-only or malformed value is skipped rather than
guessed at. taken_at is left NULL because Assignment Zero stores no date for it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ex1_exam_results_bluebook"
down_revision: Union[str, Sequence[str], None] = "wa1_chat_whatsapp_features"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- exam_results ---------------------------------------------------------
    op.create_table(
        "exam_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("exam_type", sa.String(length=16), nullable=False),
        sa.Column("test_date", sa.Date(), nullable=False),
        # precision 6, not 5: scale 2 leaves 3 integer digits at precision 5, which
        # overflows on any SAT score of 1000+.
        sa.Column("total_score", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column("verbal_score", sa.Integer(), nullable=True),
        sa.Column("math_score", sa.Integer(), nullable=True),
        sa.Column("listening_band", sa.Numeric(precision=2, scale=1), nullable=True),
        sa.Column("reading_band", sa.Numeric(precision=2, scale=1), nullable=True),
        sa.Column("writing_band", sa.Numeric(precision=2, scale=1), nullable=True),
        sa.Column("speaking_band", sa.Numeric(precision=2, scale=1), nullable=True),
        sa.Column("proof_url", sa.String(), nullable=True),
        sa.Column("proof_uploaded_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="reported"),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="staff"),
        sa.Column("recorded_by", sa.Integer(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("verified_by", sa.Integer(), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("is_superseded", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recorded_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["verified_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("student_id", "exam_type", "test_date",
                            name="uq_exam_results_student_exam_date"),
    )
    op.create_index("ix_exam_results_student_id", "exam_results", ["student_id"])
    op.create_index("ix_exam_results_test_date", "exam_results", ["test_date"])
    op.create_index("ix_exam_results_type_date", "exam_results", ["exam_type", "test_date"])
    op.create_index("ix_exam_results_student_type_date", "exam_results",
                    ["student_id", "exam_type", "test_date"])

    # --- bluebook_results -----------------------------------------------------
    op.create_table(
        "bluebook_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("assignment_id", sa.Integer(), nullable=True),
        sa.Column("submission_id", sa.Integer(), nullable=True),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("test_number", sa.Integer(), nullable=False),
        sa.Column("verbal_score", sa.Integer(), nullable=False),
        sa.Column("math_score", sa.Integer(), nullable=False),
        sa.Column("total_score", sa.Integer(), nullable=False),
        sa.Column("screenshot_url", sa.String(), nullable=True),
        sa.Column("taken_at", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="homework"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["submission_id"], ["assignment_submissions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("student_id", "assignment_id",
                            name="uq_bluebook_results_student_assignment"),
    )
    op.create_index("ix_bluebook_results_student_id", "bluebook_results", ["student_id"])
    op.create_index("ix_bluebook_results_assignment_id", "bluebook_results", ["assignment_id"])
    op.create_index("ix_bluebook_results_group_id", "bluebook_results", ["group_id"])
    op.create_index("ix_bluebook_results_group_taken", "bluebook_results",
                    ["group_id", "taken_at"])
    op.create_index("ix_bluebook_results_student_test", "bluebook_results",
                    ["student_id", "test_number"])

    # --- backfill: Assignment Zero SAT results --------------------------------
    # Only rows with BOTH a score and a test date; the two columns are unconstrained
    # relative to each other and can diverge, and a result without a date cannot be
    # placed on a timeline.
    # Only migrate values that are unambiguously a single total score. sat_result_score
    # is free text; a value like "Math 700, Verbal 730" must NOT be silently recorded as
    # a total of 700. Anything that isn't a bare number is left for a human.
    op.execute(
        """
        INSERT INTO exam_results
            (student_id, exam_type, test_date, total_score, status, source,
             recorded_at, is_superseded, created_at, updated_at)
        SELECT az.user_id,
               'sat',
               az.sat_result_test_date,
               CAST(btrim(az.sat_result_score) AS numeric),
               'reported',
               'assignment_zero',
               now(), false, now(), now()
        FROM assignment_zero_submissions az
        WHERE az.sat_result_test_date IS NOT NULL
          AND az.sat_result_score IS NOT NULL
          AND btrim(az.sat_result_score) ~ '^[0-9]{3,4}$'
        ON CONFLICT ON CONSTRAINT uq_exam_results_student_exam_date DO NOTHING
        """
    )

    # --- backfill: Assignment Zero IELTS results ------------------------------
    op.execute(
        """
        INSERT INTO exam_results
            (student_id, exam_type, test_date, total_score, status, source,
             recorded_at, is_superseded, created_at, updated_at)
        SELECT az.user_id,
               'ielts',
               az.ielts_result_test_date,
               CAST(btrim(az.ielts_result_score) AS numeric),
               'reported',
               'assignment_zero',
               now(), false, now(), now()
        FROM assignment_zero_submissions az
        WHERE az.ielts_result_test_date IS NOT NULL
          AND az.ielts_result_score IS NOT NULL
          AND btrim(az.ielts_result_score) ~ '^[0-9](\\.[05])?$'
        ON CONFLICT ON CONSTRAINT uq_exam_results_student_exam_date DO NOTHING
        """
    )

    # --- backfill: Assignment Zero Bluebook 5 baseline ------------------------
    # Stored as the free-text "Math 600, Verbal 610" produced by the AZ form. Only
    # insert where both sections parse; taken_at stays NULL because AZ records no
    # date for this score.
    op.execute(
        """
        INSERT INTO bluebook_results
            (student_id, assignment_id, submission_id, group_id, test_number,
             verbal_score, math_score, total_score, screenshot_url, taken_at,
             source, created_at, updated_at)
        SELECT az.user_id,
               NULL, NULL, NULL,
               5,
               CAST(substring(az.bluebook_practice_test_5_score from 'Verbal[^0-9]*([0-9]+)') AS integer),
               CAST(substring(az.bluebook_practice_test_5_score from 'Math[^0-9]*([0-9]+)') AS integer),
               CAST(substring(az.bluebook_practice_test_5_score from 'Verbal[^0-9]*([0-9]+)') AS integer)
                 + CAST(substring(az.bluebook_practice_test_5_score from 'Math[^0-9]*([0-9]+)') AS integer),
               az.screenshot_url,
               NULL,
               'assignment_zero',
               now(), now()
        FROM assignment_zero_submissions az
        WHERE az.bluebook_practice_test_5_score IS NOT NULL
          AND substring(az.bluebook_practice_test_5_score from 'Verbal[^0-9]*([0-9]+)') IS NOT NULL
          AND substring(az.bluebook_practice_test_5_score from 'Math[^0-9]*([0-9]+)') IS NOT NULL
        ON CONFLICT ON CONSTRAINT uq_bluebook_results_student_assignment DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_bluebook_results_student_test", table_name="bluebook_results")
    op.drop_index("ix_bluebook_results_group_taken", table_name="bluebook_results")
    op.drop_index("ix_bluebook_results_group_id", table_name="bluebook_results")
    op.drop_index("ix_bluebook_results_assignment_id", table_name="bluebook_results")
    op.drop_index("ix_bluebook_results_student_id", table_name="bluebook_results")
    op.drop_table("bluebook_results")

    op.drop_index("ix_exam_results_student_type_date", table_name="exam_results")
    op.drop_index("ix_exam_results_type_date", table_name="exam_results")
    op.drop_index("ix_exam_results_test_date", table_name="exam_results")
    op.drop_index("ix_exam_results_student_id", table_name="exam_results")
    op.drop_table("exam_results")

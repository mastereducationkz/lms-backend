"""student testimonials + Bluebook PDF report provenance and staff override

Revision ID: ex2_testimonials_reports
Revises: ex1_exam_results_bluebook
Create Date: 2026-08-08 00:00:00.000000

Two changes:

  * bluebook_results gains report provenance (the College Board PDF the scores were
    parsed from, the name printed on it, whether that name matched the account, and the
    report's own date) plus staff-override columns. Students can no longer type a
    Bluebook score at all - it is parsed from the official report - so the override is
    the recorded escape hatch for a genuine parse failure.

  * student_testimonials stores a photo and quote for marketing use together with an
    explicit consent record. Consent is modelled rather than assumed because the
    subjects are frequently minors and the material is used in advertising: the row has
    to answer "who agreed, to what channels, when, and who recorded it" long after the
    conversation. Nothing is visible to the sales team before approval, and revocation
    is always available.

Additive only: every new column is nullable or carries a server default, so existing
bluebook_results rows are unaffected.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ex2_testimonials_reports"
down_revision: Union[str, Sequence[str], None] = "ex1_exam_results_bluebook"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- bluebook_results: report provenance + staff override ---------------------
    op.add_column("bluebook_results", sa.Column("report_url", sa.String(), nullable=True))
    op.add_column("bluebook_results", sa.Column("report_student_name", sa.String(), nullable=True))
    op.add_column("bluebook_results", sa.Column("report_name_matches", sa.Boolean(), nullable=True))
    op.add_column("bluebook_results", sa.Column("report_date", sa.Date(), nullable=True))
    op.add_column("bluebook_results", sa.Column("overridden_by", sa.Integer(), nullable=True))
    op.add_column("bluebook_results", sa.Column("overridden_at", sa.DateTime(), nullable=True))
    op.add_column("bluebook_results", sa.Column("override_reason", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_bluebook_results_overridden_by", "bluebook_results", "users",
        ["overridden_by"], ["id"], ondelete="SET NULL",
    )

    # --- student_testimonials -----------------------------------------------------
    op.create_table(
        "student_testimonials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("exam_result_id", sa.Integer(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("photo_url", sa.String(), nullable=True),
        sa.Column("photo_uploaded_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("consent_given", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("consent_channels", sa.JSON(), nullable=True),
        sa.Column("guardian_consent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("consent_note", sa.Text(), nullable=True),
        sa.Column("consent_recorded_by", sa.Integer(), nullable=True),
        sa.Column("consent_recorded_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by", sa.Integer(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column("revoked_by", sa.Integer(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["exam_result_id"], ["exam_results.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["consent_recorded_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["revoked_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        # One testimonial per student keeps the marketing view unambiguous.
        sa.UniqueConstraint("student_id", name="uq_student_testimonials_student"),
    )
    op.create_index("ix_student_testimonials_student_id", "student_testimonials", ["student_id"])
    op.create_index("ix_student_testimonials_status", "student_testimonials", ["status"])


def downgrade() -> None:
    op.drop_index("ix_student_testimonials_status", table_name="student_testimonials")
    op.drop_index("ix_student_testimonials_student_id", table_name="student_testimonials")
    op.drop_table("student_testimonials")

    op.drop_constraint("fk_bluebook_results_overridden_by", "bluebook_results", type_="foreignkey")
    op.drop_column("bluebook_results", "override_reason")
    op.drop_column("bluebook_results", "overridden_at")
    op.drop_column("bluebook_results", "overridden_by")
    op.drop_column("bluebook_results", "report_date")
    op.drop_column("bluebook_results", "report_name_matches")
    op.drop_column("bluebook_results", "report_student_name")
    op.drop_column("bluebook_results", "report_url")

"""Add configurable homework attempts and deadline reopen access."""
from alembic import op
import sqlalchemy as sa

revision = "at1_homework_attempt_policies"
down_revision = "ak1_task_answer_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assignments", sa.Column("max_attempts", sa.Integer(), nullable=True, server_default="1"))
    op.add_column("assignment_submissions", sa.Column("attempt_number", sa.Integer(), nullable=True, server_default="1"))
    op.add_column("assignment_submissions", sa.Column("is_current", sa.Boolean(), nullable=True, server_default=sa.true()))
    op.execute(sa.text("""
        WITH numbered AS (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY assignment_id, user_id ORDER BY submitted_at NULLS FIRST, id
            ) AS n,
            ROW_NUMBER() OVER (
                PARTITION BY assignment_id, user_id ORDER BY submitted_at DESC NULLS LAST, id DESC
            ) AS latest
            FROM assignment_submissions
        )
        UPDATE assignment_submissions s
        SET attempt_number = numbered.n,
            is_current = (numbered.latest = 1 AND COALESCE(s.is_hidden, false) = false)
        FROM numbered WHERE s.id = numbered.id
    """))
    op.alter_column("assignment_submissions", "attempt_number", nullable=False, server_default="1")
    op.alter_column("assignment_submissions", "is_current", nullable=False, server_default=sa.true())
    op.create_index("idx_assignment_submissions_current", "assignment_submissions", ["assignment_id", "user_id", "is_current"])
    op.create_table(
        "assignment_resubmission_access",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("assignment_id", sa.Integer(), sa.ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("student_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("granted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("assignment_id", "student_id", name="uq_assignment_resubmission_access"),
    )
    op.create_index("ix_assignment_resubmission_access_assignment_id", "assignment_resubmission_access", ["assignment_id"])
    op.create_index("ix_assignment_resubmission_access_student_id", "assignment_resubmission_access", ["student_id"])


def downgrade() -> None:
    op.drop_table("assignment_resubmission_access")
    op.drop_index("idx_assignment_submissions_current", table_name="assignment_submissions")
    op.drop_column("assignment_submissions", "is_current")
    op.drop_column("assignment_submissions", "attempt_number")
    op.drop_column("assignments", "max_attempts")

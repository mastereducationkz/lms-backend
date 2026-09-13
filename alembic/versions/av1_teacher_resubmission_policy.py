"""Add post-grade resubmission policy and active grade point ledger."""
from alembic import op
import sqlalchemy as sa


revision = "av1_teacher_resubmission_policy"
down_revision = "at1_homework_attempt_policies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assignment_resubmission_access",
        sa.Column("mode", sa.String(), nullable=False, server_default="one_extra"),
    )
    op.add_column(
        "assignment_submissions",
        sa.Column("is_grade_superseded", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "assignment_submissions",
        sa.Column("grade_points_awarded", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("assignment_submissions", "grade_points_awarded")
    op.drop_column("assignment_submissions", "is_grade_superseded")
    op.drop_column("assignment_resubmission_access", "mode")

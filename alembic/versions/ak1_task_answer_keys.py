"""add task answer-key release and acknowledgement records

Revision ID: ak1_task_answer_keys
Revises: ma9_fix_cancelled_attendance
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "ak1_task_answer_keys"
down_revision: Union[str, Sequence[str], None] = "ma9_fix_cancelled_attendance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assignment_answer_key_releases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("assignment_id", sa.Integer(), sa.ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("answer_key_id", sa.String(), nullable=False),
        sa.Column("released_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("assignment_id", "task_id", "answer_key_id", name="uq_assignment_answer_key_release"),
    )
    op.create_index("ix_assignment_answer_key_releases_assignment_id", "assignment_answer_key_releases", ["assignment_id"])
    op.create_table(
        "assignment_answer_key_acknowledgements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("assignment_id", sa.Integer(), sa.ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("answer_key_id", sa.String(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("assignment_id", "user_id", "task_id", "answer_key_id", name="uq_assignment_answer_key_ack"),
    )
    op.create_index("ix_assignment_answer_key_acknowledgements_assignment_id", "assignment_answer_key_acknowledgements", ["assignment_id"])
    op.create_index("ix_assignment_answer_key_acknowledgements_user_id", "assignment_answer_key_acknowledgements", ["user_id"])


def downgrade() -> None:
    op.drop_table("assignment_answer_key_acknowledgements")
    op.drop_table("assignment_answer_key_releases")

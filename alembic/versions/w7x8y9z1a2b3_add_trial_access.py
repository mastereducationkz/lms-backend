"""add trial access (users.is_trial + trial_accesses)

Revision ID: w7x8y9z1a2b3
Revises: p16_users_lower_email_idx
Create Date: 2026-07-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "w7x8y9z1a2b3"
down_revision: Union[str, Sequence[str], None] = "p16_users_lower_email_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_trial", sa.Boolean(), nullable=True))
    op.execute("UPDATE users SET is_trial = FALSE WHERE is_trial IS NULL")
    op.alter_column("users", "is_trial", nullable=False, server_default=sa.text("false"))
    op.create_index("ix_users_is_trial", "users", ["is_trial"])

    op.create_table(
        "trial_accesses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("course_id", sa.Integer(), sa.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lesson_ids", JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("granted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("prospect_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_trial_accesses_user_id", "trial_accesses", ["user_id"])
    op.create_index("ix_trial_accesses_course_id", "trial_accesses", ["course_id"])
    op.create_index("ix_trial_accesses_status", "trial_accesses", ["status"])
    op.create_index(
        "uq_trial_active_user_course", "trial_accesses", ["user_id", "course_id"],
        unique=True, postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_table("trial_accesses")
    op.drop_index("ix_users_is_trial", table_name="users")
    op.drop_column("users", "is_trial")

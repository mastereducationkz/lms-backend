"""Stored diagnostic entry bands per student (Platform Integration Pack §2.6 / E5 "start").

Revision ID: p21_platform_diagnostics
Revises: p20_student_targets
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "p21_platform_diagnostics"
down_revision = "p20_student_targets"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "platform_diagnostics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False, server_default="ielts"),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "platform", name="uq_platform_diagnostics_user_platform"),
    )


def downgrade():
    op.drop_table("platform_diagnostics")

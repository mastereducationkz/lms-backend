"""platform_test_events — the weekly_test calendar event of each weekly set (calendar events
replace platform_test homework, lead decision 2026-09-05).

Revision ID: p23_platform_test_events
Revises: cp2_lesson_kind
Create Date: 2026-09-05
"""
from alembic import op
import sqlalchemy as sa


revision = "p23_platform_test_events"
down_revision = "cp2_lesson_kind"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "platform_test_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("weekly_set_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("platform", "weekly_set_id", name="uq_platform_test_events_set"),
    )


def downgrade():
    op.drop_table("platform_test_events")

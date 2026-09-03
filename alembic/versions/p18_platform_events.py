"""Platform events storage (Platform Integration Pack §2.5).

platform_events (append-only copy of what IELTS/SAT push, 400-day retention),
platform_results (latest state per module attempt) and platform_weekly_sets.

Revision ID: p18_platform_events
Revises: p17_student_sync_target_state
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "p18_platform_events"
down_revision = "p17_student_sync_target_state"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "platform_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("zitadel_subject", sa.String(length=64), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("platform", "event_id", name="uq_platform_events_platform_event_id"),
    )
    op.create_index("ix_platform_events_user_occurred", "platform_events", ["user_id", "occurred_at"])
    op.create_index("ix_platform_events_occurred_at", "platform_events", ["occurred_at"])
    op.create_index("ix_platform_events_error", "platform_events", ["error"])

    op.create_table(
        "platform_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("track", sa.String(length=16), nullable=False),
        sa.Column("module", sa.String(length=16), nullable=False),
        sa.Column("test_id", sa.Integer(), nullable=True),
        sa.Column("test_title", sa.String(), nullable=True),
        sa.Column("attempt_ref", sa.String(length=64), nullable=False),
        sa.Column("weekly_set_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("band", sa.Float(), nullable=True),
        sa.Column("raw_score", sa.Integer(), nullable=True),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("result_url", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("scored_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("platform", "module", "attempt_ref", name="uq_platform_results_attempt"),
    )
    op.create_index("ix_platform_results_user_set", "platform_results", ["user_id", "weekly_set_id"])
    op.create_index("ix_platform_results_platform_set", "platform_results", ["platform", "weekly_set_id"])

    op.create_table(
        "platform_weekly_sets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("weekly_set_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("date_from", sa.Date(), nullable=True),
        sa.Column("date_to", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("track", sa.String(length=16), nullable=True),
        sa.Column("modules", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("platform", "weekly_set_id", name="uq_platform_weekly_sets_set"),
    )
    op.create_index(
        "ix_platform_weekly_sets_active_window", "platform_weekly_sets", ["platform", "is_active", "date_to"]
    )


def downgrade():
    op.drop_index("ix_platform_weekly_sets_active_window", table_name="platform_weekly_sets")
    op.drop_table("platform_weekly_sets")
    op.drop_index("ix_platform_results_platform_set", table_name="platform_results")
    op.drop_index("ix_platform_results_user_set", table_name="platform_results")
    op.drop_table("platform_results")
    op.drop_index("ix_platform_events_error", table_name="platform_events")
    op.drop_index("ix_platform_events_occurred_at", table_name="platform_events")
    op.drop_index("ix_platform_events_user_occurred", table_name="platform_events")
    op.drop_table("platform_events")

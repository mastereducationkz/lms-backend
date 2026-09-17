"""meet_staff_notices: staff Meet notifications through the Support bot (replaces recording_start_alerts)

recording_start_alerts shipped switched off (ENABLE_RECORDING_WATCHDOG was never set), so it holds
no rows; the owner moved the notices to the Support bot and a curators' forum topic the same day.

Revision ID: rw2_meet_staff_notices
Revises: rw1_recording_start_alerts
"""
import sqlalchemy as sa
from alembic import op

revision = "rw2_meet_staff_notices"
down_revision = "rw1_recording_start_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_recording_start_alerts_id", table_name="recording_start_alerts")
    op.drop_table("recording_start_alerts")
    op.create_table(
        "meet_staff_notices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=True),
        sa.Column("day", sa.Date(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("reply_status", sa.String(), nullable=True),
        sa.Column("reply_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.UniqueConstraint("kind", "event_id", name="uq_meet_staff_notice_lesson"),
        sa.UniqueConstraint("kind", "day", name="uq_meet_staff_notice_day"),
    )
    op.create_index("ix_meet_staff_notices_id", "meet_staff_notices", ["id"])


def downgrade() -> None:
    op.drop_index("ix_meet_staff_notices_id", table_name="meet_staff_notices")
    op.drop_table("meet_staff_notices")
    op.create_table(
        "recording_start_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conference_record", sa.String(), nullable=False),
        sa.Column("teacher_in_room", sa.Boolean(), nullable=True),
        sa.Column("people_in_room", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("alerted_at", sa.DateTime(), nullable=False),
        sa.Column("recording_started_at", sa.DateTime(), nullable=True),
        sa.Column("messages", sa.JSON(), nullable=True),
        sa.UniqueConstraint("event_id", name="uq_recording_start_alert_event"),
    )
    op.create_index("ix_recording_start_alerts_id", "recording_start_alerts", ["id"])

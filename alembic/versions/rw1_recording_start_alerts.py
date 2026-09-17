"""recording_start_alerts: a live lesson with people in the room and no recording, told to staff

Revision ID: rw1_recording_start_alerts
Revises: gb3_pinned_timetable_on_top
"""
import sqlalchemy as sa
from alembic import op

revision = "rw1_recording_start_alerts"
down_revision = "gb3_pinned_timetable_on_top"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index("ix_recording_start_alerts_id", table_name="recording_start_alerts")
    op.drop_table("recording_start_alerts")

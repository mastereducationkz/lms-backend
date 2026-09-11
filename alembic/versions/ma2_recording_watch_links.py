"""recording watch links: a three-hour, one-lesson link to a recording, issued through the CRM

Revision ID: ma2_recording_watch_links
Revises: ma1_meet_attendance
Create Date: 2026-09-11

Accountants check recordings from the CRM and have no LMS account. The CRM asks for a link over
its service channel; the link opens only that lesson's recording, expires after three hours,
and records who asked and whether it was opened. One new table, nothing else touched.
"""
from alembic import op
import sqlalchemy as sa


revision = "ma2_recording_watch_links"
down_revision = "ma1_meet_attendance"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "recording_watch_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("issued_to", sa.String(), nullable=True),
        sa.Column("issued_role", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("first_opened_at", sa.DateTime(), nullable=True),
        sa.Column("last_opened_at", sa.DateTime(), nullable=True),
        sa.Column("open_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_recording_watch_links_id", "recording_watch_links", ["id"])
    op.create_index("ix_recording_watch_links_event_id", "recording_watch_links", ["event_id"])


def downgrade():
    op.drop_index("ix_recording_watch_links_event_id", table_name="recording_watch_links")
    op.drop_index("ix_recording_watch_links_id", table_name="recording_watch_links")
    op.drop_table("recording_watch_links")

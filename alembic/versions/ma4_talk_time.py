"""talk time: the LMS switch, rooms' transcription state, Meet's who-spoke-when, lesson transcripts

Revision ID: ma4_talk_time
Revises: ma3_meet_flag_reviews
Create Date: 2026-09-11

Four new tables, nothing else touched. The switch starts off: nothing is transcribed until an
admin turns talk time on inside the LMS.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "ma4_talk_time"
down_revision = "ma3_meet_flag_reviews"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "meet_room_transcription",
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("transcription", sa.Boolean(), nullable=False),
        sa.Column("applied_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_table(
        "meet_speech",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conference_id", sa.Integer(), sa.ForeignKey("meet_conferences.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("origin", sa.DateTime(), nullable=True),
        sa.Column("participants", postgresql.JSONB(), nullable=True),
        sa.Column("entries", postgresql.JSONB(), nullable=True),
        sa.Column("document_id", sa.String(), nullable=True),
        sa.Column("saved_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_meet_speech_event_id", "meet_speech", ["event_id"])
    op.create_table(
        "lesson_transcripts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("provider", sa.String(16), nullable=False, server_default="deepgram"),
        sa.Column("recording_started_at", sa.DateTime(), nullable=True),
        sa.Column("audio_seconds", sa.Float(), nullable=True),
        sa.Column("utterances", postgresql.JSONB(), nullable=True),
        sa.Column("languages", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table("lesson_transcripts")
    op.drop_index("ix_meet_speech_event_id", table_name="meet_speech")
    op.drop_table("meet_speech")
    op.drop_table("meet_room_transcription")
    op.drop_table("app_settings")

"""meet attendance: who was in each lesson's Meet room, when, and whose Google account is whose

Revision ID: ma1_meet_attendance
Revises: tg1_telegram_lesson_invitations
Create Date: 2026-09-11

Meet reports every join and leave in a lesson's room, identified by Google account id and
display name. Google keeps that for about 30 days, so the LMS keeps its own copy: the calls,
the people in them and each of their sessions — plus the one-time human confirmation of which
account belongs to whom. Four new tables, nothing else touched.
"""
from alembic import op
import sqlalchemy as sa


revision = "ma1_meet_attendance"
down_revision = "tg1_telegram_lesson_invitations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "meet_conferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conference_record", sa.String(), nullable=False, unique=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_meet_conferences_id", "meet_conferences", ["id"])
    op.create_index("ix_meet_conferences_event_id", "meet_conferences", ["event_id"])

    op.create_table(
        "meet_participants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conference_id", sa.Integer(), sa.ForeignKey("meet_conferences.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("participant_name", sa.String(), nullable=False, unique=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("google_user", sa.String(), nullable=True),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("lesson_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("lesson_not_a_student", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_meet_participants_id", "meet_participants", ["id"])
    op.create_index("ix_meet_participants_conference_id", "meet_participants", ["conference_id"])
    op.create_index("ix_meet_participants_event_id", "meet_participants", ["event_id"])
    op.create_index("ix_meet_participants_google_user", "meet_participants", ["google_user"])

    op.create_table(
        "meet_participant_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("participant_id", sa.Integer(), sa.ForeignKey("meet_participants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("session_name", sa.String(), nullable=False, unique=True),
        sa.Column("joined_at", sa.DateTime(), nullable=False),
        sa.Column("left_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_meet_participant_sessions_id", "meet_participant_sessions", ["id"])
    op.create_index("ix_meet_participant_sessions_participant_id", "meet_participant_sessions",
                    ["participant_id"])

    op.create_table(
        "google_account_links",
        sa.Column("google_user", sa.String(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("not_a_student", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("confirmed_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("(user_id IS NOT NULL) <> not_a_student", name="ck_google_account_link_target"),
    )
    op.create_index("ix_google_account_links_user_id", "google_account_links", ["user_id"])


def downgrade():
    op.drop_index("ix_google_account_links_user_id", table_name="google_account_links")
    op.drop_table("google_account_links")
    op.drop_index("ix_meet_participant_sessions_participant_id", table_name="meet_participant_sessions")
    op.drop_index("ix_meet_participant_sessions_id", table_name="meet_participant_sessions")
    op.drop_table("meet_participant_sessions")
    op.drop_index("ix_meet_participants_google_user", table_name="meet_participants")
    op.drop_index("ix_meet_participants_event_id", table_name="meet_participants")
    op.drop_index("ix_meet_participants_conference_id", table_name="meet_participants")
    op.drop_index("ix_meet_participants_id", table_name="meet_participants")
    op.drop_table("meet_participants")
    op.drop_index("ix_meet_conferences_event_id", table_name="meet_conferences")
    op.drop_index("ix_meet_conferences_id", table_name="meet_conferences")
    op.drop_table("meet_conferences")
